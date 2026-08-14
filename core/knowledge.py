"""LLM knowledge-tool query routing and text formatting."""

import re
from collections.abc import Awaitable, Callable

from . import formatter
from .client import TOP20_MIN_YEAR, VRS_REGIONS, HltvError, team_query_variants


_REGION_CN = {"Asia": "亚洲", "Europe": "欧洲", "Americas": "美洲"}

EVENT_ALIASES = {
    "ewc": "Esports World Cup",
    "电竞世界杯": "Esports World Cup",
    "epl": "ESL Pro League",
    "iem": "IEM",
    "blast": "BLAST",
    "pgl": "PGL",
    "esl": "ESL",
    "cct": "CCT",
    "major": "Major",
}

_CATEGORY_ALIASES = {
    "live": "live", "直播": "live", "比分": "live",
    "schedule": "schedule", "matches": "schedule", "赛程": "schedule",
    "results": "results", "result": "results", "赛果": "results", "结果": "results",
    "ranking": "ranking", "rank": "ranking", "排名": "ranking",
    "events": "events", "event": "events", "赛事": "events",
    "team": "team", "战队": "team",
    "player": "player", "选手": "player",
    "news": "news", "新闻": "news",
    "top20": "top20", "top": "top20", "年度榜单": "top20",
    "auto": "auto",
}

LiveScoreLoader = Callable[[list[dict], int], Awaitable[object]]


def _normalize(value: object) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", str(value).casefold())


def _query_variants(query: str) -> tuple[str, ...]:
    values = list(team_query_variants(query))
    event_name = EVENT_ALIASES.get(_normalize(query))
    if event_name:
        values.append(event_name)
    return tuple(dict.fromkeys(_normalize(value) for value in values if _normalize(value)))


def _matches_text(query: str, *values: object) -> bool:
    needles = _query_variants(query)
    for value in values:
        haystack = _normalize(value)
        if haystack and any(needle in haystack or haystack in needle for needle in needles):
            return True
    return False


def match_team_query(query: str, match: dict) -> bool:
    return _matches_text(query, match.get("team1", ""), match.get("team2", ""))


def match_match_query(query: str, match: dict) -> bool:
    return _matches_text(
        query,
        match.get("team1", ""),
        match.get("team2", ""),
        match.get("event", ""),
    )


def _days_and_filter(query: str, default_days: int) -> tuple[int, str]:
    value = str(query or "").strip()
    lowered = value.casefold()
    if lowered in {"today", "today's", "今日", "今天"}:
        return 1, ""
    if lowered in {"tomorrow", "明日", "明天"}:
        return 2, ""
    if lowered in {"week", "this week", "本周", "这周", "一周"}:
        return 7, ""
    if value.isdigit() and 1 <= int(value) <= 7:
        return int(value), ""

    day_match = re.search(r"(?<!\d)([1-7])\s*(?:天|days?)\b", value, re.I)
    if day_match:
        days = int(day_match.group(1))
        filter_text = (value[: day_match.start()] + value[day_match.end() :]).strip()
        return days, filter_text.strip(" ,;|/，；")
    return (7 if value else default_days), value


