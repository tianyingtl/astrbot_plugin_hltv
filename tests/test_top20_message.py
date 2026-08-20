import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]


def _load_main_module():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")

    class MessageChain:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(("text", text))
            return self

        def at(self, name, user_id):
            self.chain.append(("at", (name, user_id)))
            return self

        def file_image(self, path):
            self.chain.append(("image", path))
            return self

    class Filter:
        EventMessageType = types.SimpleNamespace(ALL="all")

        @staticmethod
        def command_group(*_args, **_kwargs):
            def decorate(function):
                function.command = lambda *_a, **_kw: lambda child: child
                return function

            return decorate

        @staticmethod
        def event_message_type(*_args, **_kwargs):
            return lambda function: function

        @staticmethod
        def llm_tool(name=None, **_kwargs):
            def decorate(function):
                function.llm_tool_name = name or function.__name__
                return function

            return decorate

    class Star:
        def __init__(self, context):
            self.context = context

    api.AstrBotConfig = dict
    api.logger = types.SimpleNamespace(
        error=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    event_module.AstrMessageEvent = object
    event_module.MessageChain = MessageChain
    event_module.filter = Filter
    star_module.Context = object
    star_module.Star = Star
    astrbot.api = api
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module

    package_name = "_hltv_plugin_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", ROOT / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Top20MessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_news_and_image_are_sent_in_one_message(self):
        module = _load_main_module()

        class Client:
            @staticmethod
            def latest_top20_year():
                return 2025

            @staticmethod
            async def get_top20_player(year, rank):
                return {
                    "year": year,
                    "rank": rank,
                    "name": "NiKo",
                    "title": "Top 20 players of 2025: NiKo (18)",
                    "description": "Season summary.",
                    "url": "https://www.hltv.org/news/43505/example",
                    "image_path": Path("official.jpg"),
                }

        class Event:
            @staticmethod
            def plain_result(text):
                return [("text", text)]

            @staticmethod
            def chain_result(chain):
                return chain

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.client = Client()
        plugin.send_waiting_tip = False
        plugin.translate_news = False

        results = [
            result
            async for result in plugin.top20(Event(), year="2025", rank="18")
        ]

        self.assertEqual(len(results), 1)
        self.assertEqual([kind for kind, _value in results[0]], ["text", "image"])
        self.assertIn("Top 20 players of 2025", results[0][0][1])
        self.assertEqual(results[0][1], ("image", "official.jpg"))


class HltvKnowledgeToolTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _plugin(module, client):
        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.client = client
        plugin.max_items = 10
        plugin.default_days = 1
        return plugin

    def test_tool_is_registered_with_two_documented_arguments(self):
        module = _load_main_module()

        self.assertEqual(module.HltvPlugin.query_hltv.llm_tool_name, "query_hltv")
        doc = module.HltvPlugin.query_hltv.__doc__ or ""
        self.assertIn("category(string)", doc)
        self.assertIn("query(string)", doc)
        self.assertIn("必须优先调用本工具", doc)
        self.assertIn("赛事名称/简称", doc)

    async def test_live_query_uses_fresh_snapshot_and_plain_text(self):
        module = _load_main_module()

        class Client:
            async def get_live_matches(self):
                return [
                    {
                        "id": 1,
                        "url": "https://www.hltv.org/matches/1/test",
                        "team1": "Natus Vincere",
                        "team2": "Vitality",
                        "event": "IEM Test",
                        "best_of": "BO3",
                    }
                ]

            async def get_live_snapshot(self, _match):
                return {
                    "maps_score": "1:0",
                    "current_map_name": "Nuke",
                    "current_score": "8:4",
                }

        text = await self._plugin(module, Client()).query_hltv(
            object(), category="live", query="NAVI"
        )

        self.assertIn("小局  Nuke   Natus Vincere 8:4 Vitality", text)
        self.assertIn("大局  BO3  Natus Vincere 1:0 Vitality", text)

    async def test_text_tool_routes_all_reference_categories(self):
        module = _load_main_module()

        class Client:
            @staticmethod
            def latest_top20_year():
                return 2025

            async def get_matches(self, days=1):
                self.schedule_days = days
                return [
                    {
                        "date": "15-08-2026",
                        "time": "18:00",
                        "team1": "Natus Vincere",
                        "team2": "Vitality",
                        "event": "IEM Test",
                    },
                    {
                        "date": "15-08-2026",
                        "time": "20:00",
                        "team1": "Spirit",
                        "team2": "Liquid",
                        "event": "Other Event",
                    },
                ]

            async def get_results(self, days=1):
                self.result_days = days
                return [
                    {
                        "date": "14-08-2026",
                        "team1": "Natus Vincere",
                        "team2": "Vitality",
                        "score1": 2,
                        "score2": 1,
                        "event": "IEM Test",
                    }
                ]

            async def get_top_teams(self, _limit):
                return [{"rank": 1, "title": "Vitality", "points": 1000}]

            async def get_events(self):
                return [
                    {"title": "IEM Test", "start_date": "15-08", "end_date": "20-08"},
                    {"title": "BLAST Test", "start_date": "21-08", "end_date": "23-08"},
                ]

            async def find_team(self, name):
                return {"title": name, "world_rank": 1, "players": []}

            async def find_player(self, nickname):
                return {"nickname": nickname, "name": "Test Player", "team": "Test Team"}

            async def get_news(self):
                return [{"title": "NAVI win IEM Test", "url": "https://www.hltv.org/news/1/test"}]

            async def get_news_detail(self, _url):
                return {"title": "NAVI win IEM Test", "paragraphs": ["Match report."]}

            async def get_top20_players(self, year):
                return [{"rank": rank, "name": f"Player {rank}"} for rank in range(1, 21)]

            async def get_top20_player(self, year, rank):
                return {
                    "title": f"Top 20 players of {year}: NiKo ({rank})",
                    "description": "Season summary.",
                    "url": "https://www.hltv.org/news/1/top20",
                }

        client = Client()
        plugin = self._plugin(module, client)

        event = object()
        schedule = await plugin.query_hltv(event, "schedule", "NAVI 7天")
        results = await plugin.query_hltv(event, "results", "NAVI 3天")
        ranking = await plugin.query_hltv(event, "ranking", "hltv")
        events = await plugin.query_hltv(event, "events", "IEM")
        team = await plugin.query_hltv(event, "team", "NAVI")
        player = await plugin.query_hltv(event, "player", "NiKo")
        news = await plugin.query_hltv(event, "news", "1")
        top20 = await plugin.query_hltv(event, "top20", "2025")
        top20_player = await plugin.query_hltv(event, "top20", "2025 18")

        self.assertEqual(client.schedule_days, 7)
        self.assertIn("Natus Vincere", schedule)
        self.assertNotIn("Spirit", schedule)
        self.assertEqual(client.result_days, 3)
        self.assertIn("Natus Vincere 2 : 1 Vitality", results)
        self.assertIn("#1 Vitality", ranking)
        self.assertIn("IEM Test", events)
        self.assertNotIn("BLAST Test", events)
        self.assertIn("HLTV #1", team)
        self.assertIn("NiKo", player)
        self.assertIn("Match report.", news)
        self.assertIn("#01  Player 1", top20)
        self.assertIn("NiKo (18)", top20_player)

    async def test_match_queries_accept_event_aliases(self):
        module = _load_main_module()

        class Client:
            async def get_matches(self, days=1):
                return [
                    {
                        "date": "15-08-2026",
                        "time": "18:00",
                        "team1": "TYLOO",
                        "team2": "Lynn Vision",
                        "event": "Esports World Cup 2026",
                    },
                    {
                        "date": "15-08-2026",
                        "time": "20:00",
                        "team1": "Natus Vincere",
                        "team2": "Vitality",
                        "event": "IEM Cologne 2026",
                    },
                ]

            async def get_results(self, days=1):
                return [
                    {
                        "date": "14-08-2026",
                        "team1": "Spirit",
                        "team2": "Liquid",
                        "score1": 2,
                        "score2": 1,
                        "event": "Esports World Cup 2026",
                    },
                    {
                        "date": "14-08-2026",
                        "team1": "MOUZ",
                        "team2": "Falcons",
                        "score1": 0,
                        "score2": 2,
                        "event": "BLAST Open 2026",
                    },
                ]

            async def get_live_matches(self):
                return [
                    {
                        "id": 1,
                        "team1": "Ninjas in Pyjamas",
                        "team2": "BetBoom",
                        "event": "Esports World Cup 2026",
                        "best_of": "BO3",
                    },
                    {
                        "id": 2,
                        "team1": "G2",
                        "team2": "FaZe",
                        "event": "IEM Cologne 2026",
                        "best_of": "BO3",
                    },
                ]

            async def get_live_snapshot(self, match):
                return {
                    "maps_score": "0:0",
                    "current_map_name": "Mirage",
                    "current_score": "5:3",
                }

        plugin = self._plugin(module, Client())
        event = object()

        schedule = await plugin.query_hltv(event, "schedule", "EWC")
        schedule_cn = await plugin.query_hltv(event, "schedule", "电竞世界杯")
        results = await plugin.query_hltv(event, "results", "EWC")
        live = await plugin.query_hltv(event, "live", "EWC")

        for text in (schedule, schedule_cn, results, live):
            self.assertIn("Esports World Cup 2026", text)
        self.assertNotIn("IEM Cologne 2026", schedule)
        self.assertNotIn("BLAST Open 2026", results)
        self.assertNotIn("G2", live)

    async def test_missing_category_falls_back_to_player_lookup(self):
        module = _load_main_module()

        class Client:
            async def find_player(self, nickname):
                return {
                    "nickname": nickname,
                    "name": "Nikola Kovač",
                    "team": "Falcons",
                    "top20": [{"year": 2025, "rank": 18}],
                }

        text = await self._plugin(module, Client()).query_hltv(
            object(), query="NiKo"
        )

        self.assertIn("NiKo", text)
        self.assertIn("2025 #18", text)


class LiveCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_spoiler_delay_names_dispatch_to_the_same_command(self):
        module = _load_main_module()
        calls = []

        class Event:
            @staticmethod
            def plain_result(text):
                return text

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)

        async def fake_antijutou(_event, minutes=""):
            calls.append(minutes)
            yield "ok"

        plugin.antijutou = fake_antijutou
        for name in ("防剧透", "antijutou"):
            results = [
                result
                async for result in plugin._dispatch(
                    Event(), ["/hltv", name, "12.75"]
                )
            ]
            self.assertEqual(results, ["ok"])

        self.assertEqual(calls, ["12.75", "12.75"])

    def test_spoiler_delay_parser_accepts_any_number_without_units(self):
        module = _load_main_module()

        self.assertEqual(module.HltvPlugin._parse_spoiler_minutes("0.5"), 0.5)
        self.assertEqual(module.HltvPlugin._parse_spoiler_minutes("20"), 20)
        self.assertEqual(module.HltvPlugin._parse_spoiler_minutes("-2"), -2)
        self.assertEqual(module.HltvPlugin._parse_spoiler_minutes("12.75"), 12.75)
        self.assertIsNone(module.HltvPlugin._parse_spoiler_minutes("20 min"))
        self.assertIsNone(module.HltvPlugin._parse_spoiler_minutes("-2分钟"))
        self.assertIsNone(module.HltvPlugin._parse_spoiler_minutes("NaN"))
        self.assertIsNone(module.HltvPlugin._parse_spoiler_minutes("Infinity"))

    async def test_spoiler_delay_command_adjusts_current_event_and_reports_total(self):
        module = _load_main_module()

        class Store:
            @staticmethod
            def all():
                return [
                    {"event": "Esports World Cup 2026", "user_id": "1"},
                    {"event": "Esports World Cup 2026", "user_id": "2"},
                ]

        class Event:
            message_str = "/hltv 防剧透 20"

            @staticmethod
            def plain_result(text):
                return text

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.live_subscriptions = Store()
        with tempfile.TemporaryDirectory() as temp:
            plugin.spoiler_delays = module.SpoilerDelayStore(
                Path(temp) / "spoiler-delays.json"
            )
            results = [result async for result in plugin.antijutou(Event(), "20")]
            self.assertIn("当前额外延迟：20 分钟", results[0])
            self.assertIn("数据出现 21 分钟后推送", results[0])
            self.assertEqual(
                plugin._rating_delay_seconds("Esports World Cup 2026"), 1260
            )
            self.assertEqual(plugin._rating_delay_seconds("Other Event 2026"), 60)

            Event.message_str = "/hltv antijutou -2"
            results = [result async for result in plugin.antijutou(Event(), "-2")]
            self.assertIn("当前额外延迟：18 分钟", results[0])
            self.assertIn("数据出现 19 分钟后推送", results[0])

            Event.message_str = "/hltv 防剧透 -100"
            results = [result async for result in plugin.antijutou(Event(), "-100")]
            self.assertIn("当前额外延迟：0 分钟", results[0])
            self.assertIn("数据出现 1 分钟后推送", results[0])

    async def test_spoiler_delay_command_rejects_missing_or_multiple_events(self):
        module = _load_main_module()

        class Event:
            message_str = "/hltv 防剧透 20"

            @staticmethod
            def plain_result(text):
                return text

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.live_subscriptions = types.SimpleNamespace(all=lambda: [])
        results = [result async for result in plugin.antijutou(Event(), "20")]
        self.assertIn("没有正在追踪的赛事", results[0])

        plugin.live_subscriptions = types.SimpleNamespace(
            all=lambda: [{"event": "Event A"}, {"event": "Event B"}]
        )
        results = [result async for result in plugin.antijutou(Event(), "20")]
        self.assertIn("同时追踪多个赛事", results[0])

    async def test_live_command_outputs_snapshot_small_and_series_scores(self):
        module = _load_main_module()

        class Client:
            async def get_live_matches(self, min_stars=0):
                return [
                    {
                        "id": 1,
                        "url": "https://www.hltv.org/matches/1/test",
                        "team1": "Team A",
                        "team2": "Team B",
                        "event": "Test Event",
                        "rating": 5,
                        "best_of": "BO3",
                    }
                ]

            async def get_live_snapshot(self, match):
                return {
                    "maps_score": "1:1",
                    "current_map": "Nuke 18:17",
                    "current_map_name": "Nuke",
                    "current_score": "18:17",
                }

            async def get_delayed_matches(self, min_stars=0):
                return []

            async def get_matches(self, days=1, min_stars=0):
                return []

        class Event:
            message_str = "/hltv live"

            @staticmethod
            def plain_result(text):
                return text

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.client = Client()
        plugin.send_waiting_tip = False
        plugin.min_stars = 0
        plugin.event_keywords = []

        with patch.object(
            module, "render_live_card", side_effect=RuntimeError("force text fallback")
        ):
            results = [result async for result in plugin.live(Event())]

        self.assertEqual(len(results), 1)
        self.assertIn("小局  Nuke   Team A 18:17 Team B", results[0])
        self.assertIn("大局  BO3  Team A 1:1 Team B", results[0])
        self.assertLess(results[0].index("小局"), results[0].index("大局"))

    async def test_live_numbers_subscribe_matches_from_the_shown_list(self):
        module = _load_main_module()

        class Client:
            async def get_live_matches(self, min_stars=0):
                return [
                    {
                        "id": index,
                        "url": f"https://www.hltv.org/matches/{index}/test",
                        "team1": f"Team {index}A",
                        "team2": f"Team {index}B",
                        "event": "Test Event",
                        "rating": 3,
                        "live": True,
                    }
                    for index in range(1, 5)
                ]

            async def get_live_snapshot(self, match):
                return {
                    "status": "live",
                    "active_map_index": 1,
                    "current_map_name": "Nuke",
                    "current_score": "3:2",
                    "maps_score": "0:0",
                    "map_ratings": [],
                }

            async def get_delayed_matches(self, min_stars=0):
                return []

            async def get_matches(self, days=1, min_stars=0):
                return []

        class Event:
            unified_msg_origin = "group:1"

            def __init__(self, message):
                self.message_str = message

            @staticmethod
            def get_sender_id():
                return "42"

            @staticmethod
            def get_sender_name():
                return "Chiaki"

            @staticmethod
            def plain_result(text):
                return text

            @staticmethod
            def image_result(path):
                return ("image", path)

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.client = Client()
        plugin.send_waiting_tip = False
        plugin.min_stars = 0
        plugin.event_keywords = []
        plugin._live_selection_cache = {}
        plugin._ensure_live_watch_task = lambda: None

        with tempfile.TemporaryDirectory() as temp:
            plugin.live_subscriptions = module.LiveSubscriptionStore(
                Path(temp) / "subscriptions.json"
            )
            with patch.object(module, "render_live_card", return_value=Path("live.png")):
                listing = [result async for result in plugin.live(Event("/hltv live"))]
                subscribed = [
                    result
                    async for result in plugin.live(Event("/hltv live 1 3"))
                ]
            with patch.object(
                module,
                "render_live_detail_card",
                side_effect=RuntimeError("force text fallback"),
            ):
                team_subscribed = [
                    result
                    async for result in plugin.live(Event("/hltv live Team 2A"))
                ]

            self.assertEqual(listing[0], ("image", "live.png"))
            self.assertEqual(len(listing), 2)
            self.assertIn("/hltv live 1 2 3", listing[1])
            self.assertIn("/hltv live 战队名", listing[1])
            self.assertIn("想订阅比赛", listing[1])
            self.assertIn("想看某场实时战绩或订阅指定战队", listing[1])
            self.assertIn("已订阅 2 场", subscribed[0])
            self.assertIn("Team 1A vs Team 1B", subscribed[0])
            self.assertIn("Team 3A vs Team 3B", subscribed[0])
            self.assertIn("已订阅 1 场", team_subscribed[0])
            self.assertEqual(
                [item["match_id"] for item in plugin.live_subscriptions.all()],
                ["1", "3", "2"],
            )

    async def test_live_team_uses_detail_snapshot_and_single_detail_card(self):
        module = _load_main_module()
        match = {
            "id": "123",
            "url": "https://www.hltv.org/matches/123/test",
            "team1": "Spirit",
            "team2": "JiJieHao",
            "event": "EWC",
            "live": True,
        }
        snapshot = {
            **match,
            "status": "live",
            "best_of": "BO3",
            "current_map_name": "Mirage",
            "current_score": "9:10",
            "maps_score": "0:0",
            "active_map_index": 1,
            "maps": [
                {"map": "Mirage", "played": True, "finished": False, "ordinal": 1},
                {"map": "Nuke", "played": False, "finished": False, "ordinal": 2},
                {"map": "Ancient", "played": False, "finished": False, "ordinal": 3},
            ],
            "live_stats": [
                {"team_id": "10", "players": [{"nickname": "donk", "kd": "20-13"}]},
                {"team_id": "20", "players": [{"nickname": "sinnopsyy", "kd": "15-13"}]},
            ],
            "map_ratings": [],
        }

        class Client:
            get_match_snapshot = AsyncMock(return_value=snapshot)

            async def get_live_matches(self, min_stars=0):
                return [dict(match)]

            async def get_live_snapshot(self, _match):
                raise AssertionError("detail snapshot should be used")

        class Event:
            message_str = "/hltv live Spirit"
            unified_msg_origin = "group:1"

            @staticmethod
            def get_sender_id():
                return "42"

            @staticmethod
            def get_sender_name():
                return "Chiaki"

            @staticmethod
            def plain_result(text):
                return text

            @staticmethod
            def image_result(path):
                return ("image", path)

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.client = Client()
        plugin.send_waiting_tip = False
        plugin._ensure_live_watch_task = lambda: None

        with tempfile.TemporaryDirectory() as temp:
            plugin.live_subscriptions = module.LiveSubscriptionStore(
                Path(temp) / "subscriptions.json"
            )
            with patch.object(
                module, "render_live_detail_card", return_value=Path("detail.png")
            ) as render:
                results = [result async for result in plugin.live(Event())]

        self.assertEqual(results, [("image", "detail.png")])
        plugin.client.get_match_snapshot.assert_awaited_once_with(
            "123", match["url"], watch=True
        )
        rendered = render.call_args.args[0]
        self.assertEqual(len(rendered["maps"]), 3)
        self.assertEqual(len(rendered["live_stats"]), 2)
        self.assertEqual(len(plugin.live_subscriptions.all()), 1)

    async def test_plain_live_only_lists_active_matches_without_subscribing(self):
        module = _load_main_module()

        class Client:
            async def get_live_matches(self, min_stars=0):
                return []

            async def get_delayed_matches(self, min_stars=0):
                raise AssertionError("plain /live must not query delayed matches")

            async def get_matches(self, days=1, min_stars=0):
                raise AssertionError("plain /live must not query upcoming matches")

        class Event:
            message_str = "/live"
            unified_msg_origin = "group:1"

            @staticmethod
            def get_sender_id():
                return "42"

            @staticmethod
            def get_sender_name():
                return "Chiaki"

            @staticmethod
            def plain_result(text):
                return text

            @staticmethod
            def image_result(path):
                return ("image", path)

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.client = Client()
        plugin.send_waiting_tip = False
        plugin.min_stars = 0
        plugin.event_keywords = []
        plugin.max_items = 10
        plugin._ensure_live_watch_task = lambda: None

        with tempfile.TemporaryDirectory() as temp:
            plugin.live_subscriptions = module.LiveSubscriptionStore(
                Path(temp) / "subscriptions.json"
            )
            with patch.object(
                module, "render_live_card", return_value=Path("live.png")
            ):
                results = [result async for result in plugin.live(Event())]

            self.assertEqual(results, [("image", "live.png")])
            self.assertEqual(plugin.live_subscriptions.all(), [])

    async def test_live_team_subscribes_its_upcoming_match(self):
        module = _load_main_module()
        upcoming = {
            "id": "9001",
            "url": "https://www.hltv.org/matches/9001/test",
            "team1": "Fluxo",
            "team2": "fnatic",
            "event": "IEM Test",
            "rating": 3,
            "unix": int(module.time()) + 3600,
            "date": "09-08-2026",
            "time": "17:00",
            "live": False,
            "late": False,
        }

        class Client:
            async def get_live_matches(self, min_stars=0):
                return []

            async def get_matches(self, days=1, min_stars=0):
                return [upcoming]

        class Event:
            message_str = "/live FNC"
            unified_msg_origin = "group:1"

            @staticmethod
            def get_sender_id():
                return "42"

            @staticmethod
            def get_sender_name():
                return "Chiaki"

            @staticmethod
            def plain_result(text):
                return text

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.client = Client()
        plugin.send_waiting_tip = False
        plugin._ensure_live_watch_task = lambda: None

        with tempfile.TemporaryDirectory() as temp:
            plugin.live_subscriptions = module.LiveSubscriptionStore(
                Path(temp) / "subscriptions.json"
            )
            results = [result async for result in plugin.live(Event())]

            self.assertEqual(len(results), 1)
            self.assertIn("已订阅这场比赛", results[0])
            item = plugin.live_subscriptions.all()[0]
            self.assertEqual(item["match_id"], "9001")
            self.assertTrue(item["pending_start"])

    async def test_slash_live_is_dispatched_as_a_shortcut(self):
        module = _load_main_module()

        class Event:
            message_str = "/live"
            is_at_or_wake_command = False

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.free_wake = True

        async def fake_live(_event):
            yield "shortcut"

        plugin.live = fake_live
        results = [result async for result in plugin.catch_hltv_messages(Event())]

        self.assertEqual(results, ["shortcut"])

    def test_live_team_filter_uses_short_and_chinese_aliases(self):
        module = _load_main_module()
        match = {"team1": "Natus Vincere", "team2": "Vitality"}

        for query in ("NAVI", "蜜蜂", "小蜜蜂"):
            with self.subTest(query=query):
                self.assertTrue(module.HltvPlugin._team_query_match(query, match))
        self.assertFalse(module.HltvPlugin._team_query_match("NIP", match))

    async def test_far_upcoming_subscription_is_not_polled_early(self):
        module = _load_main_module()
        now = int(module.time())

        class Store:
            def __init__(self):
                self.item = {
                    "match_id": "9001",
                    "url": "/matches/9001/test",
                    "umo": "group:1",
                    "user_id": "42",
                    "created_at": now,
                    "pending_start": True,
                    "start_unix": now + 3600,
                }

            def all(self):
                return [self.item]

            def remove(self, _item):
                raise AssertionError("future subscription must be kept")

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.live_subscriptions = Store()
        plugin.client = types.SimpleNamespace(get_match_snapshot=AsyncMock())

        waiting = await plugin._poll_live_subscriptions()

        self.assertFalse(waiting)
        plugin.client.get_match_snapshot.assert_not_awaited()

    async def test_watch_loop_polls_faster_while_waiting_for_map_start(self):
        module = _load_main_module()
        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.live_poll_interval = 45
        plugin._poll_live_subscriptions = AsyncMock(return_value=True)

        sleep = AsyncMock(side_effect=asyncio.CancelledError)
        with patch.object(module.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await plugin._live_watch_loop()

        sleep.assert_awaited_once_with(10)

    async def test_rating_notices_send_at_and_image_in_one_chain(self):
        module = _load_main_module()
        snapshot = {
            "status": "finished",
            "best_of": "BO3",
            "team1": "FaZe",
            "team2": "Spirit",
            "event": "Test Event",
            "maps_score": "1:2",
            "rating_version": "3.0",
            "map_ratings": [
                {
                    "index": 3,
                    "map": "Ancient",
                    "score": "9:13",
                    "rating_version": "3.0",
                    "ratings": [{"team": "FaZe", "players": [{"nickname": "a", "rating": "1.10"}]}],
                }
            ],
            "ratings": [{"team": "FaZe", "players": [{"nickname": "a", "rating": "1.05"}]}],
        }

        class Client:
            async def get_match_snapshot(self, match_id, url, watch=False):
                return snapshot

        class Store:
            def __init__(self):
                self.item = {
                    "match_id": "123",
                    "url": "/matches/123/test",
                    "umo": "group:1",
                    "user_id": "42",
                    "user_name": "Chiaki",
                    "last_map_index": 3,
                    "sent_map_ratings": [],
                }
                self.removed = False

            def all(self):
                return [self.item]

            def contains(self, item):
                return not self.removed

            def update(self, item):
                self.item = item

            def remove(self, item):
                self.removed = True

        class Context:
            def __init__(self):
                self.sent = []

            async def send_message(self, umo, chain):
                self.sent.append((umo, chain.chain))
                return True

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.client = Client()
        plugin.context = Context()
        plugin.live_subscriptions = Store()
        plugin._rating_delay_seconds = lambda _event: 0

        with patch.object(
            module,
            "render_rating_card",
            side_effect=[Path("map-rating.png"), Path("match-rating.png")],
        ):
            await plugin._poll_live_subscriptions()

        self.assertEqual(len(plugin.context.sent), 2)
        self.assertEqual(
            [[kind for kind, _value in chain] for _umo, chain in plugin.context.sent],
            [["at", "text", "image"], ["at", "text", "image"]],
        )
        self.assertEqual(plugin.context.sent[0][1][-1], ("image", "map-rating.png"))
        self.assertEqual(plugin.context.sent[1][1][-1], ("image", "match-rating.png"))
        self.assertTrue(plugin.live_subscriptions.removed)

    async def test_bo1_sends_only_one_rating_image(self):
        module = _load_main_module()
        snapshot = {
            "status": "finished",
            "best_of": "BO1",
            "team1": "Team A",
            "team2": "Team B",
            "maps": [{"ordinal": 1, "finished": True}],
            "map_ratings": [
                {
                    "index": 1,
                    "map": "Nuke",
                    "score": "13:9",
                    "ratings": [{"team": "Team A", "players": [{"nickname": "a"}]}],
                }
            ],
            "ratings": [{"team": "Team A", "players": [{"nickname": "a"}]}],
        }

        class Client:
            async def get_match_snapshot(self, match_id, url, watch=False):
                return snapshot

        class Store:
            def __init__(self):
                self.item = {
                    "match_id": "123",
                    "url": "/matches/123/test",
                    "umo": "group:1",
                    "user_id": "42",
                    "last_map_index": 1,
                    "sent_map_ratings": [],
                }
                self.removed = False

            def all(self):
                return [self.item]

            def contains(self, item):
                return not self.removed

            def update(self, item):
                self.item = item

            def remove(self, item):
                self.removed = True

        class Context:
            def __init__(self):
                self.sent = []

            async def send_message(self, umo, chain):
                self.sent.append(chain.chain)
                return True

        plugin = module.HltvPlugin.__new__(module.HltvPlugin)
        plugin.client = Client()
        plugin.context = Context()
        plugin.live_subscriptions = Store()
        plugin._rating_delay_seconds = lambda _event: 0

        with patch.object(module, "render_rating_card", return_value=Path("bo1.png")) as render:
            await plugin._poll_live_subscriptions()

        self.assertEqual(len(plugin.context.sent), 1)
        self.assertEqual(plugin.context.sent[0][-1], ("image", "bo1.png"))
        self.assertEqual(render.call_count, 1)
        self.assertTrue(plugin.live_subscriptions.removed)


if __name__ == "__main__":
    unittest.main()
