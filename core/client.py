"""HLTV 数据访问层。

上层（main.py 指令层）只依赖本模块的 HltvClient / HltvError 与缓存键助手，
不直接接触 hltv-async-api。

分工（2026-07 实测 HLTV 各页面后确定）：
- matches 页已改版，hltv-async-api 0.8.3 的选择器全部失效 → 自建解析
  （新版 DOM 的 data-* 属性含 match-id/stars/live/unix 时间戳，更稳）
- team 页新增 "Valve ranking" 行导致库按位置取值错位 → 自建按标签解析
- VRS（Valve 排名）为新功能，HLTV 站内 /valve-ranking/ 页面 → 自建解析
- 新闻改自建主页解析：保留完整文章链接供详情查看
- results / events 页选择器实测健在 → 继续用库
- player 页新增 Top20 / Major / 奖杯信息，且库的旧统计选择器已失效 → 自建解析
- 传输层统一复用库的 _fetch（重试/代理轮换/Cloudflare 检测）
"""

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urljoin, urlparse

import aiohttp
from PIL import Image, UnidentifiedImageError

from astrbot.api import logger

try:
    from curl_cffi import CurlError
    from curl_cffi.const import CurlECode, CurlWsFlag
    from curl_cffi.requests import Session as CurlSession
except (ImportError, OSError):
    CurlSession = None
    CurlError = OSError
    CurlECode = None
    CurlWsFlag = None

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


# 锁排队的最长等待时间（秒）
_QUEUE_TIMEOUT = 30

# 比赛列表缓存上限（秒）：直播状态不能吃默认 5 分钟缓存
_LIVE_TTL = 60

_SEARCH_URL = "https://www.hltv.org/search?term={}"
_MATCHES_URL = "https://www.hltv.org/matches"
_SCOREBOT_URL = "https://scorebot-lb.hltv.org"
_SCOREBOT_HOSTS = {"scorebot-lb.hltv.org", "scorebot-secure.hltv.org"}
_RANKING_URL = "https://www.hltv.org/ranking/teams"
_VRS_URL = "https://www.hltv.org/valve-ranking/teams"
_HOME_URL = "https://www.hltv.org/"
_HLTV_TOP20_IMAGE_HOSTS = {
    "www.hltv.org",
    "img-cdn.hltv.org",
    "pbs.twimg.com",
}
_FIVEE_ARTICLE_HOSTS = {"csgo.5eplay.com", "www.5eplay.com"}
_FIVEE_IMAGE_HOSTS = {"oss.5eplay.com", "static.5eplay.com"}
_TOP20_IMAGE_HOSTS = _HLTV_TOP20_IMAGE_HOSTS | _FIVEE_IMAGE_HOSTS

_FIVEE_TOP20_POSTERS = {
    2013: (
        "https://csgo.5eplay.com/article/241203sredm0",
        "https://oss.5eplay.com/editor/20241203/cefd72f358ccaf2e07fb17df5f7a01e4.png",
    ),
    2014: (
        "https://csgo.5eplay.com/article/241203lys127",
        "https://oss.5eplay.com/editor/20241204/fb37ad1a8245978a3ce7171f0f7cfdf3.png",
    ),
    2015: (
        "https://csgo.5eplay.com/article/241203epzqv5",
        "https://oss.5eplay.com/editor/20241208/c79e22058dd2bbe7ffb88482db054a44.png",
    ),
    2016: (
        "https://csgo.5eplay.com/article/241207v670bc",
        "https://oss.5eplay.com/editor/20241208/4b93e3ff0eeb95db549b3a89c4bc5476.png",
    ),
    2017: (
        "https://csgo.5eplay.com/article/2412118yczqm",
        "https://oss.5eplay.com/editor/20241215/d1392c41dafc5194b0b7afbc0412f21f.png",
    ),
    2018: (
        "https://csgo.5eplay.com/article/24121604ul8k",
        "https://oss.5eplay.com/editor/20241217/ec2537a0c410cc792ae5f0d9fc800c21.png",
    ),
    2019: (
        "https://csgo.5eplay.com/article/241219aqh78b",
        "https://oss.5eplay.com/editor/20241219/b05ea4fe369317d6704c384ed6e39d79.png",
    ),
    2020: (
        "https://csgo.5eplay.com/article/241222w18iln",
        "https://oss.5eplay.com/editor/20241222/deaed6e750b2bb141eff021cd18c56b4.png",
    ),
    2021: (
        "https://csgo.5eplay.com/article/241223kg2h87",
        "https://oss.5eplay.com/editor/20241223/562dfb9c39ad974bc39109d7e47b43f9.png",
    ),
    2022: (
        "https://csgo.5eplay.com/article/241223rlpeb9",
        "https://oss.5eplay.com/editor/20241224/148e904ccbdcf7799d77a69f25a732b5.jpg",
    ),
    2023: (
        "https://csgo.5eplay.com/article/241225dr0172",
        "https://oss.5eplay.com/editor/20241225/3c187f9d1fb3d64caa1ae4b6b2ae31df.png",
    ),
    2024: (
        "https://csgo.5eplay.com/article/251223wl4s8c",
        "https://oss.5eplay.com/editor/20251223/59625684505e256e89998280366ab1e2.png",
    ),
}

_BROWSER_HEADERS = {
    "referer": "https://www.hltv.org/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
}

_DATE_FMT = "%d-%m-%Y"
LIVE = "LIVE"

# /hltv ranking 的地区参数 → HLTV VRS 页面的地区名
VRS_REGIONS = {
    "asia": "Asia",
    "europe": "Europe",
    "americas": "Americas",
    "america": "Americas",
    "eu": "Europe",
    "na": "Americas",
    "亚洲": "Asia",
    "欧洲": "Europe",
    "美洲": "Americas",
}


# ------------------------------------------------------------------ 缓存键
# 指令层用它们探测"结果是否已有缓存"（决定要不要发等待提示），
# 与本模块内部使用的键保持同源。

MATCHES_RAW_KEY = "matches_raw"
HOME_LIVE_KEY = "home_live"
RANKING_HLTV_KEY = "ranking:hltv:50"
EVENTS_KEY = "events"
NEWS_KEY = "news"
TOP20_MIN_YEAR = 2010


def results_key(days: int, min_stars: int) -> str:
    return f"results:{days}:{min_stars}"