class HltvKnowledgeService:
    def __init__(
        self,
        client,
        *,
        max_items: int,
        default_days: int,
        live_score_loader: LiveScoreLoader,
    ):
        self.client = client
        self.max_items = max_items
        self.default_days = default_days
        self.live_score_loader = live_score_loader

    async def query(self, category: str, query: str) -> str:
        raw_category = str(category or "").strip().casefold()
        kind = "auto" if not raw_category else _CATEGORY_ALIASES.get(raw_category)
        value = str(query or "").strip()
        if kind is None:
            return (
                "不支持的查询类型。category 请使用 live、schedule、results、ranking、"
                "events、team、player、news、top20 或 auto。"
            )

        if kind == "auto":
            kind, value = self._resolve_auto_category(value)

        if kind == "auto":
            return await self._query_auto(value)
        if kind == "live":
            return await self._query_live(value)
        if kind in {"schedule", "results"}:
            return await self._query_matches(kind, value)
        if kind == "ranking":
            return await self._query_ranking(value)
        if kind == "events":
            return await self._query_events(value)
        if kind == "team":
            if not value:
                return "team 查询需要在 query 中提供战队名称。"
            return formatter.format_team(await self.client.find_team(value))
        if kind == "player":
            if not value:
                return "player 查询需要在 query 中提供选手昵称。"
            return formatter.format_player(await self.client.find_player(value))
        if kind == "news":
            return await self._query_news(value)
        return await self._query_top20(value)

    @staticmethod
    def _resolve_auto_category(value: str) -> tuple[str, str]:
        lowered = value.casefold()
        top20_pattern = r"(?i)\btop\s*20\b|年度榜单"
        if re.search(top20_pattern, value):
            remaining = re.sub(top20_pattern, "", value).strip(" :：,，")
            player_name = re.sub(r"\b\d{4}\b", "", remaining).strip()
            return ("player", player_name) if player_name else ("top20", value)

        exact_categories = {
            key: category
            for key, category in _CATEGORY_ALIASES.items()
            if category in {"live", "schedule", "results", "ranking", "events", "news"}
        }
        kind = exact_categories.get(lowered, "auto")
        return kind, "" if kind != "auto" else value

    async def _query_auto(self, value: str) -> str:
        if not value:
            return "query 不能为空，请提供要查询的 CS 赛事、战队或选手信息。"
        try:
            return formatter.format_player(await self.client.find_player(value))
        except HltvError:
            try:
                return formatter.format_team(await self.client.find_team(value))
            except HltvError:
                return f"没有找到与「{value}」匹配的 HLTV 选手或战队资料。"

    async def _query_live(self, value: str) -> str:
        matches = await self.client.get_live_matches()
        if value:
            matches = [match for match in matches if match_match_query(value, match)]
        shown = matches[:4]
        await self.live_score_loader(shown, 4)
        note = f"另有 {len(matches) - len(shown)} 场未列出。" if len(matches) > 4 else ""
        return formatter.format_live(shown, note)

    async def _query_matches(self, kind: str, value: str) -> str:
        days, filter_text = _days_and_filter(value, self.default_days)
        if kind == "schedule":
            matches = await self.client.get_matches(days=days)
            if filter_text:
                matches = [
                    match for match in matches if match_match_query(filter_text, match)
                ]
            return formatter.format_matches(matches, days, self.max_items)

        results = await self.client.get_results(days=days)
        if filter_text:
            results = [
                match for match in results if match_match_query(filter_text, match)
            ]
        return formatter.format_results(results, days, self.max_items)

    async def _query_ranking(self, value: str) -> str:
        arg = value.casefold()
        if arg in {"hltv", "h"}:
            teams = await self.client.get_top_teams(50)
            return formatter.format_ranking(teams, self.max_items, "HLTV 战队排名 Top50")
        if arg in {"", "v", "vrs", "valve", "global", "全球"}:
            region = None
        else:
            region = VRS_REGIONS.get(arg)
            if region is None:
                return "ranking 的 query 请使用 hltv、vrs、asia、europe 或 americas。"
        teams = await self.client.get_vrs_ranking(region)
        title = (
            "Valve VRS 排名（全球）"
            if region is None
            else f"Valve VRS 排名（{_REGION_CN.get(region, region)}）"
        )
        return formatter.format_ranking(
            teams, self.max_items, title, show_region=region is None
        )

    async def _query_events(self, value: str) -> str:
        events = await self.client.get_events()
        if value:
            events = [
                item for item in events if _matches_text(value, item.get("title", ""))
            ]
        return formatter.format_events(events, self.max_items)

    async def _query_news(self, value: str) -> str:
        items = await self.client.get_news()
        lowered = value.casefold()
        if lowered in {"", "latest", "today", "最新", "今日", "今天"}:
            return formatter.format_news(items, self.max_items).replace(
                "\n👉 发送 /hltv news 序号 查看详情", ""
            )
        if value.isdigit():
            index = int(value)
            if not 1 <= index <= len(items):
                return f"没有第 {index} 条新闻（今日共 {len(items)} 条）。"
            item = items[index - 1]
        else:
            terms = re.findall(r"[0-9a-z]+", lowered)
            matched = [
                item
                for item in items
                if _matches_text(value, item.get("title", ""))
                or (
                    terms
                    and all(
                        term in str(item.get("title") or "").casefold()
                        for term in terms
                    )
                )
            ]
            if not matched:
                return formatter.format_news(items, self.max_items).replace(
                    "\n👉 发送 /hltv news 序号 查看详情", ""
                )
            item = matched[0]
        detail = await self.client.get_news_detail(str(item.get("url") or ""))
        title = str(detail.get("title") or item.get("title") or "")
        paragraphs = list(detail.get("paragraphs") or [])
        if not paragraphs and item.get("desc"):
            paragraphs = [str(item["desc"])]
        return formatter.format_news_detail(
            title,
            paragraphs,
            str(item.get("url") or ""),
            original_title=title,
        )

    async def _query_top20(self, value: str) -> str:
        numbers = [int(number) for number in re.findall(r"\d+", value)]
        latest = self.client.latest_top20_year()
        year = next((number for number in numbers if number >= 1000), latest)
        rank = next((number for number in numbers if 1 <= number <= 20), 0)
        if year < TOP20_MIN_YEAR or year > latest:
            return f"TOP20 年份范围为 {TOP20_MIN_YEAR}-{latest}。"
        if rank:
            player = await self.client.get_top20_player(year, rank)
            return formatter.format_news_detail(
                str(player.get("title") or f"HLTV {year} TOP20 #{rank}"),
                [str(player.get("description"))] if player.get("description") else [],
                str(player.get("url") or ""),
            )
        players = await self.client.get_top20_players(year)
        return formatter.format_top20(players, year)
