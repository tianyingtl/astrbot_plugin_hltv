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
import json
import re
import time
from datetime import datetime, timedelta
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


# 锁排队的最长等待时间（秒）
_QUEUE_TIMEOUT = 30

# 比赛列表缓存上限（秒）：直播状态不能吃默认 5 分钟缓存
_LIVE_TTL = 60

_SEARCH_URL = "https://www.hltv.org/search?term={}"
_MATCHES_URL = "https://www.hltv.org/matches"
_RANKING_URL = "https://www.hltv.org/ranking/teams"
_VRS_URL = "https://www.hltv.org/valve-ranking/teams"
_HOME_URL = "https://www.hltv.org/"

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
RANKING_HLTV_KEY = "ranking:hltv:50"
EVENTS_KEY = "events"
NEWS_KEY = "news"


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
            names = [t.get_text(strip=True) for t in w.find_all("div", class_="match-teamname")]
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
                "event": event,
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

    async def _get_matches_raw(self) -> list[dict]:
        async def fetch() -> list[dict]:
            page = await self._fetch_raw(_MATCHES_URL)
            return self._parse_matches_page(page)

        ttl = min(float(self._cache_ttl or _LIVE_TTL), _LIVE_TTL)
        return await self._cached_locked(MATCHES_RAW_KEY, fetch, ttl=ttl)

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
    def _parse_match_maps(page: Any) -> list[dict]:
        """单场详情页的地图比分（服务器渲染，列表页拿不到）。
        played 标识已经进入的地图，finished / winner 来自结果区的 won/lost 类。"""
        maps = []
        for holder in page.find_all("div", class_="mapholder"):
            name_el = holder.find(class_="mapname")
            result = holder.find("div", class_="results")
            result_classes = result.get("class") or [] if result is not None else []
            left = holder.find(class_="results-left")
            right = holder.find(class_="results-right")
            left_classes = left.get("class") or [] if left is not None else []
            right_classes = right.get("class") or [] if right is not None else []
            scores = [
                e.get_text(strip=True)
                for e in holder.find_all(class_="results-team-score")
            ]
            if len(scores) >= 2:
                winner = 1 if "won" in left_classes else (2 if "won" in right_classes else 0)
                maps.append(
                    {
                        "map": name_el.get_text(strip=True) if name_el else "?",
                        "s1": scores[0],
                        "s2": scores[1],
                        "played": "played" in result_classes,
                        "finished": winner > 0,
                        "winner": winner,
                    }
                )
        return maps

    @staticmethod
    def summarize_map_scores(maps: list[dict]) -> dict:
        """地图列表 → 大局比分、当前地图与当前小局比分。"""
        won1 = won2 = 0
        active = None
        active_index = 0
        for index, m in enumerate(maps, start=1):
            s1, s2 = str(m.get("s1", "")), str(m.get("s2", ""))
            finished = m.get("finished")
            if finished is None:
                finished = s1.isdigit() and s2.isdigit() and max(int(s1), int(s2)) >= 13
            if finished:
                winner = int(m.get("winner") or 0)
                if not winner and s1.isdigit() and s2.isdigit() and s1 != s2:
                    winner = 1 if int(s1) > int(s2) else 2
                won1 += winner == 1
                won2 += winner == 2

            played = m.get("played")
            if played is None:
                played = s1.isdigit() or s2.isdigit()
            if played and not finished:
                active = m
                active_index = index

        current_name = str(active.get("map") or "?") if active else ""
        current_score = ""
        if active:
            s1, s2 = str(active.get("s1", "")), str(active.get("s2", ""))
            if s1.isdigit() and s2.isdigit():
                current_score = f"{s1}:{s2}"
        if not active:
            played_indexes = [
                i
                for i, item in enumerate(maps, start=1)
                if item.get("played") or str(item.get("s1", "")).isdigit()
            ]
            active_index = played_indexes[-1] if played_indexes else 0
        return {
            "maps_score": f"{won1}:{won2}",
            "current_map": (
                f"{current_name} {current_score}"
                if current_name and current_score
                else (f"{current_name} · 比分暂未同步" if current_name else "")
            ),
            "current_map_name": current_name,
            "current_score": current_score,
            "active_map_index": active_index,
            "map_total": len(maps),
        }

    @staticmethod
    def _parse_match_ratings(page: Any) -> tuple[str, list[dict]]:
        container = page.select_one(".stats-content#all-content")
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
                    players.append(
                        {
                            "nickname": nick_el.get_text(" ", strip=True),
                            "rating": rating,
                        }
                    )
            if players:
                teams.append({"team": team, "players": players})
        return version, teams

    @classmethod
    def _parse_match_snapshot(cls, page: Any) -> dict:
        maps = cls._parse_match_maps(page)
        summary = cls.summarize_map_scores(maps)
        countdown_el = page.select_one(".countdown")
        countdown = countdown_el.get_text(" ", strip=True).lower() if countdown_el else ""
        finished = "match over" in countdown

        team1_el = page.select_one(".team1-gradient .teamName")
        team2_el = page.select_one(".team2-gradient .teamName")
        event_el = page.select_one(".timeAndEvent .event")
        version, ratings = cls._parse_match_ratings(page) if finished else ("", [])
        return {
            **summary,
            "status": "finished" if finished else "live",
            "team1": team1_el.get_text(" ", strip=True) if team1_el else "?",
            "team2": team2_el.get_text(" ", strip=True) if team2_el else "?",
            "event": event_el.get_text(" ", strip=True) if event_el else "",
            "maps": maps,
            "rating_version": version,
            "ratings": ratings,
        }

    async def get_match_snapshot(
        self, match_id: Any, url: str, *, watch: bool = False
    ) -> dict:
        if not url:
            raise HltvError("该比赛没有详情页链接。")

        async def fetch() -> dict:
            page = await self._fetch_raw(url)
            return self._parse_match_snapshot(page)

        ttl = min(float(self._cache_ttl or _LIVE_TTL), 20 if watch else _LIVE_TTL)
        channel = "watch" if watch else "view"
        return await self._cached_locked(
            f"match_snapshot:{channel}:{match_id}", fetch, ttl=ttl
        )

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
        needle = name.strip().lower()
        try:
            teams = await self.get_top_teams(50)
        except HltvError:
            teams = []
        matched = next(
            (t for t in teams if needle in str(t.get("title", "")).lower()), None
        )
        if matched and matched.get("id"):
            team_id, title = matched["id"], matched.get("title") or name
        else:
            found = (await self._search(name)).get("teams") or []
            if not found or not isinstance(found[0], dict) or "id" not in found[0]:
                raise HltvError(f"没有找到战队「{name}」。")
            team_id = found[0]["id"]
            title = found[0].get("name") or found[0].get("title") or name
        return await self.get_team_details(team_id, str(title))

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
        """解析选手资料、年度 Top20、Major 与赛事冠军。"""
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
            "total_trophies": 0,
            "total_mvps": 0,
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

        for rank_el in page.select(".playerTop20 .top20ListRight a"):
            rank_match = re.search(r"#(\d+)", rank_el.get_text(" ", strip=True))
            year_el = rank_el.find_next_sibling("span", class_="top-20-year")
            year_match = (
                re.search(r"\d{2,4}", year_el.get_text(" ", strip=True))
                if year_el is not None
                else None
            )
            if not rank_match or not year_match:
                continue
            year = int(year_match.group())
            if year < 100:
                year += 2000
            info["top20"].append({"year": year, "rank": int(rank_match.group(1))})

        def count(selector: str) -> int:
            el = page.select_one(selector)
            match = re.search(r"\d+", el.get_text(" ", strip=True)) if el else None
            return int(match.group()) if match else 0

        info["major_wins"] = count(".majorSection .majorWinner")
        info["major_mvps"] = count(".majorSection .majorMVP")
        info["total_mvps"] = count(".trophySection .mvp-count")

        for trophy in page.select(".trophySection a.trophy[href^='/events/']"):
            desc = trophy.select_one(".trophyDescription")
            name = str(desc.get("title") or "").strip() if desc else ""
            if name:
                info["championships"].append(
                    {
                        "name": name,
                        "major": "majorTrophy" in (desc.get("class") or []),
                    }
                )
        info["total_trophies"] = len(info["championships"])
        return info

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

    # ---------------------------------------------------------------- 新闻

    async def get_news(self) -> list[dict]:
        """今日新闻（自建主页解析：保留完整文章链接、不丢头条）。
        条目：{'title','url','desc','posted','featured'}"""

        async def fetch() -> list[dict]:
            page = await self._fetch_raw(_HOME_URL)
            return self._parse_homepage_news(page)

        return await self._cached_locked(NEWS_KEY, fetch)

    @staticmethod
    def _parse_homepage_news(page: Any) -> list[dict]:
        box = page.find("div", class_="standard-box standard-list")
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
