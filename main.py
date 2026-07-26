"""astrbot_plugin_hltv — HLTV 查询插件入口。

分层：
    main.py             指令注册、参数解析、翻译/推送编排（本文件）
    core/client.py      HLTV 数据访问（缓存、限流、自建解析器）
    core/formatter.py   数据 → 文本消息
    core/translator.py  微软翻译（免费 Edge 通道）
"""

import asyncio
import difflib
from datetime import timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from .core import formatter
from .core.client import (
    EVENTS_KEY,
    MATCHES_RAW_KEY,
    NEWS_KEY,
    RANKING_HLTV_KEY,
    VRS_REGIONS,
    HltvClient,
    HltvError,
    results_key,
    vrs_key,
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
        self.translate_news = bool(config.get("translate_news", True))
        self.enable_push = bool(config.get("enable_push", False))
        self._push_hm = self._parse_push_time(str(config.get("push_time", "09:00")))
        # 展示用的时间取解析结果，配置串无效回退 09:00 时不给用户看原始串
        self.push_time = "{:02d}:{:02d}".format(*self._push_hm)
        self._push_task: asyncio.Task | None = None

        self.client = HltvClient(
            proxy_list=[p for p in (config.get("proxy_list") or []) if p],
            timeout=int(config.get("timeout", 15)),
            max_retries=int(config.get("max_retries", 3)),
            tz=str(config.get("timezone", "Asia/Shanghai")),
            cache_ttl=int(config.get("cache_ttl", 300)),
        )
        self.translator = Translator()

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
        yield event.plain_result(
            formatter.format_today(
                data, self.max_items, self.min_stars, bool(self.event_keywords), note
            )
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
        yield event.plain_result(
            formatter.format_matches(
                data, days, self.max_items, self.min_stars, bool(self.event_keywords), note
            )
        )

    @hltv.command("live", alias={"直播"})
    async def live(self, event: AstrMessageEvent):
        """正在进行的比赛（默认只看大赛，带比分）"""
        if tip := self._waiting_tip(event, MATCHES_RAW_KEY):
            yield tip
        try:
            data = self._filter_by_event(
                await self.client.get_live_matches(min_stars=self.min_stars)
            )
            note = ""
            if not data and (self.min_stars > 0 or self.event_keywords):
                alt = await self.client.get_live_matches()
                if alt:
                    data, note = alt, "（当前无大赛直播，已显示全部场次）"
            # 延迟场次：过了开赛时间但 HLTV 还没标 live 的比赛,
            # 不显示会让用户以为比赛消失了
            delayed = self._filter_by_event(
                await self.client.get_delayed_matches(min_stars=self.min_stars)
            )
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        # 比分在单场详情页,逐场抓取代价高,只取前几场
        for m in data[:4]:
            try:
                maps = await self.client.get_live_score(m.get("id"), m.get("url", ""))
            except HltvError:
                continue
            m.update(self.client.summarize_map_scores(maps))
        yield event.plain_result(formatter.format_live(data, note, delayed))

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
        yield event.plain_result(formatter.format_results(data, days, self.max_items))

    @hltv.command("ranking", alias={"排名", "排行"})
    async def ranking(self, event: AstrMessageEvent, kind: str = ""):
        """排名：默认 Valve VRS，可加地区或 hltv"""
        arg = kind.strip().lower()
        if arg in ("hltv", "h"):
            use_vrs, region = False, None
            key, title = RANKING_HLTV_KEY, "🏆 HLTV 战队排名 Top50"
        elif arg in ("", "v", "vrs", "valve"):
            use_vrs, region = True, None
            key, title = vrs_key(None), "🏆 Valve VRS 排名（全球）"
        elif arg in VRS_REGIONS:
            use_vrs, region = True, VRS_REGIONS[arg]
            key = vrs_key(region)
            title = f"🏆 Valve VRS 排名（{_REGION_CN.get(region, region)}）"
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
        yield event.plain_result(
            formatter.format_ranking(
                data, self.max_items, title, show_region=(use_vrs and region is None)
            )
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
        yield event.plain_result(formatter.format_events(data, self.max_items))

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
        yield event.plain_result(formatter.format_team(data))

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
        yield event.plain_result(formatter.format_player(data))

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
            title = detail.get("title") or item.get("title") or ""
            paras = list(detail.get("paragraphs") or [])
            if not paras and item.get("desc"):
                paras = [str(item["desc"])]
            if self.translate_news:
                translated = await self.translator.translate([title] + paras)
                title, paras = translated[0], translated[1:]
            yield event.plain_result(
                formatter.format_news_detail(title, paras, item["url"])
            )
            return
        # 列表：只翻译将要展示的条目，且缓存过的不重复翻译
        shown = items[: self.max_items] if self.max_items > 0 else items
        if self.translate_news:
            pending = [it for it in shown if not it.get("title_zh")]
            if pending:
                translated = await self.translator.translate(
                    [str(it.get("title", "")) for it in pending]
                )
                for it, zh in zip(pending, translated):
                    it["title_zh"] = zh
        yield event.plain_result(formatter.format_news(items, self.max_items))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def unknown_subcommand_hint(self, event: AstrMessageEvent):
        """拼错子指令时给出纠错提示（如 hltv new → news）。

        框架对"组名对、子指令不认识"的消息不派发任何 handler，
        用户只会看到沉默；这里兜底。合法子指令会被前面的 return 跳过，
        不会造成重复回复。"""
        if not getattr(event, "is_at_or_wake_command", False):
            return
        tokens = (event.message_str or "").strip().split()
        if len(tokens) < 2 or tokens[0].lower() != "hltv":
            return  # 非本插件消息；裸 /hltv 由框架自动回复指令树
        if tokens[1].lower() in _KNOWN_SUBCOMMANDS:
            return
        typo = tokens[1]
        close = difflib.get_close_matches(
            typo.lower(), _KNOWN_SUBCOMMANDS, n=1, cutoff=0.5
        )
        hint = (
            f"，你是想输入 /hltv {close[0]} 吗？"
            if close
            else "。发送 /hltv help 查看全部指令。"
        )
        yield event.plain_result(f"❓ 未知子指令「{typo}」{hint}")

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
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as e:
                logger.warning(f"[hltv] 推送到 {umo} 失败: {e!r}")

    # ---------------------------------------------------------------- 生命周期

    async def terminate(self):
        """插件卸载/停用时清理。"""
        if self._push_task is not None:
            self._push_task.cancel()
            self._push_task = None
        self.client.clear_cache()
        logger.info("[hltv] 插件已卸载")
