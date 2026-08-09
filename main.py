"""astrbot_plugin_hltv — HLTV 查询插件入口。

分层：
    main.py             指令注册、参数解析、翻译/推送编排（本文件）
    core/client.py      HLTV 数据访问（缓存、限流、自建解析器）
    core/formatter.py   数据 → 文本消息
    core/renderer.py    查询结果图片卡渲染
    core/subscriptions.py 直播提醒持久化与状态流转
    core/translator.py  微软翻译（免费 Edge 通道）
"""

import asyncio
import difflib
import re
from datetime import timedelta
from time import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from .core import formatter
from .core.client import (
    EVENTS_KEY,
    HOME_LIVE_KEY,
    MATCHES_RAW_KEY,
    NEWS_KEY,
    RANKING_HLTV_KEY,
    TOP20_MIN_YEAR,
    VRS_REGIONS,
    HltvClient,
    HltvError,
    results_key,
    team_query_variants,
    vrs_key,
)
from .core.renderer import (
    render_events_card,
    render_live_card,
    render_matches_card,
    render_news_card,
    render_player_card,
    render_rating_card,
    render_ranking_card,
    render_results_card,
    render_team_card,
    render_top20_card,
    render_top20_player_card,
)
from .core.subscriptions import (
    LiveSubscriptionStore,
    advance_subscription,
)
from .core.translator import Translator

_REGION_CN = {"Asia": "亚洲", "Europe": "欧洲", "Americas": "美洲"}

# 全部子指令名与别名。新增子指令时同步维护，
# 用于拼错提示（未匹配到任何子指令时框架会静默，体验极差）
_KNOWN_SUBCOMMANDS = {
    "help", "帮助",
    "today", "今日", "今天", "今日赛程",
    "matches", "比赛", "大赛",
    "live", "直播",
    "results", "赛果", "结果",
    "ranking", "排名", "排行",
    "top20", "年度榜单",
    "events", "赛事",
    "team", "战队",
    "player", "选手",
    "news", "新闻",
    "sub", "订阅",
    "unsub", "退订", "取消订阅",
}


class HltvPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_items = int(config.get("max_items", 10))
        # HLTV 星级门槛（0-5）：只显示 ≥ 该星级的比赛，0 = 不过滤
        self.min_stars = max(0, min(int(config.get("min_stars", 1)), 5))
        # 大赛关键词白名单（空 = 不启用），对 matches / today 生效
        self.event_keywords = [
            str(k).strip().lower()
            for k in (config.get("event_keywords") or [])
            if str(k).strip()
        ]
        self.default_days = max(1, min(int(config.get("default_days", 1)), 7))
        self.send_waiting_tip = bool(config.get("send_waiting_tip", False))
        # 群里直接发 /hltv 指令即可响应,无需 @ 机器人或依赖全局唤醒前缀
        self.free_wake = bool(config.get("free_wake", True))
        self.translate_news = bool(config.get("translate_news", True))
        self.enable_push = bool(config.get("enable_push", False))
        self._push_hm = self._parse_push_time(str(config.get("push_time", "09:00")))
        # 展示用的时间取解析结果，配置串无效回退 09:00 时不给用户看原始串
        self.push_time = "{:02d}:{:02d}".format(*self._push_hm)
        self._push_task: asyncio.Task | None = None
        self.live_poll_interval = max(
            20, min(int(config.get("live_poll_interval", 45)), 300)
        )
        self._live_watch_task: asyncio.Task | None = None
        self.live_subscriptions = LiveSubscriptionStore()
        self._live_selection_cache: dict[tuple[str, str], tuple[int, list[dict]]] = {}

        self.client = HltvClient(
            proxy_list=[p for p in (config.get("proxy_list") or []) if p],
            timeout=int(config.get("timeout", 15)),
            max_retries=int(config.get("max_retries", 3)),
            tz=str(config.get("timezone", "Asia/Shanghai")),
            cache_ttl=int(config.get("cache_ttl", 300)),
        )
        self.translator = Translator(timeout=max(30, int(config.get("timeout", 15))))

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _parse_push_time(s: str) -> tuple[int, int]:
        try:
            h, m = str(s).strip().split(":")
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except (ValueError, AttributeError):
            pass
        logger.warning(f"[hltv] push_time {s!r} 格式无效（应为 HH:MM），已回退 09:00")
        return 9, 0

    @staticmethod
    def _rest_after(event: AstrMessageEvent, subcmds: set[str], fallback: str) -> str:
        """取消息中子指令（含别名）之后的整段文本。

        AstrBot 的 str 指令参数按空格只绑定一个词，这里从原始消息里把
        子指令后面的部分整体取出，让含空格的战队名/选手名也能查。
        """
        tokens = (event.message_str or "").split()
        for i, tok in enumerate(tokens):
            if tok.lower() in subcmds:
                rest = " ".join(tokens[i + 1 :]).strip()
                return rest or fallback
        return fallback

    @staticmethod
    def _team_query_match(query: str, m: dict) -> bool:
        """队名模糊匹配：规范化(去空格/符号、小写)后做双向子串，
        并展开常见缩写、中文称呼。"""
        def norm(s):
            return re.sub(r"[^0-9a-z一-鿿]", "", str(s).lower())

        queries = [norm(value) for value in team_query_variants(query)]
        if not queries:
            return False
        for t in (m.get("team1", ""), m.get("team2", "")):
            tn = norm(t)
            if tn and any(q in tn or tn in q for q in queries if q):
                return True
        return False

    def _filter_by_event(self, matches: list[dict]) -> list[dict]:
        if not self.event_keywords:
            return matches
        return [
            m
            for m in matches
            if any(k in str(m.get("event", "")).lower() for k in self.event_keywords)
        ]

    def _waiting_tip(self, event: AstrMessageEvent, cache_key: str | None = None):
        """查询前的等待提示；结果已有缓存（秒回）时不发，免得连刷两条。"""
        if not self.send_waiting_tip:
            return None
        if cache_key is not None and self.client.is_fresh(cache_key):
            return None
        return event.plain_result("🔎 正在查询 HLTV，稍等…")

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        return str(getter() or "") if callable(getter) else ""

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_name", None)
        return str(getter() or "") if callable(getter) else ""

    def _ensure_live_watch_task(self) -> None:
        if self._live_watch_task is None or self._live_watch_task.done():
            self._live_watch_task = asyncio.create_task(self._live_watch_loop())

    @staticmethod
    def _has_cjk(text: str) -> bool:
        return bool(re.search(r"[\u3400-\u9fff]", str(text)))

    @classmethod
    def _is_chinese_translation(cls, source: str, translated: str) -> bool:
        source, translated = str(source).strip(), str(translated).strip()
        if not source:
            return True
        if cls._has_cjk(source):
            return True
        return translated != source and cls._has_cjk(translated)

    async def _translate_to_chinese(self, texts: list[str]) -> list[str]:
        """翻译失败时重试一次；只把确实含中文的结果视为成功。"""
        source = [str(text) for text in texts]
        translated = await self.translator.translate(source)
        unresolved = [
            index
            for index, (raw, zh) in enumerate(zip(source, translated))
            if raw.strip() and not self._is_chinese_translation(raw, zh)
        ]
        if unresolved:
            retried = await self.translator.translate([source[index] for index in unresolved])
            for index, zh in zip(unresolved, retried):
                if self._is_chinese_translation(source[index], zh):
                    translated[index] = zh
        return translated

    async def _fetch_today_view(self) -> tuple[list[dict], str]:
        """今日赛程 + 空结果自动回退：配置了星级/关键词过滤但过滤后为空时，
        改用不过滤的全量再展示一次，并附说明，而不是只回一句"没有比赛"。"""
        data = self._filter_by_event(
            await self.client.get_today_matches(min_stars=self.min_stars)
        )
        note = ""
        if not data and (self.min_stars > 0 or self.event_keywords):
            alt = await self.client.get_today_matches(min_stars=0)
            if alt:
                data, note = alt, "（今日无符合大赛条件的比赛，已显示全部场次）"
        return data, note

    @staticmethod
    async def _image_or_text(
        event: AstrMessageEvent,
        renderer,
        fallback: str,
        *args,
        log_name: str,
        **kwargs,
    ):
        try:
            card = await asyncio.to_thread(renderer, *args, **kwargs)
            return event.image_result(str(card))
        except Exception as e:
            logger.warning(f"[hltv] {log_name}渲染失败，回退文本: {e!r}")
            return event.plain_result(fallback)

    def _limit_items(self, items: list[dict]) -> list[dict]:
        return items[: self.max_items] if self.max_items > 0 else items

    async def _fetch_live_scores(
        self, matches: list[dict], limit: int
    ) -> tuple[list[tuple[dict, dict]], int]:
        scored = []
        failures = 0
        for match in matches[:limit]:
            try:
                snapshot = await self.client.get_live_snapshot(match)
            except HltvError as e:
                failures += 1
                logger.warning(f"[hltv] 获取比分失败({match.get('id')}): {e}")
                continue
            match.update(snapshot)
            scored.append((match, snapshot))
        return scored, failures

    @staticmethod
    def _parse_live_indexes(value: str) -> list[int] | None:
        tokens = str(value).split()
        if not tokens or not all(token.isdigit() for token in tokens):
            return None
        return list(dict.fromkeys(int(token) for token in tokens))

    def _remember_live_selection(
        self, event: AstrMessageEvent, matches: list[dict]
    ) -> None:
        user_id = self._sender_id(event)
        umo = str(getattr(event, "unified_msg_origin", ""))
        if user_id and umo:
            self._live_selection_cache[(umo, user_id)] = (
                int(time()),
                [dict(match) for match in matches],
            )

    def _get_live_selection(self, event: AstrMessageEvent) -> list[dict]:
        user_id = self._sender_id(event)
        umo = str(getattr(event, "unified_msg_origin", ""))
        cached = self._live_selection_cache.get((umo, user_id))
        if cached is None:
            return []
        created_at, matches = cached
        if int(time()) - created_at > 5 * 60:
            self._live_selection_cache.pop((umo, user_id), None)
            return []
        return [dict(match) for match in matches]

    async def _subscribe_live_matches(
        self,
        event: AstrMessageEvent,
        matches: list[dict],
        limit: int,
    ) -> tuple[int, int, int]:
        scored, failures = await self._fetch_live_scores(matches, limit)
        watched = already_watching = 0
        sender_id = self._sender_id(event)
        sender_name = self._sender_name(event)
        umo = str(getattr(event, "unified_msg_origin", ""))
        for match, snapshot in scored:
            try:
                if not sender_id:
                    continue
                added = self.live_subscriptions.add(
                    match,
                    snapshot,
                    umo=umo,
                    user_id=sender_id,
                    user_name=sender_name,
                )
                if added:
                    watched += 1
                else:
                    already_watching += 1
            except OSError as e:
                failures += 1
                logger.warning(f"[hltv] 保存直播订阅失败: {e!r}")
        if watched:
            self._ensure_live_watch_task()
        return watched, already_watching, failures

    def _subscribe_upcoming_matches(
        self,
        event: AstrMessageEvent,
        matches: list[dict],
    ) -> tuple[int, int, int]:
        watched = already_watching = failures = 0
        sender_id = self._sender_id(event)
        sender_name = self._sender_name(event)
        umo = str(getattr(event, "unified_msg_origin", ""))
        if not sender_id or not umo:
            return 0, 0, len(matches)
        for match in matches:
            if not match.get("id") or not match.get("url"):
                failures += 1
                continue
            try:
                added = self.live_subscriptions.add(
                    match,
                    None,
                    umo=umo,
                    user_id=sender_id,
                    user_name=sender_name,
                    pending_start=True,
                )
                if added:
                    watched += 1
                else:
                    already_watching += 1
            except OSError as e:
                failures += 1
                logger.warning(f"[hltv] 保存待开赛订阅失败: {e!r}")
        if watched:
            self._ensure_live_watch_task()
        return watched, already_watching, failures

    # ------------------------------------------------------------------ 指令组

    @filter.command_group("hltv")
    def hltv(self):
        """HLTV 查询指令组，输入 /hltv help 查看用法。"""

    @hltv.command("help", alias={"帮助"})
    async def help(self, event: AstrMessageEvent):
        """显示帮助"""
        yield event.plain_result(formatter.HELP_TEXT)

    @hltv.command("today", alias={"今日", "今天", "今日赛程"})
    async def today(self, event: AstrMessageEvent):
        """今日赛程（直播优先）"""
        if tip := self._waiting_tip(event, MATCHES_RAW_KEY):
            yield tip
        try:
            data, note = await self._fetch_today_view()
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        fallback = formatter.format_today(
            data, self.max_items, self.min_stars, bool(self.event_keywords), note
        )
        shown = self._limit_items(data)
        subtitle = note or f"共 {len(data)} 场  /  显示前 {len(shown)} 场"
        yield await self._image_or_text(
            event,
            render_matches_card,
            fallback,
            shown,
            "今日赛程",
            subtitle=subtitle,
            log_name="今日赛程卡片",
        )

    @hltv.command("matches", alias={"比赛", "大赛"})
    async def matches(self, event: AstrMessageEvent, days: int = 0):
        """近期大赛，可带天数"""
        days = days if days > 0 else self.default_days
        days = min(days, 7)
        if tip := self._waiting_tip(event, MATCHES_RAW_KEY):
            yield tip
        try:
            data = self._filter_by_event(
                await self.client.get_matches(days=days, min_stars=self.min_stars)
            )
            note = ""
            if not data and (self.min_stars > 0 or self.event_keywords):
                alt = await self.client.get_matches(days=days, min_stars=0)
                if alt:
                    data, note = alt, "（该时段无符合大赛条件的比赛，已显示全部场次）"
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        fallback = formatter.format_matches(
            data, days, self.max_items, self.min_stars, bool(self.event_keywords), note
        )
        shown = self._limit_items(data)
        subtitle = note or f"共 {len(data)} 场  /  显示前 {len(shown)} 场"
        yield await self._image_or_text(
            event,
            render_matches_card,
            fallback,
            shown,
            f"近 {days} 天赛程",
            subtitle=subtitle,
            log_name="赛程卡片",
        )

    @hltv.command("live", alias={"直播"})
    async def live(self, event: AstrMessageEvent, name: str = ""):
        """正在进行的比赛（默认只看大赛，带比分）；可带队名只看该队"""
        name = self._rest_after(event, {"live", "/live", "直播"}, name)
        if name.lower() in {"cancel", "取消", "退订", "取消订阅"}:
            removed = self.live_subscriptions.remove_user(
                str(event.unified_msg_origin), self._sender_id(event)
            )
            message = (
                f"已取消 {removed} 场直播提醒。"
                if removed
                else "当前没有你的直播提醒。"
            )
            yield event.plain_result(message)
            return
        indexes = self._parse_live_indexes(name)
        if indexes is not None:
            if not self._sender_id(event):
                yield event.plain_result("当前平台未提供用户 ID，无法建立 @ 提醒。")
                return
            shown = self._get_live_selection(event)
            if not shown:
                yield event.plain_result(
                    "订阅编号已过期或尚未生成，请先发送 /live 获取当前列表。"
                )
                return
            if any(index <= 0 or index > len(shown) for index in indexes):
                yield event.plain_result(
                    f"当前卡片只有 1-{len(shown)} 号比赛，请按卡片序号重新选择。"
                )
                return
            selected = [shown[index - 1] for index in indexes]
            watched, already_watching, failures = (
                await self._subscribe_live_matches(event, selected, len(selected))
            )
            lines = []
            if watched:
                lines.append(f"已订阅 {watched} 场：")
            elif already_watching:
                lines.append(f"所选 {already_watching} 场均已在监听：")
            else:
                lines.append("未能建立直播订阅。")
            lines.extend(
                f"{index}. {match.get('team1') or '?'} vs {match.get('team2') or '?'}"
                for index, match in zip(indexes, selected)
            )
            if failures:
                lines.append(f"另有 {failures} 场比分读取或订阅保存失败，请稍后重试。")
            if already_watching and watched:
                lines.append(f"另有 {already_watching} 场已在监听，无需重复订阅。")
            if watched or already_watching:
                lines.append(
                    "每张地图结束后会推送该图 Rating；新地图开始和整场完赛时也会 @ 你。"
                )
            yield event.plain_result("\n".join(lines))
            return
        if tip := self._waiting_tip(event, HOME_LIVE_KEY):
            yield tip
        if name:
            # 指定战队：不做星级/关键词过滤——用户点名要看的队就该给结果
            try:
                mine = [
                    m
                    for m in await self.client.get_live_matches()
                    if self._team_query_match(name, m)
                ]
                upcoming = None
                if not mine:
                    upcoming = next(
                        (
                            m
                            for m in await self.client.get_matches(days=1)
                            if self._team_query_match(name, m)
                        ),
                        None,
                    )
            except HltvError as e:
                yield event.plain_result(str(e))
                return
            watched, already_watching, subscription_failures = (
                await self._subscribe_live_matches(event, mine, 2)
            )
            if mine:
                text = formatter.format_live(mine)
                footer = ""
                if watched:
                    footer = (
                        f"已订阅 {watched} 场：逐图 Rating、新地图开始和完赛 Rating 会 @ 你。"
                    )
                    text += f"\n\n{footer}"
                elif already_watching:
                    footer = "这场比赛已在监听；逐图 Rating、新地图开始和完赛时会 @ 你。"
                    text += f"\n\n{footer}"
                if not self._sender_id(event):
                    footer = "当前平台未提供用户 ID，无法建立 @ 提醒。"
                    text += f"\n\n{footer}"
                elif subscription_failures:
                    footer = (
                        f"有 {subscription_failures} 场详情获取或订阅保存失败，"
                        "未建立提醒，请稍后重试。"
                    )
                    text += f"\n\n{footer}"
                yield await self._image_or_text(
                    event,
                    render_live_card,
                    text,
                    mine,
                    footer=footer,
                    log_name="直播卡片",
                )
            else:
                text = formatter.format_team_not_live(name, upcoming)
                if upcoming:
                    if not self._sender_id(event):
                        text += "\n当前平台未提供用户 ID，无法建立开赛 @ 提醒。"
                    else:
                        pending, existing, failures = self._subscribe_upcoming_matches(
                            event, [upcoming]
                        )
                        if pending:
                            text += "\n已订阅这场比赛，图一正式开始后会 @ 你。"
                        elif existing:
                            text += "\n这场待开赛比赛已在监听，图一开始后会 @ 你。"
                        elif failures:
                            text += "\n订阅保存失败，请稍后重试。"
                yield event.plain_result(text)
            return
        try:
            data = self._filter_by_event(
                await self.client.get_live_matches(min_stars=self.min_stars)
            )
            note = ""
            if not data and (self.min_stars > 0 or self.event_keywords):
                alt = await self.client.get_live_matches()
                if alt:
                    data, note = alt, "（当前无大赛直播，已显示全部场次）"
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        shown = data[:4]
        await self._fetch_live_scores(shown, 4)
        fallback = formatter.format_live(shown, note)
        if data:
            self._remember_live_selection(event, shown)
            footer_parts = []
            if len(data) > len(shown):
                footer_parts.append(f"另有 {len(data) - len(shown)} 场未显示")
            footer = "  |  ".join(footer_parts)
            if footer:
                fallback += f"\n\n{footer}"
            yield await self._image_or_text(
                event,
                render_live_card,
                fallback,
                shown,
                note=note,
                footer=footer,
                log_name="直播卡片",
            )
            if self._sender_id(event):
                yield event.plain_result(
                    "订阅直播：发送 /live 1 2 3\n"
                    "数字对应图片中的比赛序号，可多选。"
                )
        else:
            yield await self._image_or_text(
                event,
                render_live_card,
                fallback,
                [],
                log_name="直播卡片",
            )

    @hltv.command("results", alias={"赛果", "结果"})
    async def results(self, event: AstrMessageEvent, days: int = 0):
        """近期赛果，可带天数"""
        days = days if days > 0 else self.default_days
        days = min(days, 7)
        if tip := self._waiting_tip(event, results_key(days, 0)):
            yield tip
        try:
            # 赛果不做星级过滤：已经打完的比赛用户要的是完整结果
            data = await self.client.get_results(days=days)
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        fallback = formatter.format_results(data, days, self.max_items)
        yield await self._image_or_text(
            event,
            render_results_card,
            fallback,
            self._limit_items(data),
            f"近 {days} 天赛果",
            log_name="赛果卡片",
        )

    @hltv.command("ranking", alias={"排名", "排行"})
    async def ranking(self, event: AstrMessageEvent, kind: str = ""):
        """排名：默认 Valve VRS，可加地区或 hltv"""
        arg = kind.strip().lower()
        if arg in ("hltv", "h"):
            use_vrs, region = False, None
            key, title = RANKING_HLTV_KEY, "HLTV 战队排名 Top50"
        elif arg in ("", "v", "vrs", "valve"):
            use_vrs, region = True, None
            key, title = vrs_key(None), "Valve VRS 排名（全球）"
        elif arg in VRS_REGIONS:
            use_vrs, region = True, VRS_REGIONS[arg]
            key = vrs_key(region)
            title = f"Valve VRS 排名（{_REGION_CN.get(region, region)}）"
        else:
            yield event.plain_result(
                "用法：/hltv ranking [地区|hltv]\n"
                "· 默认显示 Valve VRS 全球排名（V社积分）\n"
                "· 地区：asia/亚洲、europe/欧洲、americas/美洲\n"
                "· hltv：HLTV 自家世界排名"
            )
            return
        if tip := self._waiting_tip(event, key):
            yield tip
        try:
            if use_vrs:
                data = await self.client.get_vrs_ranking(region)
            else:
                data = await self.client.get_top_teams(50)
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        show_region = use_vrs and region is None
        fallback = formatter.format_ranking(data, self.max_items, title, show_region=show_region)
        yield await self._image_or_text(
            event,
            render_ranking_card,
            fallback,
            self._limit_items(data),
            title,
            show_region=show_region,
            log_name="排名卡片",
        )

    @hltv.command("events", alias={"赛事"})
    async def events(self, event: AstrMessageEvent):
        """近期赛事"""
        if tip := self._waiting_tip(event, EVENTS_KEY):
            yield tip
        try:
            data = await self.client.get_events()
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        fallback = formatter.format_events(data, self.max_items)
        yield await self._image_or_text(
            event,
            render_events_card,
            fallback,
            self._limit_items(data),
            log_name="赛事卡片",
        )

    @hltv.command("top20", alias={"年度榜单"})
    async def top20(
        self, event: AstrMessageEvent, year: str = "", rank: str = ""
    ):
        """HLTV 年度 TOP20 榜单或指定名次新闻；默认上一年。"""
        latest = self.client.latest_top20_year()
        raw_year = str(year or "").strip()
        if raw_year:
            try:
                selected = int(raw_year)
            except ValueError:
                yield event.plain_result(
                    "用法：/hltv top20 [年份] [名次]，例如 /hltv top20 2025 18"
                )
                return
        else:
            selected = latest
        if selected < TOP20_MIN_YEAR or selected > latest:
            yield event.plain_result(
                f"TOP20 年份范围为 {TOP20_MIN_YEAR}-{latest}；不填年份默认查询 {latest}。"
            )
            return
        raw_rank = str(rank or "").strip()
        selected_rank = 0
        if raw_rank:
            try:
                selected_rank = int(raw_rank)
            except ValueError:
                yield event.plain_result("TOP20 名次必须是 1-20 的整数。")
                return
            if not 1 <= selected_rank <= 20:
                yield event.plain_result("TOP20 名次范围为 1-20。")
                return

        cache_key = (
            f"top20:player:{selected}:{selected_rank}"
            if selected_rank
            else f"top20:{selected}"
        )
        if tip := self._waiting_tip(event, cache_key):
            yield tip
        if selected_rank:
            try:
                result = await self.client.get_top20_player(
                    selected, selected_rank
                )
            except HltvError as e:
                yield event.plain_result(str(e))
                return

            original_title = str(result.get("title") or "")
            title = original_title
            description = str(result.get("description") or "")
            if self.translate_news:
                translated = await self._translate_to_chinese(
                    [title, description]
                )
                title, description = translated
            card_data = {
                **result,
                "title": title,
                "description": description,
            }
            image = result.get("image_path")
            if not image:
                try:
                    image = await asyncio.to_thread(
                        render_top20_player_card, card_data
                    )
                except Exception as e:
                    logger.warning(f"[hltv] TOP20 个人卡片渲染失败，回退文本: {e!r}")

            news_text = formatter.format_news_detail(
                title,
                [description] if description else [],
                str(result.get("url") or ""),
                original_title=original_title,
            )
            if image:
                chain = MessageChain().message(news_text).file_image(str(image))
                yield event.chain_result(chain.chain)
            else:
                yield event.plain_result(
                    news_text + "\n\nTOP20 个人图片生成失败，请通过新闻原文查看。"
                )
            return

        try:
            result = await self.client.get_top20(selected)
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        image = result.get("image_path")
        if image:
            yield event.image_result(str(image))
            return
        players = result.get("players") or []
        yield await self._image_or_text(
            event,
            render_top20_card,
            formatter.format_top20(players, selected),
            players,
            selected,
            log_name="TOP20 卡片",
        )

    @hltv.command("team", alias={"战队"})
    async def team(self, event: AstrMessageEvent, name: str = ""):
        """战队信息，任意战队"""
        name = self._rest_after(event, {"team", "战队"}, name)
        if not name:
            yield event.plain_result("用法：/hltv team <战队名称>，例如 /hltv team navi")
            return
        if tip := self._waiting_tip(event):
            yield tip
        try:
            data = await self.client.find_team(name)
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        yield await self._image_or_text(
            event,
            render_team_card,
            formatter.format_team(data),
            data,
            log_name="战队卡片",
        )

    @hltv.command("player", alias={"选手"})
    async def player(self, event: AstrMessageEvent, nickname: str = ""):
        """选手信息，任意选手"""
        nickname = self._rest_after(event, {"player", "选手"}, nickname)
        if not nickname:
            yield event.plain_result("用法：/hltv player <选手昵称>，例如 /hltv player donk")
            return
        if tip := self._waiting_tip(event):
            yield tip
        try:
            data = await self.client.find_player(nickname)
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        try:
            await self.client.prepare_player_assets(data)
        except Exception as e:
            logger.warning(f"[hltv] 官方奖杯图片缓存失败，使用内置徽章: {e!r}")
        yield await self._image_or_text(
            event,
            render_player_card,
            formatter.format_player(data),
            data,
            log_name="选手卡片",
        )

    @hltv.command("news", alias={"新闻"})
    async def news(self, event: AstrMessageEvent, index: int = 0):
        """今日新闻，加序号看详情"""
        if tip := self._waiting_tip(event, NEWS_KEY if index <= 0 else None):
            yield tip
        try:
            items = await self.client.get_news()
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        if index > 0:
            if index > len(items):
                yield event.plain_result(
                    f"没有第 {index} 条新闻（今日共 {len(items)} 条）。"
                )
                return
            item = items[index - 1]
            try:
                detail = await self.client.get_news_detail(item["url"])
            except HltvError as e:
                yield event.plain_result(str(e))
                return
            original_title = detail.get("title") or item.get("title") or ""
            title = original_title
            paras = list(detail.get("paragraphs") or [])
            if not paras and item.get("desc"):
                paras = [str(item["desc"])]
            if self.translate_news:
                translated = await self._translate_to_chinese([title] + paras)
                title, paras = translated[0], translated[1:]
            yield event.plain_result(
                formatter.format_news_detail(
                    title, paras, item["url"], original_title=original_title
                )
            )
            return
        # 列表：只翻译将要展示的条目，且缓存过的不重复翻译
        shown = items[: self.max_items] if self.max_items > 0 else items
        if self.translate_news:
            pending = [
                it
                for it in shown
                if not self._is_chinese_translation(
                    str(it.get("title") or ""), str(it.get("title_zh") or "")
                )
            ]
            if pending:
                translated = await self._translate_to_chinese(
                    [str(it.get("title", "")) for it in pending]
                )
                for it, zh in zip(pending, translated):
                    source = str(it.get("title") or "")
                    if self._is_chinese_translation(source, zh):
                        it["title_zh"] = zh
                    else:
                        it.pop("title_zh", None)
        fallback = formatter.format_news(items, self.max_items)
        yield await self._image_or_text(
            event,
            render_news_card,
            fallback,
            shown,
            log_name="新闻卡片",
        )

    @staticmethod
    def _typo_hint(typo: str) -> str:
        close = difflib.get_close_matches(
            typo.lower(), _KNOWN_SUBCOMMANDS, n=1, cutoff=0.5
        )
        hint = (
            f"，你是想输入 /hltv {close[0]} 吗？"
            if close
            else "。发送 /hltv help 查看全部指令。"
        )
        return f"❓ 未知子指令「{typo}」{hint}"

    async def _dispatch(self, event: AstrMessageEvent, tokens: list[str]):
        """免 @ 模式的手动分发：token → 对应指令 handler。"""
        sub = tokens[1].lower() if len(tokens) > 1 else ""
        args = tokens[2:]

        def _int_arg(default: int = 0) -> int:
            try:
                return int(args[0])
            except (IndexError, ValueError):
                return default

        if not sub or sub in ("help", "帮助"):
            handler = self.help(event)
        elif sub in ("today", "今日", "今天", "今日赛程"):
            handler = self.today(event)
        elif sub in ("matches", "比赛", "大赛"):
            handler = self.matches(event, days=_int_arg())
        elif sub in ("live", "直播"):
            handler = self.live(event)  # 队名由 _rest_after 从原文提取
        elif sub in ("results", "赛果", "结果"):
            handler = self.results(event, days=_int_arg())
        elif sub in ("ranking", "排名", "排行"):
            handler = self.ranking(event, kind=args[0] if args else "")
        elif sub in ("top20", "年度榜单"):
            handler = self.top20(
                event,
                year=args[0] if args else "",
                rank=args[1] if len(args) > 1 else "",
            )
        elif sub in ("events", "赛事"):
            handler = self.events(event)
        elif sub in ("team", "战队"):
            handler = self.team(event)
        elif sub in ("player", "选手"):
            handler = self.player(event)
        elif sub in ("news", "新闻"):
            handler = self.news(event, index=_int_arg())
        elif sub in ("sub", "订阅"):
            handler = self.sub(event)
        elif sub in ("unsub", "退订", "取消订阅"):
            handler = self.unsub(event)
        else:
            yield event.plain_result(self._typo_hint(tokens[1]))
            return
        async for r in handler:
            yield r

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def catch_hltv_messages(self, event: AstrMessageEvent):
        """三件事：
        1. 将 /live 作为 /hltv live 的快捷入口；
        2. 免 @ 响应——群里没 @ 机器人、全局唤醒前缀也没命中时，
           以 /hltv 开头的消息由本插件自行分发（free_wake 配置可关）；
        3. 正常唤醒但子指令拼错时给纠错提示（框架对未知子指令静默）。
        合法指令走框架派发，这里会跳过，不会重复回复。"""
        tokens = (event.message_str or "").strip().split()
        if not tokens:
            return
        head = tokens[0].lower()
        woke = bool(getattr(event, "is_at_or_wake_command", False))
        if head == "/live" or (head == "live" and woke):
            if not self.free_wake and not woke:
                return
            async for r in self.live(event):
                yield r
            return
        if woke:
            # 已唤醒：唤醒前缀已被剥掉，首 token 是 "hltv"
            if head != "hltv" or len(tokens) < 2:
                return
            if tokens[1].lower() in _KNOWN_SUBCOMMANDS:
                return
            yield event.plain_result(self._typo_hint(tokens[1]))
            return
        # 未唤醒：只认 "/hltv" 开头，避免误伤普通聊天
        if not self.free_wake or head != "/hltv":
            return
        async for r in self._dispatch(event, tokens):
            yield r

    # ---------------------------------------------------------------- 订阅推送

    @hltv.command("sub", alias={"订阅"})
    async def sub(self, event: AstrMessageEvent):
        """在本会话订阅每日赛程推送"""
        umo = event.unified_msg_origin
        sessions = [s for s in (self.config.get("push_sessions") or []) if s]
        if umo in sessions:
            yield event.plain_result("本会话已订阅每日赛程推送。")
            return
        sessions.append(umo)
        self.config["push_sessions"] = sessions
        self.config.save_config()
        extra = "" if self.enable_push else "\n⚠️ 推送总开关未开启，请在 WebUI 插件配置中打开 enable_push。"
        yield event.plain_result(f"✅ 已订阅，每天 {self.push_time} 推送今日赛程。{extra}")

    @hltv.command("unsub", alias={"退订", "取消订阅"})
    async def unsub(self, event: AstrMessageEvent):
        """退订本会话的每日赛程推送"""
        umo = event.unified_msg_origin
        sessions = [s for s in (self.config.get("push_sessions") or []) if s]
        if umo not in sessions:
            yield event.plain_result("本会话没有订阅每日赛程推送。")
            return
        sessions.remove(umo)
        self.config["push_sessions"] = sessions
        self.config.save_config()
        yield event.plain_result("已退订每日赛程推送。")

    async def initialize(self):
        """AstrBot 在每次加载/重载插件时都会调用 initialize；
        on_astrbot_loaded 钩子只在进程启动时触发一次，WebUI 保存配置
        走的是 reload 路径，用它启动推送会在重载后永远失效。"""
        if self.enable_push and self._push_task is None:
            self._push_task = asyncio.create_task(self._push_loop())
            logger.info(f"[hltv] 每日推送已启动（{self.push_time}）")
        self._ensure_live_watch_task()

    def _seconds_until_push(self) -> float:
        h, m = self._push_hm
        now = self.client._now_local()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max((target - now).total_seconds(), 1.0)

    async def _push_loop(self):
        while True:
            try:
                await asyncio.sleep(self._seconds_until_push())
                await self._do_push()
                # 跨过当前分钟，避免同一分钟内重复触发
                await asyncio.sleep(61)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[hltv] 推送循环异常: {e!r}")
                await asyncio.sleep(300)

    async def _do_push(self):
        sessions = [s for s in (self.config.get("push_sessions") or []) if s]
        if not sessions:
            return
        try:
            data, note = await self._fetch_today_view()
        except HltvError as e:
            logger.warning(f"[hltv] 每日推送查询失败，本次跳过: {e}")
            return
        text = "⏰ HLTV 每日赛程\n" + formatter.format_today(
            data, self.max_items, self.min_stars, bool(self.event_keywords), note
        )
        for umo in sessions:
            try:
                delivered = await self.context.send_message(
                    umo, MessageChain().message(text)
                )
                if delivered is False:
                    logger.warning(f"[hltv] 未找到每日推送目标平台: {umo}")
            except Exception as e:
                logger.warning(f"[hltv] 推送到 {umo} 失败: {e!r}")

    async def _live_watch_loop(self):
        while True:
            try:
                waiting = await self._poll_live_subscriptions()
                interval = (
                    min(self.live_poll_interval, 10)
                    if waiting
                    else self.live_poll_interval
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[hltv] 直播提醒循环异常: {e!r}")
                await asyncio.sleep(self.live_poll_interval)

    async def _poll_live_subscriptions(self) -> bool:
        subscriptions = self.live_subscriptions.all()
        now = int(time())
        active = []
        for item in subscriptions:
            created_at = int(item.get("created_at") or now)
            expires_at = created_at + 12 * 60 * 60
            if item.get("pending_start") and int(item.get("start_unix") or 0):
                expires_at = int(item["start_unix"]) + 12 * 60 * 60
            if now >= expires_at:
                self.live_subscriptions.remove(item)
            else:
                active.append(item)
        grouped: dict[tuple[str, str], list[dict]] = {}
        for item in active:
            start_unix = int(item.get("start_unix") or 0)
            if item.get("pending_start") and start_unix > now + 10 * 60:
                continue
            key = (str(item.get("match_id") or ""), str(item.get("url") or ""))
            grouped.setdefault(key, []).append(item)

        for (match_id, url), subscribers in grouped.items():
            try:
                snapshot = await self.client.get_match_snapshot(match_id, url, watch=True)
            except HltvError as e:
                logger.warning(f"[hltv] 直播提醒查询失败({match_id}): {e}")
                continue
            for item in subscribers:
                if not self.live_subscriptions.contains(item):
                    continue
                updated, events, finished = advance_subscription(item, snapshot, now=now)
                if not events:
                    self.live_subscriptions.update(updated)
                    continue
                sent = True
                for notice in events:
                    kind = notice.get("kind")
                    if kind == "match_finished":
                        text = formatter.format_match_finished(snapshot)
                    elif kind == "map_finished":
                        text = formatter.format_map_rating(
                            snapshot, notice.get("map") or {}
                        )
                    else:
                        text = formatter.format_map_started(snapshot)
                    try:
                        chain = MessageChain().at(
                            str(item.get("user_name") or item.get("user_id") or ""),
                            str(item.get("user_id") or ""),
                        )
                        if kind in {"map_finished", "match_finished"}:
                            try:
                                card = await asyncio.to_thread(
                                    render_rating_card,
                                    snapshot,
                                    map_rating=(
                                        notice.get("map")
                                        if kind == "map_finished"
                                        else None
                                    ),
                                )
                                chain.message("\n").file_image(str(card))
                            except Exception as e:
                                logger.warning(
                                    f"[hltv] Rating 图片渲染失败，回退文本: {e!r}"
                                )
                                chain.message("\n" + text)
                        else:
                            chain.message("\n" + text)
                        delivered = await self.context.send_message(
                            str(item.get("umo") or ""), chain
                        )
                        if delivered is False:
                            sent = False
                            logger.warning(
                                f"[hltv] 未找到直播提醒目标平台({match_id}, "
                                f"{item.get('umo')})，保留订阅等待重试"
                            )
                            break
                    except Exception as e:
                        sent = False
                        logger.warning(
                            f"[hltv] 直播提醒发送失败({match_id}, {item.get('umo')}): {e!r}"
                        )
                        break
                if sent:
                    if finished:
                        self.live_subscriptions.remove(item)
                    else:
                        self.live_subscriptions.update(updated)
        return any(
            item.get("awaiting_map_start") for item in self.live_subscriptions.all()
        )

    # ---------------------------------------------------------------- 生命周期

    async def terminate(self):
        """插件卸载/停用时清理。"""
        tasks = [task for task in (self._push_task, self._live_watch_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._push_task = None
        self._live_watch_task = None
        self.client.clear_cache()
        logger.info("[hltv] 插件已卸载")
