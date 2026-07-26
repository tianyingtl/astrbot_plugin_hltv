"""astrbot_plugin_hltv — HLTV 查询插件入口。

分层：
    main.py            指令注册与参数解析（本文件）
    core/client.py     HLTV 数据访问（缓存、限流、异常归一化）
    core/formatter.py  数据 → 文本消息
"""

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.client import HltvClient, HltvError
from .core import formatter


class HltvPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_items = int(config.get("max_items", 10))
        # HLTV 星级门槛（0-5）：只显示 ≥ 该星级的比赛，0 = 不过滤
        self.min_stars = max(0, min(int(config.get("min_stars", 1)), 5))
        # 大赛关键词白名单（空 = 不启用），对 matches / today 生效
        self.event_keywords = [
            str(k).strip().lower() for k in (config.get("event_keywords") or []) if str(k).strip()
        ]
        self.default_days = max(1, min(int(config.get("default_days", 1)), 7))
        self.send_waiting_tip = bool(config.get("send_waiting_tip", False))
        self.client = HltvClient(
            proxy_list=[p for p in (config.get("proxy_list") or []) if p],
            timeout=int(config.get("timeout", 15)),
            max_retries=int(config.get("max_retries", 3)),
            tz=str(config.get("timezone", "Asia/Shanghai")),
            cache_ttl=int(config.get("cache_ttl", 300)),
        )

    # ------------------------------------------------------------------ 指令组

    @staticmethod
    def _rest_after(event: AstrMessageEvent, subcmds: set[str], fallback: str) -> str:
        """取消息中子指令（含别名）之后的整段文本。

        AstrBot 的 str 指令参数按空格只绑定一个词，"/hltv team natus vincere"
        只会绑到 "natus"；这里从原始消息里把子指令后面的部分整体取出，
        让含空格的战队名/选手名也能查。
        """
        tokens = (event.message_str or "").split()
        for i, tok in enumerate(tokens):
            if tok.lower() in subcmds:
                rest = " ".join(tokens[i + 1 :]).strip()
                return rest or fallback
        return fallback

    def _filter_by_event(self, matches: list[dict]) -> list[dict]:
        """按配置的大赛关键词白名单过滤（未配置时原样返回）。"""
        if not self.event_keywords:
            return matches
        return [
            m
            for m in matches
            if any(k in str(m.get("event", "")).lower() for k in self.event_keywords)
        ]

    def _waiting_tip(self, event: AstrMessageEvent):
        """查询前的等待提示（可在 WebUI 配置中开关）。"""
        if self.send_waiting_tip:
            return event.plain_result("🔎 正在查询 HLTV，稍等…")
        return None

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
        if tip := self._waiting_tip(event):
            yield tip
        try:
            data = await self.client.get_today_matches(min_stars=self.min_stars)
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        data = self._filter_by_event(data)
        yield event.plain_result(
            formatter.format_today(data, self.max_items, self.min_stars)
        )

    @hltv.command("matches", alias={"比赛", "大赛"})
    async def matches(self, event: AstrMessageEvent, days: int = 0):
        """近期大赛，可带天数"""
        days = days if days > 0 else self.default_days
        days = min(days, 7)
        if tip := self._waiting_tip(event):
            yield tip
        try:
            data = await self.client.get_matches(days=days, min_stars=self.min_stars)
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        data = self._filter_by_event(data)
        yield event.plain_result(
            formatter.format_matches(data, days, self.max_items, self.min_stars)
        )

    @hltv.command("live", alias={"直播"})
    async def live(self, event: AstrMessageEvent):
        """正在进行的比赛"""
        if tip := self._waiting_tip(event):
            yield tip
        try:
            data = await self.client.get_live_matches()
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        yield event.plain_result(formatter.format_live(data))

    @hltv.command("results", alias={"赛果", "结果"})
    async def results(self, event: AstrMessageEvent, days: int = 0):
        """近期赛果，可带天数"""
        days = days if days > 0 else self.default_days
        days = min(days, 7)
        if tip := self._waiting_tip(event):
            yield tip
        try:
            data = await self.client.get_results(days=days)
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        yield event.plain_result(formatter.format_results(data, days, self.max_items))

    @hltv.command("ranking", alias={"排名", "排行"})
    async def ranking(self, event: AstrMessageEvent):
        """战队世界排名 Top50"""
        if tip := self._waiting_tip(event):
            yield tip
        try:
            data = await self.client.get_top_teams(50)
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        yield event.plain_result(formatter.format_ranking(data, self.max_items))

    @hltv.command("events", alias={"赛事"})
    async def events(self, event: AstrMessageEvent):
        """近期赛事"""
        if tip := self._waiting_tip(event):
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
    async def news(self, event: AstrMessageEvent):
        """今日新闻"""
        if tip := self._waiting_tip(event):
            yield tip
        try:
            data = await self.client.get_news()
        except HltvError as e:
            yield event.plain_result(str(e))
            return
        yield event.plain_result(formatter.format_news(data, self.max_items))

    # ---------------------------------------------------------------- 生命周期

    async def terminate(self):
        """插件卸载/停用时清理缓存。"""
        self.client.clear_cache()
        logger.info("[hltv] 插件已卸载")
