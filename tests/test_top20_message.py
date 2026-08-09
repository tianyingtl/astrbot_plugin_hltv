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


class LiveCommandTests(unittest.IsolatedAsyncioTestCase):
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
                module, "render_live_card", side_effect=RuntimeError("force text fallback")
            ):
                team_subscribed = [
                    result
                    async for result in plugin.live(Event("/hltv live Team 2A"))
                ]

            self.assertEqual(listing[0], ("image", "live.png"))
            self.assertEqual(len(listing), 2)
            self.assertIn("/live 1 2 3", listing[1])
            self.assertIn("已订阅 2 场", subscribed[0])
            self.assertIn("Team 1A vs Team 1B", subscribed[0])
            self.assertIn("Team 3A vs Team 3B", subscribed[0])
            self.assertIn("已订阅 1 场", team_subscribed[0])
            self.assertEqual(
                [item["match_id"] for item in plugin.live_subscriptions.all()],
                ["1", "3", "2"],
            )

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


if __name__ == "__main__":
    unittest.main()
