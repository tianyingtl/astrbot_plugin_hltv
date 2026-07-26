"""HLTV 数据访问层。

上层（main.py 指令层）只依赖本模块的 HltvClient / HltvError 与缓存键助手，
不直接接触 hltv-async-api。之后若想更换数据源（自建爬虫、
第三方 REST 镜像等），只需改写本文件，指令层与格式化层不动。

实现均以 hltv-async-api 0.8.3 的**源码**为准（其 README 多处过时，
如 safe_mode 实际默认 False、方法名 get_top_players、字段名 nickname）。
"""

import asyncio
import time
from datetime import datetime
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import aiohttp

from astrbot.api import logger

try:
    from hltv_async_api import Hltv
except ImportError:
    Hltv = None

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


class HltvError(Exception):
    """对上层暴露的统一异常，message 可直接回复给用户。"""


# 锁排队的最长等待时间（秒）。HLTV 被风控时单次请求可能占锁数十秒，
# 超过该时长的排队请求直接快速失败，避免用户无限等待。
_QUEUE_TIMEOUT = 30

# 比赛列表的缓存上限（秒）：live/today 的"进行中"状态不能吃默认 5 分钟
# 缓存，否则已结束的比赛会滞留在直播列表里。
_LIVE_TTL = 60

# HLTV 站内搜索接口（返回 JSON），hltv-async-api 没有封装，自行请求。
# 结构：[{"teams": [{id, name, ...}], "players": [{id, nickname, ...}], ...}]
_SEARCH_URL = "https://www.hltv.org/search?term={}"

_BROWSER_HEADERS = {
    "referer": "https://www.hltv.org/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
}

# 比赛条目的日期格式（hltv-async-api 源码 get_matches 用 %d-%m-%Y，
# 直播比赛的 date/time 字段固定为字符串 'LIVE'）
_DATE_FMT = "%d-%m-%Y"
LIVE = "LIVE"


# ------------------------------------------------------------------ 缓存键
# 指令层用它们探测"结果是否已有缓存"（决定要不要发等待提示），
# 与本模块内部使用的键保持同源，避免两处拼写漂移。

def matches_key(days: int, min_stars: int) -> str:
    return f"matches:{days}:{min_stars}"


def results_key(days: int, min_stars: int) -> str:
    return f"results:{days}:{min_stars}"


RANKING_KEY = "top_teams:50"
EVENTS_KEY = "events"
NEWS_KEY = "news"


