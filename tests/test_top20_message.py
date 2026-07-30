import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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
                    }
                ]

            async def get_match_snapshot(self, match_id, url):
                return {
                    "maps_score": "1:1",
                    "current_map": "Nuke 18:17",
                    "current_map_name": "Nuke",
                    "current_score": "18:17",
                }

            async def get_delayed_matches(self, min_stars=0):
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
        self.assertIn("大局  Team A 1:1 Team B", results[0])
        self.assertLess(results[0].index("小局"), results[0].index("大局"))


if __name__ == "__main__":
    unittest.main()