def vrs_key(region: str | None) -> str:
    return f"ranking:vrs:{region or 'global'}"


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
            # 库在 safe_mode 下 get_results 等直接返回 None，必须显式关闭
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
        if ZoneInfo is not None:
            try:
                ZoneInfo(tz)
            except Exception:
                logger.warning(f"[hltv] 无效时区 {tz!r}，已回退 Asia/Shanghai")
                return "Asia/Shanghai"
        return tz

    def _tzinfo(self):
        if ZoneInfo is not None:
            try:
                return ZoneInfo(self._tz)
            except Exception:
                pass
        return None

    def _now_local(self) -> datetime:
        tzi = self._tzinfo()
        return datetime.now(tzi) if tzi else datetime.now()

    def _cache_get(self, key: str) -> Any | None:
        if self._cache_ttl <= 0:
            return None
        hit = self._cache.get(key)
        if not hit:
            return None
        ts, ttl, value = hit
        if time.monotonic() - ts >= ttl:
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
        空列表等空值是合法结果，照常缓存。注意：fetch 内部严禁再调用
        _cached_locked（锁不可重入），需要多次抓取时直接连用 _fetch_raw。
        """
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            raise HltvError("前面还有 HLTV 查询在排队，请稍后再试。") from None
        try:
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
        """调用 hltv-async-api 的方法（仅用于实测仍健在的页面）。"""
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

    async def _fetch_raw(self, url: str) -> Any:
        """经 hltv-async-api 的传输层抓任意 HLTV 页面，返回 BeautifulSoup。

        依赖库私有方法 _fetch（版本已锁 ~=0.8.3）——它的重试、代理轮换、
        Cloudflare 检测正是自建 aiohttp 请求缺的东西。仅供各 fetch 闭包
        内部调用，调用时 self._lock 已被 _cached_locked 持有。
        """
        if Hltv is None:
            raise HltvError(
                "依赖 hltv-async-api 未安装。请在 WebUI 插件管理中安装依赖后重载插件。"
            )
        try:
            async with Hltv(**self._hltv_opts) as hltv:
                fetcher = getattr(hltv, "_fetch", None)
                if fetcher is None:
                    raise HltvError(
                        "hltv-async-api 版本不兼容（缺少 _fetch），请安装 0.8.x 版本。"
                    )
                page = await fetcher(url)
        except HltvError:
            raise
        except Exception as e:
            logger.error(f"[hltv] 抓取 {url} 失败: {e!r}")
            raise HltvError(
                "请求 HLTV 失败，可能是网络问题或触发了 Cloudflare 风控，"
                "可稍后重试或在插件配置中设置代理。"
            ) from e
        if page is None:
            raise HltvError("HLTV 未返回数据（可能被风控拦截）。")
        return page

    # ---------------------------------------------------------------- 站内搜索

    @staticmethod
    def _normalize_search(data: Any) -> dict:
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        if isinstance(data, dict):
            return data
        return {}

    async def _search(self, term: str) -> dict:
        """HLTV 站内搜索，返回 {"players": [...], "teams": [...], ...}。

        搜索 JSON 接口对非浏览器流量的风控比普通页面严：先用 aiohttp
        多次尝试（轮换代理），全失败后退回库的传输层抓同一接口
        （JSON 文本会被包进 soup，取纯文本还原）。
        """
        url = _SEARCH_URL.format(quote(term))

        async def attempt(proxy: str | None) -> dict:
            client_timeout = aiohttp.ClientTimeout(total=min(self._timeout, 8))
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.get(
                    url, headers=_BROWSER_HEADERS, proxy=proxy
                ) as resp:
                    if resp.status != 200:
                        raise HltvError(f"HLTV 搜索接口返回 {resp.status}")
                    data = await resp.json(content_type=None)
            return self._normalize_search(data)

        async def fetch() -> dict:
            proxies = self._proxy_list or [None]
            for i in range(3):
                proxy = proxies[i % len(proxies)]
                try:
                    return await attempt(proxy)
                except Exception as e:
                    logger.warning(
                        f"[hltv] 搜索 {term!r} 第 {i + 1} 次（经 {proxy or '直连'}）失败: {e!r}"
                    )
                    await asyncio.sleep(1)
            try:
                page = await self._fetch_raw(url)
                text = page.get_text() if hasattr(page, "get_text") else str(page)
                return self._normalize_search(json.loads(text.strip()))
            except Exception as e:
                logger.warning(f"[hltv] 搜索兜底通道也失败: {e!r}")
            raise HltvError(
                "HLTV 搜索接口不可用（多次重试仍失败），可稍后再试或配置代理。"
            )

        return await self._cached_locked(f"search:{term.strip().lower()}", fetch)

    # ---------------------------------------------------------------- 比赛

    @staticmethod
    def _parse_matches_page(page: Any) -> list[dict]:
        """新版 matches 页解析。关键信息都在 data-* 属性上：
        data-match-id / data-stars / live / .match-time[data-unix]。
        "Matches for you" 推荐区与正文重复，按 match-id 去重。"""
        items: list[dict] = []
        by_id: dict[str, dict] = {}
        for w in page.find_all("div", class_="match-wrapper"):
            mid = str(w.get("data-match-id") or "")
            if not mid:
                continue
            try:
                stars = int(w.get("data-stars") or 0)
            except (ValueError, TypeError):
                stars = 0
            event = ""
            ev = w.find("div", class_="match-event")
            if ev is not None:
                event = str(ev.get("data-event-headline") or "").strip() or ev.get_text(
                    " ", strip=True
                )
            best_of = ""
            for meta in w.find_all("div", class_="match-meta"):
                match_format = re.fullmatch(
                    r"bo\s*(\d+)", meta.get_text(" ", strip=True), re.IGNORECASE
                )
                if match_format:
                    best_of = f"BO{match_format.group(1)}"
                    break
            names = [t.get_text(strip=True) for t in w.find_all("div", class_="match-teamname")]
            team_ids = []
            for score in w.select("[data-livescore-team]"):
                team_id = str(score.get("data-livescore-team") or "")
                if team_id.isdigit() and team_id not in team_ids:
                    team_ids.append(team_id)
            unix = None
            tdiv = w.find("div", class_="match-time")
            if tdiv is not None and tdiv.get("data-unix"):
                try:
                    unix = int(tdiv["data-unix"]) / 1000.0
                except (ValueError, TypeError):
                    unix = None
            url = ""
            link = w.find("a", href=lambda h: h and h.startswith("/matches/"))
            if link is not None:
                url = f"https://www.hltv.org{link['href']}"
            # 注意：列表页的直播比分 span 在服务器端 HTML 里是空的（由
            # scorebot websocket 填充），比分要到单场详情页取（见
            # get_live_score）
            entry = {
                "id": mid,
                "live": str(w.get("live")) == "true",
                "rating": stars,
                "team1": names[0] if names else "",
                "team2": names[1] if len(names) > 1 else "",
                "team1_id": team_ids[0] if team_ids else "",
                "team2_id": team_ids[1] if len(team_ids) > 1 else "",
                "event": event,
                "best_of": best_of,
                "unix": unix,
                "url": url,
            }
            # 同一比赛会在"置顶/为你推荐"区和正文各出现一次，且置顶卡片
            # 往往缺赛事名等字段：去重时用后出现的条目回填缺失字段
            if mid in by_id:
                stored = by_id[mid]
                for k, v in entry.items():
                    if not stored.get(k) and v:
                        stored[k] = v
                continue
            by_id[mid] = entry
            items.append(entry)
        return items

    @staticmethod
    def _parse_home_live_matches(page: Any) -> list[dict]:
        """从主页实时卡片读取当前直播，排除同区展示的已结束场次。"""
        active: dict[str, dict] = {}
        for link in page.select("a.hotmatch-box[data-livescore-match]"):
            box = link.select_one(".teambox")
            classes = set(box.get("class") or []) if box else set()
            if (
                box is None
                or str(box.get("filteraslive") or "").lower() != "true"
                or "matchover" in classes
            ):
                continue
            match_id = str(link.get("data-livescore-match") or "")
            if not match_id.isdigit():
                continue
            href = str(link.get("href") or "")
            names = [
                item.get_text(" ", strip=True)
                for item in link.select(".team")
            ]
            try:
                stars = int(box.get("stars") or 0)
            except (TypeError, ValueError):
                stars = 0
            title = str(link.get("title") or "").strip()
            event = title.rsplit(" - ", 1)[-1].strip() if title else ""
            active[match_id] = {
                "id": match_id,
                "live": True,
                "rating": stars,
                "team1": names[0] if names else "",
                "team2": names[1] if len(names) > 1 else "",
                "team1_id": str(box.get("team1") or ""),
                "team2_id": str(box.get("team2") or ""),
                "event": event,
                "best_of": "",
                "unix": None,
                "url": urljoin(_HOME_URL, href) if href else "",
            }

        for table in page.select("table.match-table"):
            table_ids = list(
                dict.fromkeys(
                    str(item.get("data-livescore-team") or "")
                    for item in table.select("[data-livescore-team]")
                    if str(item.get("data-livescore-team") or "").isdigit()
                )
            )
            table_names = [
                item.get_text(" ", strip=True) for item in table.select(".a-default")
            ]
            match_id = next(
                (
                    key
                    for key, item in active.items()
                    if len(table_ids) >= 2
                    and {item.get("team1_id"), item.get("team2_id")}
                    == set(table_ids[:2])
                ),
                "",
            )
            if not match_id and len(table_names) >= 2:
                match_id = next(
                    (
                        key
                        for key, item in active.items()
                        if {item.get("team1"), item.get("team2")}
                        == set(table_names[:2])
                    ),
                    "",
                )
            if match_id not in active:
                continue
            rows = table.select("tr")
            if rows:
                cells = rows[0].select("td")
                if cells:
                    active[match_id]["event"] = cells[0].get_text(" ", strip=True)
                if len(cells) > 1:
                    format_match = re.search(
                        r"\bbo\s*(\d+)\b",
                        cells[1].get_text(" ", strip=True),
                        re.IGNORECASE,
                    )
                    if format_match:
                        active[match_id]["best_of"] = f"BO{format_match.group(1)}"
            if len(table_names) >= 2:
                active[match_id]["team1"] = table_names[0]
                active[match_id]["team2"] = table_names[1]
        return list(active.values())

    async def _get_matches_raw(self) -> list[dict]:
        async def fetch() -> list[dict]:
            page = await self._fetch_raw(_MATCHES_URL)
            return self._parse_matches_page(page)

        ttl = min(float(self._cache_ttl or _LIVE_TTL), _LIVE_TTL)
        return await self._cached_locked(MATCHES_RAW_KEY, fetch, ttl=ttl)

    async def _get_home_live_raw(self) -> list[dict]:
        async def fetch() -> list[dict]:
            page = await self._fetch_raw(_HOME_URL)
            return self._parse_home_live_matches(page)

        ttl = min(float(self._cache_ttl or _LIVE_TTL), _LIVE_TTL)
        return await self._cached_locked(HOME_LIVE_KEY, fetch, ttl=ttl)

    def _matches_view(self, raw: list[dict], days: int, min_stars: int) -> list[dict]:
        """原始比赛 → 展示视图：星级过滤、时间窗口、丢弃纯占位对阵。

        窗口按**滚动 N×24 小时**算而非日历日：CS 的欧洲赛事在北京时间
        普遍零点后开打，23:50 查"今日赛程"时用户要的是接下来这一晚，
        按日历日切会在午夜前后给出违反直觉的空/满结果。

        已过预定开赛时间但仍挂在 upcoming 列表的比赛（延迟开赛很常见，
        HLTV 也可能尚未标记 live）**保留**并打 late 标记——打完的比赛
        会从列表页消失，还挂着就说明没结束，丢掉会让比赛凭空蒸发。
        """
        now = self._now_local()
        now_ts = now.timestamp()
        horizon = now_ts + max(days, 1) * 86400
        out = []
        for m in raw:
            if m.get("rating", 0) < min_stars:
                continue
            if m.get("live"):
                out.append({**m, "date": LIVE, "time": LIVE})
                continue
            if not m.get("team1") and not m.get("team2"):
                continue
            if m.get("unix") is None:
                continue
            if m["unix"] > horizon:
                continue
            dt = datetime.fromtimestamp(m["unix"], now.tzinfo)
            out.append(
                {
                    **m,
                    "date": dt.strftime(_DATE_FMT),
                    "time": dt.strftime("%H:%M"),
                    "late": m["unix"] < now_ts,
                }
            )
        return out

    async def get_matches(self, days: int = 1, min_stars: int = 0) -> list[dict]:
        """近期（含进行中）比赛，days=N 即未来 N×24 小时。"""
        return self._matches_view(await self._get_matches_raw(), days, min_stars)

    async def get_today_matches(self, min_stars: int = 0) -> list[dict]:
        """今日赛程：直播中的 + 未来 24 小时开赛的（按配置时区显示）。"""
        return await self.get_matches(days=1, min_stars=min_stars)

    async def get_live_matches(self, min_stars: int = 0) -> list[dict]:
        try:
            raw = await self._get_home_live_raw()
        except HltvError as e:
            logger.warning(f"[hltv] 主页直播列表不可用，改用 matches 页: {e}")
            raw = await self._get_matches_raw()
        return [
            {**m, "date": LIVE, "time": LIVE}
            for m in raw
            if m.get("live") and m.get("rating", 0) >= min_stars
        ]

    async def get_delayed_matches(self, min_stars: int = 0) -> list[dict]:
        """已过预定开赛时间但 HLTV 尚未标记 live 的比赛（延迟或刚开打）。"""
        return [
            m
            for m in self._matches_view(
                await self._get_matches_raw(), days=1, min_stars=min_stars
            )
            if m.get("late")
        ]

    # ------------------------------------------------------------ 直播比分

    @staticmethod
    def _parse_match_maps(page: Any, *, match_finished: bool) -> list[dict]:
        """读取详情页地图状态；直播中最后一张有数字比分的地图为当前地图。"""
        maps = []
        for holder in page.find_all("div", class_="mapholder"):
            name_el = holder.find(class_="mapname")
            stats_link = holder.select_one("a.results-stats[href*='/mapstatsid/']")
            stats_href = str(stats_link.get("href") or "") if stats_link else ""
            stats_match = re.search(r"/mapstatsid/(\d+)", stats_href)
            scores = [
                e.get_text(strip=True)
                for e in holder.find_all(class_="results-team-score")
            ]
            if len(scores) >= 2:
                started = scores[0].isdigit() and scores[1].isdigit()
                finished = bool(started and (match_finished or stats_link))
                winner = 0
                if finished and scores[0] != scores[1]:
                    winner = 1 if int(scores[0]) > int(scores[1]) else 2
                maps.append(
                    {
                        "map": name_el.get_text(strip=True) if name_el else "?",
                        "s1": scores[0],
                        "s2": scores[1],
                        "played": started,
                        "finished": finished,
                        "winner": winner,
                        "stats_id": stats_match.group(1) if stats_match else "",
                    }
                )

        # played / won 类也用于未开地图和直播领先方，不能表示开始或结束。
        started_indexes = [i for i, item in enumerate(maps) if item.get("played")]
        for i in started_indexes[:-1]:
            item = maps[i]
            s1, s2 = str(item.get("s1") or ""), str(item.get("s2") or "")
            if s1 != s2:
                item["finished"] = True
                item["winner"] = 1 if int(s1) > int(s2) else 2
        return maps

    @staticmethod
    def summarize_map_scores(maps: list[dict]) -> dict:
        """汇总已结束地图和最后一张进行中的地图。"""
        won1 = won2 = 0
        active = None
        active_index = 0
        for index, item in enumerate(maps, start=1):
            if item.get("played"):
                active_index = index
            if item.get("finished"):
                winner = int(item.get("winner") or 0)
                won1 += winner == 1
                won2 += winner == 2
            elif item.get("played"):
                active = item
        current_name = str(active.get("map") or "") if active else ""
        s1, s2 = (
            (str(active.get("s1") or ""), str(active.get("s2") or ""))
            if active
            else ("", "")
        )
        current_score = f"{s1}:{s2}" if s1.isdigit() and s2.isdigit() else ""
        return {
            "maps_score": f"{won1}:{won2}",
            "current_map": (
                f"{current_name} {current_score}"
                if current_name and current_score
                else ""
            ),
            "current_map_name": current_name,
            "current_score": current_score,
            "active_map_index": active_index,
            "map_total": len(maps),
        }

    @staticmethod
    def _parse_scorebot_config(page: Any) -> dict:
        element = page.select_one("#scoreboardElement[data-scorebot-id]")
        if element is None:
            return {}
        config = {
            "list_id": str(element.get("data-scorebot-id") or ""),
            "team1_id": str(element.get("data-team1-id") or ""),
            "team2_id": str(element.get("data-team2-id") or ""),
        }
        if not all(config.values()) or not all(
            config[key].isdigit() for key in ("list_id", "team1_id", "team2_id")
        ):
            return {}
        urls = str(element.get("data-scorebot-url") or "").split(",")
        for value in reversed(urls):
            url = value.strip().rstrip("/")
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.hostname in _SCOREBOT_HOSTS:
                return {"url": url, **config}
        return {}

    @staticmethod
    def _scorebot_config_from_match(match: dict) -> dict:
        """列表页 data-match-id 即 scorebot listId。"""
        config = {
            "list_id": str(match.get("id") or ""),
            "team1_id": str(match.get("team1_id") or ""),
            "team2_id": str(match.get("team2_id") or ""),
        }
        if not all(value.isdigit() for value in config.values()):
            return {}
        return {
            "url": _SCOREBOT_URL,
            **config,
            "referer": str(match.get("url") or _MATCHES_URL),
        }

    @staticmethod
    def _combine_scorebot_events(
        legacy: dict | None, scoreboard: dict | None, list_id: str
    ) -> dict | None:
        if scoreboard is None:
            return legacy
        result = dict(legacy) if legacy is not None else {
            "listId": int(list_id),
            "wins": {},
            "mapScores": {},
        }
        result["_scoreboard"] = scoreboard
        result["_legacy_score_available"] = legacy is not None
        return result

    @staticmethod
    def _recv_scorebot_message(ws: Any, deadline: float) -> str:
        chunks = []
        while time.monotonic() < deadline:
            try:
                chunk, frame = ws.recv_fragment()
            except CurlError as e:
                if CurlECode is not None and e.code == CurlECode.AGAIN:
                    time.sleep(0.05)
                    continue
                raise
            chunks.append(bytes(chunk))
            if frame.bytesleft == 0 and not frame.flags & CurlWsFlag.CONT:
                return b"".join(chunks).decode("utf-8", errors="replace")
        return ""

    @classmethod
    def _fetch_scorebot_sync(
        cls, config: dict, *, proxy: str | None, timeout: int
    ) -> dict | None:
        if CurlSession is None or CurlWsFlag is None:
            raise RuntimeError("curl_cffi 未安装或无法加载，不能连接 HLTV scorebot")
        session = CurlSession(impersonate="chrome")
        ws = None
        endpoint = (
            f"{config['url'].replace('https://', 'wss://', 1)}"
            "/socket.io/?EIO=3&transport=websocket"
        )
        options: dict[str, Any] = {
            "timeout": min(max(timeout, 5), 20),
            "allow_redirects": True,
            "impersonate": "chrome",
        }
        if proxy:
            options["proxy"] = proxy
        referer = str(config.get("referer") or "https://www.hltv.org/")
        headers = {
            "origin": "https://www.hltv.org",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
        }

        try:
            ws = session.ws_connect(
                endpoint,
                headers=headers,
                referer=referer,
                **options,
            )
            legacy_subscription = json.dumps(
                {"token": "", "listIds": [int(config["list_id"])]},
                separators=(",", ":"),
            )
            current_subscription = json.dumps(
                {"token": "", "listId": config["list_id"]},
                separators=(",", ":"),
            )
            legacy = None
            scoreboard = None
            subscribed = False
            deadline = time.monotonic() + options["timeout"]
            while time.monotonic() < deadline:
                incoming = cls._recv_scorebot_message(ws, deadline)
                if not incoming:
                    break
                if incoming.startswith("0{"):
                    ws.send("40", CurlWsFlag.TEXT)
                    continue
                if incoming == "40" and not subscribed:
                    ws.send(
                        "42"
                        + json.dumps(
                            ["readyForScores", legacy_subscription],
                            separators=(",", ":"),
                        ),
                        CurlWsFlag.TEXT,
                    )
                    ws.send(
                        "42"
                        + json.dumps(
                            ["readyForMatch", current_subscription],
                            separators=(",", ":"),
                        ),
                        CurlWsFlag.TEXT,
                    )
                    subscribed = True
                    continue
                if incoming == "2":
                    ws.send("3", CurlWsFlag.TEXT)
                    continue
                if incoming.startswith("42"):
                    try:
                        event = json.loads(incoming[2:])
                    except (TypeError, ValueError):
                        continue
                    if not (
                        isinstance(event, list)
                        and len(event) >= 2
                        and isinstance(event[1], dict)
                    ):
                        continue
                    if (
                        event[0] == "score"
                        and str(event[1].get("listId")) == config["list_id"]
                    ):
                        legacy = event[1]
                    elif event[0] == "scoreboard":
                        scoreboard = event[1]
                if legacy is not None and scoreboard is not None:
                    break
            return cls._combine_scorebot_events(
                legacy, scoreboard, config["list_id"]
            )
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            session.close()

    async def _fetch_scorebot(self, config: dict) -> dict | None:
        routes = [*self._proxy_list, None] if self._proxy_list else [None]
        for proxy in routes:
            try:
                score = await asyncio.to_thread(
                    self._fetch_scorebot_sync,
                    config,
                    proxy=proxy,
                    timeout=self._timeout,
                )
            except Exception as e:
                logger.warning(
                    f"[hltv] scorebot 获取失败（经 {proxy or '直连'}）: {e!r}"
                )
                continue
            if score is not None:
                return score
        return None

    @staticmethod
    def _scorebot_value(values: Any, team_id: str) -> Any:
        if not isinstance(values, dict):
            return None
        if team_id in values:
            return values[team_id]
        try:
            return values.get(int(team_id))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _scorebot_map_name(raw: Any) -> str:
        name = str(raw or "").removeprefix("de_").replace("_", " ")
        return name.title() if name else "?"

    @classmethod
    def _summarize_scoreboard(cls, scoreboard: Any, config: dict) -> dict:
        if not isinstance(scoreboard, dict) or scoreboard.get("live") is False:
            return {}

        def integer(primary: str, fallback: str) -> int | None:
            for key in (primary, fallback):
                value = scoreboard.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
                if isinstance(value, str) and value.isdigit():
                    return int(value)
            return None

        ct_id = str(scoreboard.get("ctTeamId") or "")
        t_id = str(scoreboard.get("tTeamId") or "")
        team1_id, team2_id = config["team1_id"], config["team2_id"]
        if (team1_id, team2_id) == (ct_id, t_id):
            reversed_sides = False
        elif (team1_id, team2_id) == (t_id, ct_id):
            reversed_sides = True
        else:
            return {}

        name = cls._scorebot_map_name(scoreboard.get("mapName"))
        result = {
            "current_map": "",
            "current_map_name": name,
            "current_score": "",
        }
        has_round_state = any(
            key in scoreboard
            for key in ("currentRoundState", "frozen", "roundTimeRemainingMS")
        )
        if has_round_state:
            remaining = integer("roundTimeRemainingMS", "roundTimeRemaining")
            result["round_live"] = (
                str(scoreboard.get("currentRoundState") or "").lower() == "started"
                and scoreboard.get("frozen") is not True
                and remaining is not None
                and remaining > 0
            )

        ct_score = integer("ctTeamScore", "counterTerroristScore")
        t_score = integer("tTeamScore", "terroristScore")
        if ct_score is None or t_score is None:
            return result if has_round_state else {}
        s1, s2 = (t_score, ct_score) if reversed_sides else (ct_score, t_score)
        result["current_score"] = f"{s1}:{s2}"
        result["current_map"] = f"{name} {s1}:{s2}"
        return result

    @classmethod
    def summarize_scorebot(
        cls, score: dict, config: dict, planned_maps: list[dict]
    ) -> dict:
        """按 HLTV scorebot 的 wins / mapScores 生成直播比分。"""
        team1_id, team2_id = config["team1_id"], config["team2_id"]
        entries = []
        for key, value in (score.get("mapScores") or {}).items():
            if not isinstance(value, dict):
                continue
            try:
                ordinal = int(value.get("mapOrdinal") or key)
            except (TypeError, ValueError):
                continue
            entries.append((ordinal, value))
        entries.sort(key=lambda item: item[0])

        maps = []
        for ordinal, value in entries:
            scores = value.get("scores") or {}
            s1 = cls._scorebot_value(scores, team1_id)
            s2 = cls._scorebot_value(scores, team2_id)
            planned_name = (
                str(planned_maps[ordinal - 1].get("map") or "")
                if 0 < ordinal <= len(planned_maps)
                else ""
            )
            name = planned_name or cls._scorebot_map_name(value.get("map"))
            finished = bool(value.get("mapOver"))
            winner = 0
            if finished and isinstance(s1, int) and isinstance(s2, int) and s1 != s2:
                winner = 1 if s1 > s2 else 2
            maps.append(
                {
                    "map": name,
                    "s1": str(s1) if isinstance(s1, int) else "-",
                    "s2": str(s2) if isinstance(s2, int) else "-",
                    "played": True,
                    "finished": finished,
                    "winner": winner,
                    "ordinal": ordinal,
                }
            )

        wins = score.get("wins") or {}
        won1 = cls._scorebot_value(wins, team1_id)
        won2 = cls._scorebot_value(wins, team2_id)
        if not isinstance(won1, int) or not isinstance(won2, int):
            won1 = sum(item["winner"] == 1 for item in maps if item["finished"])
            won2 = sum(item["winner"] == 2 for item in maps if item["finished"])

        current = maps[-1] if maps and not maps[-1]["finished"] else None
        current_name = str(current.get("map") or "") if current else ""
        current_score = (
            f"{current['s1']}:{current['s2']}"
            if current and current["s1"].isdigit() and current["s2"].isdigit()
            else ""
        )
        active_index = maps[-1]["ordinal"] if maps else 0
        summary = {
            "maps_score": (
                f"{won1}:{won2}"
                if score.get("_legacy_score_available", True)
                else ""
            ),
            "current_map": (
                f"{current_name} {current_score}"
                if current_name and current_score
                else ""
            ),
            "current_map_name": current_name,
            "current_score": current_score,
            "active_map_index": active_index,
            "map_total": max(len(planned_maps), active_index),
            "maps": maps,
            "score_source": "scorebot",
        }
        live = cls._summarize_scoreboard(score.get("_scoreboard"), config)
        if not live:
            return summary

        def map_key(value: Any) -> str:
            return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

        live_name = live["current_map_name"]
        live_key = map_key(live_name)
        live_index = next(
            (
                index
                for index, item in enumerate(planned_maps, start=1)
                if map_key(item.get("map")) == live_key
            ),
            0,
        )
        current_item = next(
            (item for item in maps if map_key(item.get("map")) == live_key), None
        )
        if current_item is not None:
            live_index = int(current_item.get("ordinal") or live_index or 1)
        elif not live_index:
            live_index = (
                max((int(item.get("ordinal") or 0) for item in maps), default=0) + 1
                if maps
                else 1
            )
        summary.update(
            {
                **live,
                "active_map_index": live_index,
                "map_total": max(len(planned_maps), len(maps), live_index),
                "score_source": "scoreboard",
            }
        )
        return summary

    @staticmethod
    def _parse_rating_container(container: Any) -> tuple[str, list[dict]]:
        if container is None:
            return "", []
        version_el = container.select_one("table.totalstats .ratingDesc")
        version = version_el.get_text(" ", strip=True) if version_el else ""
        teams = []
        for table in container.find_all("table", class_="totalstats", recursive=False):
            team_el = table.select_one(".header-row .teamName")
            team = team_el.get_text(" ", strip=True) if team_el else "?"
            players = []
            for row in table.find_all("tr")[1:]:
                nick_el = row.select_one(".player-nick")
                rating_el = row.select_one("td.rating")
                if nick_el is None or rating_el is None:
                    continue
                rating = rating_el.get_text(" ", strip=True)
                if rating:
                    nickname = nick_el.get_text(" ", strip=True)
                    player = {"nickname": nickname, "rating": rating}
                    name_el = row.select_one(".statsPlayerName")
                    full_name = name_el.get_text(" ", strip=True) if name_el else ""
                    if full_name and full_name != nickname:
                        player["name"] = full_name
                    for key, selector in (
                        ("kd", "td.kd"),
                        ("swing", "td.roundSwing, td.swing, td.kddiff"),
                        ("adr", "td.adr"),
                        ("kast", "td.kast"),
                    ):
                        cell = row.select_one(selector)
                        value = cell.get_text(" ", strip=True) if cell else ""
                        if value:
                            player[key] = value
                    players.append(player)
            if players:
                teams.append({"team": team, "players": players})
        return version, teams

    @classmethod
    def _parse_match_ratings(cls, page: Any) -> tuple[str, list[dict]]:
        return cls._parse_rating_container(
            page.find(class_="stats-content", id="all-content")
        )

    @classmethod
    def _parse_map_ratings(cls, page: Any, maps: list[dict]) -> list[dict]:
        results = []
        for index, item in enumerate(maps, start=1):
            stats_id = str(item.get("stats_id") or "")
            if not stats_id:
                continue
            container = page.find(class_="stats-content", id=f"{stats_id}-content")
            version, ratings = cls._parse_rating_container(container)
            if not ratings:
                continue
            s1, s2 = str(item.get("s1") or ""), str(item.get("s2") or "")
            results.append(
                {
                    "index": index,
                    "map": str(item.get("map") or "?"),
                    "score": f"{s1}:{s2}" if s1.isdigit() and s2.isdigit() else "",
                    "rating_version": version,
                    "ratings": ratings,
                }
            )
        return results

    @classmethod
    def _parse_match_snapshot(cls, page: Any) -> dict:
        countdown_el = page.select_one(".countdown")
        countdown = countdown_el.get_text(" ", strip=True).lower() if countdown_el else ""
        finished = "match over" in countdown
        maps = cls._parse_match_maps(page, match_finished=finished)
        has_page_score = any(
            item.get("played")
            and str(item.get("s1") or "").isdigit()
            and str(item.get("s2") or "").isdigit()
            for item in maps
        )
        summary = cls.summarize_map_scores(maps) if finished or has_page_score else {
            "maps_score": "",
            "current_map": "",
            "current_map_name": "",
            "current_score": "",
            "active_map_index": 0,
            "map_total": len(maps),
        }

        team1_el = page.select_one(".team1-gradient .teamName")
        team2_el = page.select_one(".team2-gradient .teamName")
        event_el = page.select_one(".timeAndEvent .event")
        format_el = page.select_one(".preformatted-text")
        format_match = re.search(
            r"\bBest\s+of\s+(\d+)\b",
            format_el.get_text(" ", strip=True) if format_el else "",
            re.IGNORECASE,
        )
        version, ratings = cls._parse_match_ratings(page) if finished else ("", [])
        return {
            **summary,
            "status": "finished" if finished else "live",
            "team1": team1_el.get_text(" ", strip=True) if team1_el else "?",
            "team2": team2_el.get_text(" ", strip=True) if team2_el else "?",
            "event": event_el.get_text(" ", strip=True) if event_el else "",
            "best_of": f"BO{format_match.group(1)}" if format_match else "",
            "maps": maps,
            "map_ratings": cls._parse_map_ratings(page, maps),
            "rating_version": version,
            "ratings": ratings,
            "score_source": "page" if finished or has_page_score else "unavailable",
        }

    async def get_match_snapshot(
        self, match_id: Any, url: str, *, watch: bool = False
    ) -> dict:
        if not url:
            raise HltvError("该比赛没有详情页链接。")

        async def fetch() -> dict:
            page = await self._fetch_raw(url)
            snapshot = self._parse_match_snapshot(page)
            if snapshot["status"] != "live":
                return snapshot
            config = self._parse_scorebot_config(page)
            if not config:
                return snapshot
            config["referer"] = url
            score = await self._fetch_scorebot(config)
            if score is None:
                return snapshot
            snapshot.update(
                self.summarize_scorebot(score, config, snapshot.get("maps") or [])
            )
            return snapshot

        ttl = min(float(self._cache_ttl or _LIVE_TTL), 10 if watch else _LIVE_TTL)
        channel = "watch" if watch else "view"
        return await self._cached_locked(
            f"match_snapshot:{channel}:{match_id}", fetch, ttl=ttl
        )

    async def get_live_snapshot(self, match: dict) -> dict:
        """读取已由 /matches 确认为直播中的比赛比分。"""
        if not match.get("live"):
            raise HltvError("该比赛当前不在 HLTV 直播列表中。")

        config = self._scorebot_config_from_match(match)
        if config:
            score = await self._fetch_scorebot(config)
            if score is not None:
                return {
                    "status": "live",
                    "team1": str(match.get("team1") or "?"),
                    "team2": str(match.get("team2") or "?"),
                    "event": str(match.get("event") or ""),
                    "best_of": str(match.get("best_of") or ""),
                    "rating_version": "",
                    "ratings": [],
                    **self.summarize_scorebot(score, config, []),
                }

        return await self.get_match_snapshot(match.get("id"), match.get("url", ""))

    async def get_live_score(self, match_id: Any, url: str) -> list[dict]:
        """兼容旧调用：从统一比赛快照中只返回地图列表。"""
        snapshot = await self.get_match_snapshot(match_id, url)
        return list(snapshot.get("maps") or [])

    # ---------------------------------------------------------------- 排名

    @staticmethod
    def _parse_ranking_page(page: Any, max_teams: int) -> list[dict]:
        """HLTV 自家排名页与 VRS 页共用同一套 .ranked-team 结构。"""
        teams = []
        for i, div in enumerate(page.find_all("div", class_="ranked-team"), start=1):
            if i > max_teams:
                break
            name_el = div.find("span", class_="name")
            pos_el = div.find("span", class_="position")
            pts_el = div.find("span", class_="points")
            points = ""
            if pts_el is not None:
                digits = re.search(r"\d+", pts_el.get_text(" ", strip=True))
                points = digits.group() if digits else ""
            region_el = div.find("span", class_="region")
            tid = ""
            link = div.find("a", href=lambda h: h and h.startswith("/team/"))
            if link is not None:
                parts = str(link.get("href", "")).split("/")
                tid = parts[2] if len(parts) > 2 else ""
            teams.append(
                {
                    "rank": pos_el.get_text(strip=True).lstrip("#") if pos_el else str(i),
                    "title": name_el.get_text(strip=True) if name_el else "?",
                    "points": points,
                    "region": region_el.get_text(strip=True) if region_el else "",
                    "id": tid,
                    "change": "",
                }
            )
        return teams

    async def get_top_teams(self, max_teams: int = 50) -> list[dict]:
        """HLTV 自家世界排名。"""

        async def fetch() -> list[dict]:
            page = await self._fetch_raw(_RANKING_URL)
            teams = self._parse_ranking_page(page, max_teams)
            if not teams:
                raise HltvError("HLTV 排名解析为空（页面可能改版）。")
            return teams

        return await self._cached_locked(f"ranking:hltv:{max_teams}", fetch)

    async def get_vrs_ranking(
        self, region: str | None = None, max_teams: int = 50
    ) -> list[dict]:
        """Valve VRS 排名（默认全球）。region 取 VRS_REGIONS 的值
        （Asia/Europe/Americas）。地区页 URL 带日期，从全球页动态提取。"""

        async def fetch() -> list[dict]:
            page = await self._fetch_raw(_VRS_URL)
            if region:
                link = page.find("a", href=lambda h: h and f"/region/{region}" in h)
                if link is None:
                    raise HltvError(
                        f"VRS 页面上没找到 {region} 地区入口（HLTV 可能改版）。"
                    )
                href = str(link.get("href"))
                url = href if href.startswith("http") else f"https://www.hltv.org{href}"
                page = await self._fetch_raw(url)
            teams = self._parse_ranking_page(page, max_teams)
            if not teams:
                raise HltvError("VRS 排名解析为空（页面可能改版）。")
            return teams

        return await self._cached_locked(vrs_key(region), fetch)

    # ---------------------------------------------------------------- 战队

    async def find_team(self, name: str) -> dict:
        """查战队：先在 HLTV 排名 Top50 内模糊匹配，找不到走站内搜索。"""
        needle = self._normalize_name(name)
        try:
            teams = await self.get_top_teams(50)
        except HltvError:
            teams = []
        candidates = [
            t
            for t in teams
            if needle
            and (
                needle in self._normalize_name(t.get("title", ""))
                or self._normalize_name(t.get("title", "")) in needle
            )
        ]
        matched = (
            min(
                candidates,
                key=lambda item: self._name_match_score(
                    needle, item.get("title") or ""
                ),
            )
            if candidates
            else None
        )
        if matched and matched.get("id"):
            team_id, title = matched["id"], matched.get("title") or name
        else:
            found = [
                item
                for item in ((await self._search(name)).get("teams") or [])
                if isinstance(item, dict) and "id" in item
            ]
            if not found:
                raise HltvError(f"没有找到战队「{name}」。")
            best = min(
                found,
                key=lambda item: self._name_match_score(
                    needle, item.get("name") or item.get("title") or ""
                ),
            )
            team_id = best["id"]
            title = best.get("name") or best.get("title") or name
        return await self.get_team_details(team_id, str(title))

    @staticmethod
    def _normalize_name(value: Any) -> str:
        return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", str(value).lower())

    @classmethod
    def _name_match_score(cls, needle: str, candidate: Any) -> tuple[int, int]:
        normalized = cls._normalize_name(candidate)
        if normalized == needle:
            return (0, len(normalized))
        if normalized.startswith(needle):
            return (1, len(normalized))
        if needle in normalized:
            return (2, len(normalized))
        return (3, len(normalized))

    async def get_team_details(self, team_id: Any, title: str) -> dict:
        async def fetch() -> dict:
            slug = title.replace(" ", "-").lower()
            page = await self._fetch_raw(
                f"https://www.hltv.org/team/{team_id}/{quote(slug)}"
            )
            return self._parse_team_page(page, title, self._tzinfo())

        return await self._cached_locked(f"team:{team_id}", fetch)

    @staticmethod
    def _parse_team_page(page: Any, title: str, tzinfo: Any = None) -> dict:
        """team 页按【标签】解析统计项。HLTV 新增了 "Valve ranking" 行，
        按位置取值会整体错位（这正是库 0.8.3 把 Top30 周数当年龄的原因）。"""
        info: dict[str, Any] = {
            "title": title,
            "valve_rank": "",
            "world_rank": "",
            "weeks_top30": "",
            "age": "",
            "coach": "",
            "players": [],
            "trophies": [],
            "recent": [],
        }
        h1 = page.find("h1", class_="profile-team-name") or page.find("h1")
        if h1 is not None:
            info["title"] = h1.get_text(strip=True) or title

        for stat in page.find_all("div", class_="profile-team-stat"):
            b = stat.find("b")
            if b is None:
                continue
            label = b.get_text(" ", strip=True).lower()
            value_el = stat.find("span", class_="right") or stat.find("a")
            value = value_el.get_text(" ", strip=True) if value_el else ""
            if "valve" in label:
                info["valve_rank"] = value.lstrip("#")
            elif "world" in label:
                info["world_rank"] = value.lstrip("#")
            elif "weeks" in label:
                info["weeks_top30"] = value
            elif "age" in label:
                info["age"] = value
            elif "coach" in label:
                a = stat.find("a")
                info["coach"] = a.get_text(" ", strip=True) if a else value

        box = page.find("div", class_="bodyshot-team")
        if box is not None:
            for a in box.find_all("a", href=True):
                nm = a.find("span", class_="text-ellipsis bold")
                pname = nm.get_text(strip=True) if nm else str(a.get("title") or "").strip()
                if not pname:
                    continue
                iso = ""
                flag = a.find("img", class_="flag")
                if flag is not None and flag.get("src"):
                    m = re.search(r"/flags/[^/]+/([A-Za-z]{2})\.", str(flag["src"]))
                    iso = m.group(1) if m else ""
                info["players"].append({"name": pname, "cc": iso})

        for holder in page.find_all("div", class_="trophyHolder"):
            # 实测标记：title 在 span.trophyDescription 上，img 无 title/alt
            tname = ""
            titled = holder.find(attrs={"title": True})
            if titled is not None:
                tname = str(titled.get("title") or "").strip()
            if not tname:
                img = holder.find("img")
                if img is not None:
                    tname = str(img.get("title") or img.get("alt") or "").strip()
            if tname:
                info["trophies"].append(tname)

        table = page.find(id="matchesBox")
        if table is not None:
            for row in table.find_all("tr", class_="team-row")[:5]:
                try:
                    d = ""
                    date_el = row.find("span", attrs={"data-unix": True})
                    if date_el is not None:
                        d = datetime.fromtimestamp(
                            int(date_el["data-unix"]) / 1000.0, tzinfo
                        ).strftime("%m-%d")
                    names = [
                        a.get_text(strip=True)
                        for a in row.find_all("a", class_="team-name")
                    ]
                    opp = next(
                        (
                            n
                            for n in names
                            if n and n.lower() != str(info["title"]).lower()
                        ),
                        "?",
                    )
                    scores = [
                        s.get_text(strip=True)
                        for s in row.find_all("span", class_="score")
                    ]
                    # 实测原始 HTML：败方 flex 带 'lost' 类，胜方【没有】
                    # 'won' 类（浏览器渲染后才有），故用无 lost 判定获胜。
                    # 第一个 team-flex 恒为本队列。
                    flex = row.find("div", class_="team-flex")
                    won = bool(
                        flex is not None and "lost" not in (flex.get("class") or [])
                    )
                    info["recent"].append(
                        {
                            "date": d,
                            "opp": opp,
                            "score": "-".join(scores[:2]) if len(scores) >= 2 else "",
                            "won": won,
                        }
                    )
                except Exception:
                    continue
        return info

    # ---------------------------------------------------------------- 选手

    @staticmethod
    def _parse_player_page(page: Any, player_id: Any, nickname: str) -> dict:
        """解析选手资料，并按 HLTV 当前 trophyHolder 结构拆分各类荣誉。"""
        info: dict[str, Any] = {
            "id": player_id,
            "nickname": nickname,
            "name": "",
            "team": "",
            "team_id": "",
            "nationality": "",
            "age": "",
            "rating": "",
            "rating_label": "Rating",
            "top20": [],
            "major_wins": 0,
            "major_mvps": 0,
            "championships": [],
            "career_awards": [],
            "total_trophies": 0,
            "total_mvps": 0,
            "mvp_events": [],
            "mvp_icon_url": "",
        }

        nick_el = page.find("h1", class_="playerNickname")
        if nick_el is not None:
            info["nickname"] = nick_el.get_text(strip=True) or nickname

        real_el = page.find("div", class_="playerRealname")
        if real_el is not None:
            info["name"] = real_el.get_text(" ", strip=True)
            flag = real_el.find("img")
            if flag is not None:
                info["nationality"] = str(
                    flag.get("title") or flag.get("alt") or ""
                ).strip()

        team_el = page.select_one(".playerTeam a[href^='/team/']")
        if team_el is not None:
            info["team"] = team_el.get_text(" ", strip=True)
            parts = str(team_el.get("href") or "").split("/")
            info["team_id"] = parts[2] if len(parts) > 2 else ""

        age_el = page.select_one(".playerAge .listRight")
        if age_el is not None:
            age_match = re.search(r"\d+", age_el.get_text(" ", strip=True))
            info["age"] = age_match.group() if age_match else ""

        for stat in page.select(".playerpage-container .player-stat"):
            label_el = stat.find("b")
            label = label_el.get_text(" ", strip=True) if label_el else ""
            if not label.lower().startswith("rating"):
                continue
            value_el = stat.select_one(".statsVal p") or stat.select_one(".statsVal")
            info["rating_label"] = label or "Rating"
            info["rating"] = value_el.get_text(" ", strip=True) if value_el else ""
            break

        top20_by_year: dict[int, dict[str, Any]] = {}
        for rank_el in page.select(".playerTop20 .top20ListRight a"):
            rank_match = re.search(r"#(\d+)", rank_el.get_text(" ", strip=True))
            year_el = rank_el.find_next_sibling("span", class_="top-20-year")
            year_match = (
                re.search(r"\d{2,4}", year_el.get_text(" ", strip=True))
                if year_el is not None
                else None
            )
            if year_match is None:
                year_match = re.search(
                    r"top-20-players-of-(\d{4})", str(rank_el.get("href") or "")
                )
            if not rank_match or year_match is None:
                continue
            year = int(year_match.group())
            if year < 100:
                year += 2000
            rank = int(rank_match.group(1))
            top20_by_year[year] = {"year": year, "rank": rank}

        def count(selector: str) -> int:
            el = page.select_one(selector)
            match = re.search(r"\d+", el.get_text(" ", strip=True)) if el else None
            return int(match.group()) if match else 0

        info["major_wins"] = count(".majorSection .majorWinner")
        info["major_mvps"] = count(".majorSection .majorMVP")
        info["total_mvps"] = count(".trophySection .mvp-count")

        for holder in page.select(".trophySection .trophyHolder"):
            desc = holder.select_one(".trophyDescription")
            if desc is None:
                continue
            title = str(desc.get("title") or "").strip()
            image = holder.select_one("img[src]")
            icon_url = HltvClient._asset_url(image.get("src") if image else "")

            mvp_count = holder.select_one(".mvp-count")
            if mvp_count is not None:
                lines = [line.strip() for line in title.splitlines() if line.strip()]
                info["mvp_events"] = (
                    lines[1:]
                    if lines and lines[0].lower().startswith("mvp")
                    else lines
                )
                info["mvp_icon_url"] = icon_url
                continue

            top_match = re.search(
                r"#(\d+)\s+best player in\s+(\d{2,4})", title, re.IGNORECASE
            )
            if top_match:
                rank, year = int(top_match.group(1)), int(top_match.group(2))
                if year < 100:
                    year += 2000
                item = top20_by_year.setdefault(year, {"year": year, "rank": rank})
                item["rank"] = rank
                if icon_url:
                    item["icon_url"] = icon_url
                continue

            link = holder.find_parent(
                "a", href=lambda value: value and value.startswith("/events/")
            )
            if link is not None and title:
                info["championships"].append(
                    {
                        "name": title,
                        "major": "majorTrophy" in (desc.get("class") or []),
                        "icon_url": icon_url,
                    }
                )
                continue

            if title and "/img/static/award/" in icon_url:
                year_el = holder.select_one(".award-year")
                info["career_awards"].append(
                    {
                        "name": title,
                        "year": (
                            year_el.get_text(" ", strip=True).lstrip("'")
                            if year_el
                            else ""
                        ),
                        "icon_url": icon_url,
                    }
                )

        for year, item in top20_by_year.items():
            item.setdefault(
                "icon_url",
                f"https://www.hltv.org/img/static/event/trophies/{year}/{item['rank']}.png",
            )
        info["top20"] = [top20_by_year[year] for year in sorted(top20_by_year)]
        if not info["total_mvps"]:
            info["total_mvps"] = len(info["mvp_events"])
        info["total_trophies"] = len(info["championships"])
        return info

    @staticmethod
    def _asset_url(src: Any) -> str:
        value = str(src or "").strip()
        return urljoin("https://www.hltv.org/", value) if value else ""

    async def prepare_player_assets(self, player: dict) -> dict:
        """Best-effort cache of the official TOP/MVP icons used by the card."""
        references: list[tuple[dict, str]] = [
            (item, "icon_path")
            for item in player.get("top20") or []
            if item.get("icon_url")
        ]
        if player.get("mvp_icon_url"):
            references.append((player, "mvp_icon_path"))
        if not references:
            return player

        cache_dir = Path.home() / ".astrbot_plugin_hltv" / "media"
        cache_dir.mkdir(parents=True, exist_ok=True)
        timeout = aiohttp.ClientTimeout(total=min(self._timeout, 6))
        proxy = self._proxy_list[0] if self._proxy_list else None

        async def download(
            session: aiohttp.ClientSession, target: dict, path_key: str
        ) -> None:
            url_key = "mvp_icon_url" if path_key == "mvp_icon_path" else "icon_url"
            url = str(target.get(url_key) or "")
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in {
                "www.hltv.org",
                "img-cdn.hltv.org",
            }:
                return
            suffix = Path(parsed.path).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                suffix = ".png"
            destination = cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}{suffix}"
            if destination.is_file() and destination.stat().st_size > 0:
                target[path_key] = str(destination)
                return
            try:
                async with session.get(url, proxy=proxy) as response:
                    if response.status != 200:
                        return
                    body = await response.read()
                is_image = body.startswith((b"\x89PNG", b"\xff\xd8\xff")) or (
                    body.startswith(b"RIFF") and body[8:12] == b"WEBP"
                )
                if not body or len(body) > 2 * 1024 * 1024 or not is_image:
                    return
                destination.write_bytes(body)
                target[path_key] = str(destination)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                return

        headers = {
            **_BROWSER_HEADERS,
            "accept": "image/avif,image/webp,image/png,image/*",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            await asyncio.gather(
                *(download(session, target, path_key) for target, path_key in references)
            )
        return player

    async def get_player_details(self, player_id: Any, nickname: str) -> dict:
        async def fetch() -> dict:
            slug = quote(nickname.replace(" ", "-").lower())
            page = await self._fetch_raw(
                f"https://www.hltv.org/player/{player_id}/{slug}"
            )
            return self._parse_player_page(page, player_id, nickname)

        return await self._cached_locked(f"player:{player_id}", fetch)

    async def find_player(self, nickname: str) -> dict:
        """查选手：站内搜索优先，被风控时退回选手榜（Top 100）匹配。"""
        needle = nickname.strip().lower()
        pid = nick = None
        try:
            found = (await self._search(nickname)).get("players") or []
            if found and isinstance(found[0], dict) and "id" in found[0]:
                pid = found[0]["id"]
                # 实测搜索接口的选手键是驼峰 nickName
                nick = (
                    found[0].get("nickName")
                    or found[0].get("nickname")
                    or found[0].get("name")
                    or nickname
                )
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
                (p for p in players if needle in str(p.get("nickname", "")).lower()),
                None,
            )
            if not matched:
                raise HltvError(f"没有找到选手「{nickname}」。")
            pid, nick = matched["id"], matched["nickname"]
        return await self.get_player_details(pid, str(nick))

    # ---------------------------------------------------------------- 赛果/赛事

    async def get_results(self, days: int = 1, min_stars: int = 0) -> list[dict]:
        """近期赛果（库解析实测健在）。featured=False：精选 box 无日期、
        超窗口且与按日列表重复。"""
        return await self._call(
            "get_results",
            days=days,
            min_rating=min_stars,
            featured=False,
            cache_key=results_key(days, min_stars),
        )

    async def get_events(self) -> list[dict]:
        return await self._call("get_events", cache_key=EVENTS_KEY)

    # ---------------------------------------------------------------- 年度 TOP20

    def latest_top20_year(self) -> int:
        return self._now_local().year - 1

    @staticmethod
    def _parse_top20_article_url(page: Any, year: int) -> str:
        exact = re.compile(
            rf"top-20-players-of-{year}-(?:final-list|final-ranking)",
            re.IGNORECASE,
        )
        recap = re.compile(
            rf"/news/\d+/video-top-20-players-of-{year}/?$",
            re.IGNORECASE,
        )
        fallback = re.compile(
            rf"top\s*20\s+players\s+of\s+{year}.*final\s+(?:list|ranking)",
            re.IGNORECASE,
        )
        links = list(page.find_all("a", href=True))
        for link in links:
            href = str(link.get("href") or "").strip()
            if exact.search(href):
                url = urljoin("https://www.hltv.org/", href)
                parsed = urlparse(url)
                if parsed.scheme == "https" and parsed.hostname == "www.hltv.org":
                    return url
        for link in links:
            href = str(link.get("href") or "").strip()
            if recap.search(href):
                return urljoin("https://www.hltv.org/", href)
        for link in links:
            href = str(link.get("href") or "").strip()
            text = link.get_text(" ", strip=True)
            if fallback.search(f"{text} {href.replace('-', ' ')}"):
                url = urljoin("https://www.hltv.org/", href)
                parsed = urlparse(url)
                if parsed.scheme == "https" and parsed.hostname == "www.hltv.org":
                    return url
        return ""

    @staticmethod
    def _parse_top20_image_url(page: Any, year: int) -> str:
        def trusted_url(value: Any) -> str:
            source = str(value or "").strip()
            if source.startswith("http://pbs.twimg.com/"):
                source = "https://" + source.removeprefix("http://")
            url = urljoin("https://www.hltv.org/", source)
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.hostname in _TOP20_IMAGE_HOSTS:
                return url
            return ""

        candidates: list[tuple[int, int, str]] = []
        for index, image in enumerate(page.find_all("img")):
            source = (
                image.get("src")
                or image.get("data-src")
                or image.get("data-original")
            )
            if not source and image.get("srcset"):
                source = str(image.get("srcset")).split(",", 1)[0].strip().split(" ", 1)[0]
            parent_link = image.find_parent("a", href=True)
            if parent_link is not None:
                linked_url = trusted_url(parent_link.get("href"))
                if Path(urlparse(linked_url).path).suffix.lower() in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".webp",
                    ".gif",
                }:
                    source = linked_url
            url = trusted_url(source)
            if not url:
                continue
            image_host = urlparse(url).hostname
            context = " ".join(
                str(value or "")
                for value in (
                    url,
                    image.get("alt"),
                    image.get("title"),
                    " ".join(image.get("class") or []),
                )
            ).lower()
            score = 0
            if str(year) in context:
                score += 6
            if any(
                token in context
                for token in ("top20", "top-20", "top_20", "top 20")
            ):
                score += 6
            if any(token in context for token in ("final", "ranking", "players")):
                score += 2
            if image_host == "pbs.twimg.com":
                score += 5
            if image.find_parent(
                "blockquote", class_=re.compile("twitter", re.IGNORECASE)
            ):
                score += 5
            if image.find_parent("figure") is not None:
                score += 4
            elif image.find_parent("article") is not None or image.find_parent(
                class_=re.compile(r"news", re.IGNORECASE)
            ) is not None:
                score += 2
            if any(token in context for token in ("flag", "avatar", "logo", "advert")):
                score -= 8
            candidates.append((score, -index, url))

        meta = page.find("meta", attrs={"property": "og:image"})
        if meta is not None:
            url = trusted_url(meta.get("content"))
            if url:
                context = url.lower()
                score = 3
                if str(year) in context:
                    score += 6
                if any(token in context for token in ("top20", "top-20", "top_20")):
                    score += 6
                candidates.append((score, 0, url))

        best = max(candidates, default=(0, 0, ""))
        return best[2] if best[0] > 0 else ""

    @staticmethod
    def _parse_top20_players(pages: list[Any], year: int) -> list[dict]:
        pattern = re.compile(
            rf"/news/\d+/top-20-players-of-{year}-(.+)-(\d+)$",
            re.IGNORECASE,
        )
        title_pattern = re.compile(
            rf"top\s*20\s+players\s+of\s+{year}\s*:\s*(.+?)\s*\((\d+)\)(?:\s|$)",
            re.IGNORECASE,
        )
        players: dict[int, dict] = {}
        for page in pages:
            if page is None:
                continue
            for link in page.find_all("a", href=True):
                href = str(link.get("href") or "").strip()
                path = urlparse(
                    urljoin("https://www.hltv.org/", href)
                ).path.rstrip("/")
                match = pattern.search(path)
                if not match:
                    continue
                rank = int(match.group(2))
                if not 1 <= rank <= 20:
                    continue
                text = link.get_text(" ", strip=True)
                title_match = title_pattern.search(text)
                name = (
                    title_match.group(1).strip()
                    if title_match and int(title_match.group(2)) == rank
                    else match.group(1).replace("-", " ").strip()
                )
                if name:
                    flag = link.find("img", class_="flag")
                    country = (
                        str(flag.get("title") or flag.get("alt") or "").strip()
                        if flag is not None
                        else ""
                    )
                    if title_match or rank not in players:
                        players[rank] = {
                            "rank": rank,
                            "name": name,
                            "country": country,
                            "url": f"https://www.hltv.org{path}",
                        }
        return [players[rank] for rank in sorted(players)]

    @staticmethod
    def _parse_top20_player_image_url(page: Any) -> str:
        allowed_hosts = {"www.hltv.org", "img-cdn.hltv.org"}
        for image in page.select(
            "article.newsitem .newstext-con .image-con img.image"
        ):
            source = image.get("src") or image.get("data-src")
            url = urljoin("https://www.hltv.org/", str(source or "").strip())
            parsed = urlparse(url)
            if (
                parsed.scheme == "https"
                and parsed.hostname in allowed_hosts
                and Path(parsed.path).suffix.lower()
                in {".png", ".jpg", ".jpeg", ".webp"}
            ):
                return url
        return ""

    @staticmethod
    def _download_top20_image_browser(
        url: str,
        *,
        referer: str,
        proxy: str | None,
        timeout: int,
    ) -> bytes:
        if CurlSession is None:
            raise HltvError("浏览器指纹下载组件未安装。")

        image_host = urlparse(url).hostname
        if image_host not in _TOP20_IMAGE_HOSTS:
            raise HltvError("TOP20 图片地址无效。")
        referer_url = urlparse(referer)
        if image_host in _FIVEE_IMAGE_HOSTS:
            safe_referer = (
                referer
                if referer_url.scheme == "https"
                and referer_url.hostname in _FIVEE_ARTICLE_HOSTS
                else "https://csgo.5eplay.com/"
            )
        else:
            safe_referer = (
                referer
                if referer_url.scheme == "https"
                and referer_url.hostname == "www.hltv.org"
                else "https://www.hltv.org/"
            )
        request_options: dict[str, Any] = {
            "timeout": timeout,
            "allow_redirects": True,
        }
        if proxy:
            request_options["proxy"] = proxy

        browser_session = None
        try:
            browser_session = CurlSession(impersonate="chrome")
            response = browser_session.get(
                url,
                headers={
                    "accept": "image/avif,image/webp,image/png,image/*;q=0.8"
                },
                referer=safe_referer,
                **request_options,
            )
            status = int(response.status_code)
            final_url = str(response.url)
            body = bytes(response.content)
        except HltvError:
            raise
        except Exception as e:
            raise HltvError(
                "HLTV TOP20 图片下载失败，请稍后重试或配置代理。"
            ) from e
        finally:
            if browser_session is not None:
                try:
                    browser_session.close()
                except Exception:
                    pass

        if urlparse(final_url).hostname not in _TOP20_IMAGE_HOSTS:
            raise HltvError("TOP20 图片跳转到了非可信地址。")
        if status != 200:
            raise HltvError(f"TOP20 图片下载失败（HTTP {status}）。")
        return body

    async def _download_top20_image(
        self,
        url: str,
        year: int,
        referer: str = "https://www.hltv.org/",
        *,
        session: aiohttp.ClientSession | None = None,
        proxy: str | None = None,
    ) -> Path:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in _TOP20_IMAGE_HOSTS:
            raise HltvError("TOP20 图片地址无效。")

        cache_dir = Path.home() / ".astrbot_plugin_hltv" / "top20"
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        stem = f"top20_{year}_{digest}"
        for suffix in (".png", ".jpg", ".webp", ".gif"):
            cached = cache_dir / f"{stem}{suffix}"
            if cached.is_file() and cached.stat().st_size > 0:
                try:
                    with Image.open(cached) as image:
                        image.load()
                    return cached
                except (OSError, UnidentifiedImageError):
                    cached.unlink(missing_ok=True)

        timeout_seconds = max(15, min(self._timeout, 30))
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        selected_proxy = proxy
        if session is None and selected_proxy is None and self._proxy_list:
            selected_proxy = self._proxy_list[0]
        headers = {
            **_BROWSER_HEADERS,
            "referer": referer,
            "accept": "image/webp,image/png,image/jpeg,image/*;q=0.8",
        }

        async def request(active_session: aiohttp.ClientSession) -> bytes:
            async with active_session.get(
                url,
                proxy=selected_proxy,
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.url.host not in _TOP20_IMAGE_HOSTS:
                    raise HltvError("TOP20 图片跳转到了非可信地址。")
                if response.status != 200:
                    raise HltvError(
                        f"TOP20 图片下载失败（HTTP {response.status}）。"
                    )
                return await response.content.read(12 * 1024 * 1024 + 1)

        if (
            parsed.hostname
            in {"img-cdn.hltv.org", "pbs.twimg.com"} | _FIVEE_IMAGE_HOSTS
            and CurlSession is not None
        ):
            body = await asyncio.to_thread(
                self._download_top20_image_browser,
                url,
                referer=referer,
                proxy=selected_proxy,
                timeout=max(30, timeout_seconds),
            )
        else:
            try:
                if session is not None:
                    body = await request(session)
                else:
                    async with aiohttp.ClientSession() as owned_session:
                        body = await request(owned_session)
            except HltvError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                raise HltvError(
                    "HLTV TOP20 图片下载失败，请稍后重试或配置代理。"
                ) from e

        if body.startswith(b"\x89PNG"):
            suffix = ".png"
        elif body.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif body.startswith(b"RIFF") and body[8:12] == b"WEBP":
            suffix = ".webp"
        elif body.startswith((b"GIF87a", b"GIF89a")):
            suffix = ".gif"
        else:
            raise HltvError("HLTV TOP20 返回的内容不是可识别的图片。")
        if len(body) > 12 * 1024 * 1024:
            raise HltvError("TOP20 图片超过 12MB，已停止下载。")
        try:
            with Image.open(BytesIO(body)) as image:
                image.load()
        except (OSError, UnidentifiedImageError) as e:
            raise HltvError("TOP20 图片内容损坏或无法解码。") from e

        destination = cache_dir / f"{stem}{suffix}"
        destination.write_bytes(body)
        return destination

    async def get_top20(self, year: int) -> dict:
        async def fetch() -> dict:
            fivee_poster = _FIVEE_TOP20_POSTERS.get(year)
            if fivee_poster:
                article_url, image_url = fivee_poster
                try:
                    image_path = await self._download_top20_image(
                        image_url,
                        year,
                        referer=article_url,
                    )
                    return {
                        "year": year,
                        "image_path": image_path,
                        "players": [],
                    }
                except HltvError as e:
                    logger.warning(
                        f"[hltv] 5E {year} 年 TOP20 总榜不可用，改用 HLTV: {e}"
                    )

            if Hltv is None:
                raise HltvError(
                    "依赖 hltv-async-api 未安装。请在 WebUI 插件管理中安装依赖后重载插件。"
                )

            async def fetch_page(hltv: Any, url: str) -> Any:
                fetcher = getattr(hltv, "_fetch", None)
                if fetcher is None:
                    raise HltvError(
                        "hltv-async-api 版本不兼容（缺少 _fetch），请安装 0.8.x 版本。"
                    )
                page = await fetcher(url)
                if page is None:
                    raise HltvError("HLTV 未返回数据（可能被风控拦截）。")
                return page

            try:
                async with Hltv(**self._hltv_opts) as hltv:
                    january = await fetch_page(
                        hltv,
                        f"https://www.hltv.org/news/archive/{year + 1}/january",
                    )
                    december = None
                    article_url = self._parse_top20_article_url(january, year)
                    if not article_url:
                        december = await fetch_page(
                            hltv,
                            f"https://www.hltv.org/news/archive/{year}/december",
                        )
                        article_url = self._parse_top20_article_url(december, year)
                    if not article_url:
                        players = self._parse_top20_players(
                            [january, december], year
                        )
                        if len(players) == 20:
                            return {
                                "year": year,
                                "image_path": None,
                                "players": players,
                            }
                        raise HltvError(f"没有找到完整的 HLTV {year} 年 TOP20 榜单。")

                    article = await fetch_page(hltv, article_url)
                    image_url = self._parse_top20_image_url(article, year)
                    download_error = None
                    if image_url:
                        active_proxy = None
                        if getattr(hltv, "USE_PROXY", False):
                            get_proxy = getattr(hltv, "_get_proxy", None)
                            active_proxy = get_proxy() if callable(get_proxy) else None
                        try:
                            image_path = await self._download_top20_image(
                                image_url,
                                year,
                                referer=article_url,
                                session=getattr(hltv, "session", None),
                                proxy=active_proxy,
                            )
                            return {
                                "year": year,
                                "image_path": image_path,
                                "players": [],
                            }
                        except HltvError as e:
                            download_error = e
                            logger.warning(
                                f"[hltv] {year} 年 TOP20 官方图不可用，改用榜单渲染: {e}"
                            )

                    if december is None:
                        try:
                            december = await fetch_page(
                                hltv,
                                f"https://www.hltv.org/news/archive/{year}/december",
                            )
                        except HltvError as e:
                            logger.warning(f"[hltv] TOP20 十二月归档读取失败: {e}")
                    players = self._parse_top20_players(
                        [january, december, article], year
                    )
                    if len(players) == 20:
                        return {"year": year, "image_path": None, "players": players}
                    if players:
                        logger.warning(
                            f"[hltv] {year} 年 TOP20 榜单不完整: {len(players)}/20"
                        )
                    if download_error is not None:
                        raise download_error
                    raise HltvError(f"没有找到完整的 HLTV {year} 年 TOP20 榜单。")
            except HltvError:
                raise
            except Exception as e:
                logger.error(f"[hltv] 获取 {year} 年 TOP20 失败: {e!r}")
                raise HltvError(
                    "请求 HLTV 失败，可能是网络问题或触发了 Cloudflare 风控，"
                    "可稍后重试或在插件配置中设置代理。"
                ) from e

        return await self._cached_locked(f"top20:{year}", fetch, ttl=86400)

    async def get_top20_image(self, year: int) -> Path:
        result = await self.get_top20(year)
        image_path = result.get("image_path")
        if image_path:
            return Path(image_path)
        raise HltvError(f"HLTV {year} 年 TOP20 官方总图当前不可用。")

    async def get_top20_player(self, year: int, rank: int) -> dict:
        if not 1 <= rank <= 20:
            raise HltvError("TOP20 名次范围为 1-20。")

        async def fetch() -> dict:
            if Hltv is None:
                raise HltvError(
                    "依赖 hltv-async-api 未安装。请在 WebUI 插件管理中安装依赖后重载插件。"
                )

            async def fetch_page(hltv: Any, url: str) -> Any:
                fetcher = getattr(hltv, "_fetch", None)
                if fetcher is None:
                    raise HltvError(
                        "hltv-async-api 版本不兼容（缺少 _fetch），请安装 0.8.x 版本。"
                    )
                page = await fetcher(url)
                if page is None:
                    raise HltvError("HLTV 未返回数据（可能被风控拦截）。")
                return page

            try:
                async with Hltv(**self._hltv_opts) as hltv:
                    january = await fetch_page(
                        hltv,
                        f"https://www.hltv.org/news/archive/{year + 1}/january",
                    )
                    players = self._parse_top20_players([january], year)
                    target = next(
                        (player for player in players if player["rank"] == rank),
                        None,
                    )
                    if target is None:
                        december = await fetch_page(
                            hltv,
                            f"https://www.hltv.org/news/archive/{year}/december",
                        )
                        players = self._parse_top20_players(
                            [january, december], year
                        )
                        target = next(
                            (player for player in players if player["rank"] == rank),
                            None,
                        )
                    if target is None:
                        raise HltvError(
                            f"没有找到 HLTV {year} 年 TOP20 第 {rank} 名的新闻。"
                        )

                    article_url = str(target["url"])
                    article = await fetch_page(hltv, article_url)
                    headline = article.select_one("h1.headline") or article.find("h1")
                    title = (
                        headline.get_text(" ", strip=True)
                        if headline is not None
                        else f"Top 20 players of {year}: {target['name']} ({rank})"
                    )
                    description_meta = article.find(
                        "meta", attrs={"property": "og:description"}
                    )
                    description = (
                        str(description_meta.get("content") or "").strip()
                        if description_meta is not None
                        else ""
                    )
                    image_path = None
                    image_url = self._parse_top20_player_image_url(article)
                    if image_url:
                        active_proxy = None
                        if getattr(hltv, "USE_PROXY", False):
                            get_proxy = getattr(hltv, "_get_proxy", None)
                            active_proxy = get_proxy() if callable(get_proxy) else None
                        try:
                            image_path = await self._download_top20_image(
                                image_url,
                                year,
                                referer=article_url,
                                session=getattr(hltv, "session", None),
                                proxy=active_proxy,
                            )
                        except HltvError as e:
                            logger.warning(
                                f"[hltv] {year} 年 TOP20 第 {rank} 名官方图不可用，"
                                f"改用本地渲染: {e}"
                            )
                    return {
                        "year": year,
                        "rank": rank,
                        "name": target["name"],
                        "title": title,
                        "description": description,
                        "url": article_url,
                        "image_path": image_path,
                    }
            except HltvError:
                raise
            except Exception as e:
                logger.error(
                    f"[hltv] 获取 {year} 年 TOP20 第 {rank} 名失败: {e!r}"
                )
                raise HltvError(
                    "请求 HLTV 失败，可能是网络问题或触发了 Cloudflare 风控，"
                    "可稍后重试或在插件配置中设置代理。"
                ) from e

        return await self._cached_locked(
            f"top20:player:{year}:{rank}", fetch, ttl=86400
        )

    # ---------------------------------------------------------------- 新闻

    async def get_news(self) -> list[dict]:
        """今日新闻（精确读取 Today's news，不混入赛事专题或昨日新闻）。
        条目：{'title','url','desc','posted','featured'}"""

        async def fetch() -> list[dict]:
            page = await self._fetch_raw(_HOME_URL)
            return self._parse_homepage_news(page)

        return await self._cached_locked(NEWS_KEY, fetch)

    @staticmethod
    def _parse_homepage_news(page: Any) -> list[dict]:
        boxes = page.select("div.standard-box.standard-list")
        box = next(
            (
                candidate
                for candidate in boxes
                if candidate.find_previous_sibling() is not None
                and candidate.find_previous_sibling()
                .get_text(" ", strip=True)
                .replace("’", "'")
                .casefold()
                == "today's news"
            ),
            boxes[0] if boxes else None,
        )
        if box is None:
            return []
        items: list[dict] = []
        for a in box.find_all("a", class_="newsline"):
            href = str(a.get("href") or "")
            url = f"https://www.hltv.org{href}" if href.startswith("/") else href
            featured = "featured" in (a.get("class") or [])
            if featured:
                title_div = a.find("div", class_="featured-newstext")
                desc_div = a.find("div", class_="featured-small-newstext")
                items.append(
                    {
                        "title": title_div.get_text(strip=True) if title_div else "",
                        "desc": desc_div.get_text(strip=True) if desc_div else "",
                        "posted": "",
                        "url": url,
                        "featured": True,
                    }
                )
            else:
                title_div = a.find("div", class_="newstext")
                posted_div = a.find("div", class_="newsrecent")
                items.append(
                    {
                        "title": title_div.get_text(strip=True) if title_div else "",
                        "desc": "",
                        "posted": posted_div.get_text(strip=True) if posted_div else "",
                        "url": url,
                        "featured": False,
                    }
                )
        return [i for i in items if i["title"] and i["url"]]

    async def get_news_detail(self, url: str) -> dict:
        """文章详情：标题 + 正文前几段。解析不出正文时段落为空，由上层降级。"""

        async def fetch() -> dict:
            page = await self._fetch_raw(url)
            h1 = page.find("h1")
            container = page.find("article") or page
            paras = [p.get_text(" ", strip=True) for p in container.find_all("p")]
            paras = [p for p in paras if len(p) >= 40][:4]
            return {
                "title": h1.get_text(strip=True) if h1 is not None else "",
                "paragraphs": paras,
            }

        return await self._cached_locked(f"news_detail:{url}", fetch)

    def clear_cache(self) -> None:
        self._cache.clear()