class HltvClient:
    def __init__(
        self,
        *,
        proxy_list: list[str] | None = None,
        timeout: int = 15,
        max_retries: int = 3,
        tz: str = "Asia/Shanghai",
        cache_ttl: int = 300,
    ):
        tz = self._validate_tz(tz)
        self._hltv_opts: dict[str, Any] = {
            "timeout": timeout,
            "max_retries": max_retries,
            "tz": tz,
            # get_matches / get_results / get_top_players 在 safe_mode 下
            # 直接返回 None，必须显式关闭（0.8.3 默认即 False，防默认值变动）
            "safe_mode": False,
        }
        if proxy_list:
            self._hltv_opts["proxy_list"] = proxy_list
        self._proxy_list = proxy_list or []
        self._timeout = timeout
        self._tz = tz
        self._cache_ttl = cache_ttl
        # 键 -> (写入时刻, 该条目的 TTL, 值)
        self._cache: dict[str, tuple[float, float, Any]] = {}
        # HLTV 有 Cloudflare 风控，串行化所有请求以降低触发概率
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 基础设施

    @staticmethod
    def _validate_tz(tz: str) -> str:
        """校验时区名。无效时区会让库端（静默退回哥本哈根时间）和
        本地"今天"判定（退回服务器时间）错位，导致 today 漏比赛，
        所以提前统一回退到默认值。"""
        if ZoneInfo is not None:
            try:
                ZoneInfo(tz)
            except Exception:
                logger.warning(f"[hltv] 无效时区 {tz!r}，已回退 Asia/Shanghai")
                return "Asia/Shanghai"
        return tz

    def _cache_get(self, key: str) -> Any | None:
        if self._cache_ttl <= 0:
            return None
        hit = self._cache.get(key)
        if not hit:
            return None
        ts, ttl, value = hit
        if time.monotonic() - ts >= ttl:
            # 过期即删，避免长期运行时缓存字典无界增长
            del self._cache[key]
            return None
        return value

    def is_fresh(self, key: str) -> bool:
        """指令层探针：该键是否有未过期缓存（用于跳过等待提示）。"""
        return self._cache_get(key) is not None

    async def _cached_locked(
        self,
        cache_key: str,
        fetch: Callable[[], Awaitable[Any]],
        ttl: float | None = None,
    ) -> Any:
        """统一入口：缓存 → 加锁（限时排队）→ 请求 → 缓存回填。

        fetch 内部应自行把异常归一化为 HltvError；返回 None 视为失败，
        空列表等空值是合法结果，照常缓存，避免"无数据"场景穿透缓存。
        ttl 为该条目的缓存时长，缺省用全局 cache_ttl。
        """
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            raise HltvError("前面还有 HLTV 查询在排队，请稍后再试。") from None
        try:
            # 排队期间可能已有同样的请求完成，再查一次缓存
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
            result = await fetch()
        finally:
            self._lock.release()
        if result is None:
            raise HltvError("HLTV 未返回数据（可能被风控拦截）。")
        if self._cache_ttl > 0:
            self._cache[cache_key] = (
                time.monotonic(),
                ttl if ttl is not None else self._cache_ttl,
                result,
            )
        return result

    async def _call(
        self,
        method: str,
        /,
        *args: Any,
        cache_key: str,
        ttl: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """调用 hltv-async-api 的方法。"""
        if Hltv is None:
            raise HltvError(
                "依赖 hltv-async-api 未安装。请在 WebUI 插件管理中安装依赖，"
                "或手动执行 pip install hltv-async-api 后重载插件。"
            )

        async def fetch() -> Any:
            try:
                async with Hltv(**self._hltv_opts) as hltv:
                    return await getattr(hltv, method)(*args, **kwargs)
            except Exception as e:
                logger.error(f"[hltv] 调用 {method} 失败: {e!r}")
                raise HltvError(
                    "请求 HLTV 失败，可能是网络问题或触发了 Cloudflare 风控，"
                    "可稍后重试或在插件配置中设置代理。"
                ) from e

        return await self._cached_locked(cache_key, fetch, ttl=ttl)

    async def _search(self, term: str) -> dict:
        """HLTV 站内搜索，返回 {"players": [...], "teams": [...], ...}。

        配置了多个代理时逐个尝试（对齐库内 _switch_proxy 的容错行为），
        全部失败才抛错。
        """

        async def attempt(proxy: str | None) -> dict:
            url = _SEARCH_URL.format(quote(term))
            client_timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.get(
                    url, headers=_BROWSER_HEADERS, proxy=proxy
                ) as resp:
                    if resp.status != 200:
                        raise HltvError(
                            f"HLTV 搜索接口返回 {resp.status}"
                            "（可能被风控拦截，可稍后重试或配置代理）。"
                        )
                    data = await resp.json(content_type=None)
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return data[0]
            if isinstance(data, dict):
                return data
            return {}

        async def fetch() -> dict:
            last_err: Exception | None = None
            for proxy in self._proxy_list or [None]:
                try:
                    return await attempt(proxy)
                except Exception as e:
                    logger.warning(
                        f"[hltv] 搜索 {term!r} 经 {proxy or '直连'} 失败: {e!r}"
                    )
                    last_err = e
            if isinstance(last_err, HltvError):
                raise last_err
            raise HltvError(
                "请求 HLTV 搜索接口失败，可能是网络/代理问题或触发了风控。"
            ) from last_err

        return await self._cached_locked(f"search:{term.strip().lower()}", fetch)

    def _today_str(self) -> str:
        """配置时区下的今天，格式与比赛条目 date 字段一致（DD-MM-YYYY）。"""
        now = None
        if ZoneInfo is not None:
            try:
                now = datetime.now(ZoneInfo(self._tz))
            except Exception:
                now = None
        if now is None:
            now = datetime.now()
        return now.strftime(_DATE_FMT)

    # ---------------------------------------------------------------- 领域方法
    # 返回结构均为 hltv-async-api 的原始数据，字段含义见 README「数据结构」一节。

    async def get_matches(self, days: int = 1, min_stars: int = 0) -> list[dict]:
        """近期（含进行中）比赛列表，min_stars 为 HLTV 星级下限（0-5）。

        含"进行中"状态，缓存时长压到 _LIVE_TTL，避免已结束的比赛
        在 live/today 里滞留 5 分钟。
        """
        return await self._call(
            "get_matches",
            days=days,
            min_rating=min_stars,
            cache_key=matches_key(days, min_stars),
            ttl=min(float(self._cache_ttl or _LIVE_TTL), _LIVE_TTL),
        )

    async def get_today_matches(self, min_stars: int = 0) -> list[dict]:
        """今日赛程（配置时区）：直播中的 + 今天开赛的。

        get_matches 的 days 是"取页面前 N 个日期分组"，今天没有未开赛
        场次时第一组可能是明天，所以取 2 组后按日期过滤。
        """
        matches = await self.get_matches(days=2, min_stars=min_stars)
        today = self._today_str()
        return [m for m in matches if m.get("date") in (LIVE, today)]

    async def get_live_matches(self) -> list[dict]:
        """进行中的比赛（源码约定：直播条目的 date 字段为 'LIVE'）。"""
        matches = await self.get_matches(days=1, min_stars=0)
        return [m for m in matches if m.get("date") == LIVE]

    async def get_results(self, days: int = 1, min_stars: int = 0) -> list[dict]:
        """近期赛果，与 matches 一致按星级过滤。

        featured=False：库默认会把"精选赛果"box 混进来——无日期、
        可能超出天数窗口、且和按日分组的条目重复，这里只取按日列表。
        """
        return await self._call(
            "get_results",
            days=days,
            min_rating=min_stars,
            featured=False,
            cache_key=results_key(days, min_stars),
        )

    async def get_top_teams(self, max_teams: int = 50) -> list[dict]:
        """战队世界排名（HLTV 榜单有多少取多少，上限 max_teams）。"""
        return await self._call(
            "get_top_teams", max_teams=max_teams, cache_key=f"top_teams:{max_teams}"
        )

    async def get_events(self) -> list[dict]:
        """进行中/即将开始的赛事。"""
        return await self._call("get_events", cache_key=EVENTS_KEY)

    async def get_news(self) -> list[dict]:
        """今日新闻。max_reg_news 抬高到 10，库默认 2 条太少。"""
        return await self._call(
            "get_last_news", only_today=True, max_reg_news=10, cache_key=NEWS_KEY
        )

    async def find_team(self, name: str) -> dict:
        """查战队：先在排名榜（Top 50）内模糊匹配（附带排名信息、命中率高），
        找不到再走 HLTV 站内搜索，因此任意战队都能查。"""
        needle = name.strip().lower()
        try:
            teams = await self.get_top_teams(50)
        except HltvError:
            teams = []
        matched = next(
            (t for t in teams if needle in str(t.get("title", "")).lower()), None
        )
        if matched:
            team_id, title = matched.get("id"), matched.get("title")
        else:
            found = (await self._search(name)).get("teams") or []
            if not found or not isinstance(found[0], dict) or "id" not in found[0]:
                raise HltvError(f"没有找到战队「{name}」。")
            team_id = found[0]["id"]
            title = found[0].get("name") or found[0].get("title") or name
        return await self._call(
            "get_team_info", team_id, str(title), cache_key=f"team:{team_id}"
        )

    async def find_player(self, nickname: str) -> dict:
        """查选手：直接走 HLTV 站内搜索，任意选手都能查；
        搜索接口被风控时退回选手榜（Top 100）模糊匹配。"""
        needle = nickname.strip().lower()
        pid = nick = None
        try:
            found = (await self._search(nickname)).get("players") or []
            if found and isinstance(found[0], dict) and "id" in found[0]:
                pid = found[0]["id"]
                nick = found[0].get("nickname") or found[0].get("name") or nickname
        except HltvError as search_err:
            logger.warning(f"[hltv] 搜索接口不可用，退回选手榜匹配: {search_err}")
        if pid is None:
            try:
                players = await self._call(
                    "get_top_players", top=100, cache_key="top_players:100"
                )
            except HltvError:
                players = []
            matched = next(
                (
                    p
                    for p in players
                    if needle in str(p.get("nickname", "")).lower()
                ),
                None,
            )
            if not matched:
                raise HltvError(f"没有找到选手「{nickname}」。")
            pid, nick = matched["id"], matched["nickname"]
        return await self._call(
            "get_player_info", pid, str(nick), cache_key=f"player:{pid}"
        )

    def clear_cache(self) -> None:
        self._cache.clear()
