import sys
import tempfile
import types
import unittest
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image, ImageChops


astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.logger = types.SimpleNamespace(error=lambda *args: None, warning=lambda *args: None)
astrbot.api = astrbot_api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", astrbot_api)

from core.client import HltvClient
from core.formatter import (
    format_live,
    format_map_started,
    format_match_finished,
    format_player,
)
from core.renderer import CARD_SIZE, render_player_card, render_team_card
from core.subscriptions import LiveSubscriptionStore, advance_subscription
from core.translator import Translator


class LiveFormatTests(unittest.TestCase):
    def test_current_map_score_is_above_series_score(self):
        text = format_live(
            [
                {
                    "team1": "Wildcard",
                    "team2": "MongolZ",
                    "maps_score": "0:1",
                    "current_map": "当前 Ancient 4:8",
                    "event": "IEM Cologne",
                    "rating": 3,
                }
            ]
        )

        self.assertLess(
            text.index("小局  Ancient 4:8"),
            text.index("大局  Wildcard 0:1 MongolZ"),
        )
        self.assertIn("MATCH 01  |  LIVE  ★★★", text)

    def test_small_score_row_is_kept_when_hltv_has_not_synced(self):
        text = format_live(
            [{"team1": "100 Thieves", "team2": "Spirit", "maps_score": "0:1"}]
        )

        self.assertLess(text.index("小局"), text.index("大局"))
        self.assertIn("当前地图暂未同步", text)


class MatchSnapshotTests(unittest.TestCase):
    HTML = """
    <div class="countdown">Match over</div>
    <div class="team1-gradient"><div class="teamName">100 Thieves</div></div>
    <div class="team2-gradient"><div class="teamName">Spirit</div></div>
    <div class="timeAndEvent"><div class="event">BLAST Bounty</div></div>
    <div class="mapholder">
      <div class="mapname">Anubis</div>
      <div class="results played">
        <div class="results-left"><div class="results-team-score">11</div></div>
        <div class="results-right won"><div class="results-team-score">13</div></div>
      </div>
    </div>
    <div class="mapholder">
      <div class="mapname">Mirage</div>
      <div class="results played">
        <div class="results-left won"><div class="results-team-score">13</div></div>
        <div class="results-right"><div class="results-team-score">4</div></div>
      </div>
    </div>
    <div class="stats-content" id="all-content">
      <table class="totalstats">
        <tr class="header-row"><td class="teamName">100 Thieves</td><td class="ratingDesc">3.0</td></tr>
        <tr><td class="player-nick">sirah</td><td class="rating">1.23</td></tr>
      </table>
      <table class="totalstats">
        <tr class="header-row"><td class="teamName">Spirit</td></tr>
        <tr><td class="player-nick">donk</td><td class="rating">1.61</td></tr>
      </table>
    </div>
    """

    def test_finished_match_includes_series_score_and_ratings(self):
        snapshot = HltvClient._parse_match_snapshot(BeautifulSoup(self.HTML, "lxml"))

        self.assertEqual(snapshot["status"], "finished")
        self.assertEqual(snapshot["maps_score"], "1:1")
        self.assertEqual(snapshot["rating_version"], "3.0")
        self.assertEqual(
            snapshot["ratings"][1]["players"][0],
            {"nickname": "donk", "rating": "1.61"},
        )

        notice = format_match_finished(snapshot)
        self.assertIn("100 Thieves 1:1 Spirit", notice)
        self.assertIn("donk 1.61", notice)

    def test_map_notice_keeps_small_score_above_series_score(self):
        notice = format_map_started(
            {
                "team1": "100 Thieves",
                "team2": "Spirit",
                "current_map_name": "Mirage",
                "current_score": "0:0",
                "active_map_index": 2,
                "map_total": 3,
                "maps_score": "0:1",
            }
        )

        self.assertLess(notice.index("小局"), notice.index("大局"))


class LiveSubscriptionTests(unittest.TestCase):
    def test_store_is_persistent_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "subscriptions.json"
            store = LiveSubscriptionStore(path)
            match = {"id": "123", "url": "/matches/123/test", "team1": "100T", "team2": "Spirit"}
            snapshot = {"active_map_index": 1, "current_map_name": "Anubis"}

            self.assertTrue(
                store.add(
                    match,
                    snapshot,
                    umo="group:1",
                    user_id="42",
                    user_name="Chiaki",
                )
            )
            self.assertFalse(
                store.add(
                    match,
                    snapshot,
                    umo="group:1",
                    user_id="42",
                    user_name="Chiaki",
                )
            )
            self.assertEqual(len(LiveSubscriptionStore(path).all()), 1)
            self.assertTrue(store.contains(store.all()[0]))

    def test_map_transition_notifies_only_once(self):
        subscription = {"last_map_index": 1, "last_map_name": "Anubis"}
        snapshot = {"status": "live", "active_map_index": 2, "current_map_name": "Mirage"}

        updated, events, finished = advance_subscription(subscription, snapshot)
        self.assertEqual([event["kind"] for event in events], ["map_started"])
        self.assertFalse(finished)
        _, repeated, _ = advance_subscription(updated, snapshot)
        self.assertEqual(repeated, [])

    def test_finished_match_waits_for_rating_then_completes(self):
        subscription = {"last_map_index": 3}
        snapshot = {"status": "finished", "ratings": []}

        waiting, events, finished = advance_subscription(subscription, snapshot, now=100)
        self.assertEqual(events, [])
        self.assertFalse(finished)
        resumed, _, _ = advance_subscription(
            waiting, {"status": "live", "active_map_index": 3}, now=101
        )
        self.assertNotIn("finished_seen_at", resumed)
        _, events, finished = advance_subscription(waiting, snapshot, now=279)
        self.assertEqual(events, [])
        self.assertFalse(finished)
        _, events, finished = advance_subscription(waiting, snapshot, now=280)
        self.assertEqual([event["kind"] for event in events], ["match_finished"])
        self.assertTrue(finished)

        rated = {
            "status": "finished",
            "ratings": [
                {
                    "team": "Spirit",
                    "players": [{"nickname": "donk", "rating": "1.61"}],
                }
            ],
        }
        _, events, finished = advance_subscription(subscription, rated, now=100)
        self.assertEqual([event["kind"] for event in events], ["match_finished"])
        self.assertTrue(finished)


class RendererTests(unittest.TestCase):
    def test_team_and_player_cards_are_nonblank(self):
        team = {
            "title": "100 Thieves Academy International",
            "valve_rank": "18",
            "world_rank": "23",
            "age": "24.1",
            "players": [{"name": f"Player {index}", "cc": "CN"} for index in range(1, 7)],
            "coach": "Very Long Coach Name",
            "weeks_top30": "42",
            "recent": [{"won": True, "opp": "Spirit", "score": "2:1"}],
            "trophies": ["IEM Chengdu 2026"],
        }
        player = {
            "nickname": "donk",
            "name": "Danil Kryshkovets",
            "team": "Spirit",
            "nationality": "Russia",
            "age": "19",
            "rating": "1.53",
            "rating_label": "Rating 3.0",
            "major_wins": 1,
            "major_mvps": 1,
            "total_trophies": 12,
            "total_mvps": 11,
            "top20": [{"year": 2024, "rank": 1}, {"year": 2025, "rank": 2}],
            "championships": [{"name": "Shanghai Major 2024", "major": True}],
        }
        with tempfile.TemporaryDirectory() as temp:
            paths = [
                render_team_card(team, output_dir=Path(temp)),
                render_player_card(player, output_dir=Path(temp)),
            ]
            for path in paths:
                with Image.open(path) as image:
                    self.assertEqual(image.size, CARD_SIZE)
                    flat = Image.new("RGB", image.size, image.getpixel((0, 0)))
                    self.assertIsNotNone(ImageChops.difference(image, flat).getbbox())


class TranslatorTests(unittest.TestCase):
    def test_default_timeout_allows_slow_edge_auth(self):
        self.assertEqual(Translator()._timeout, 30)


class PlayerTests(unittest.TestCase):
    HTML = """
    <div class="playerProfile">
      <h1 class="playerNickname">donk</h1>
      <div class="playerRealname"><img title="Russia"> Danil Kryshkovets</div>
      <div class="playerInfoRow playerAge"><span class="listRight">19 years</span></div>
      <div class="playerInfoRow playerTeam"><a href="/team/7020/spirit">Spirit</a></div>
      <div class="playerInfoRow playerTop20"><span class="top20ListRight">
        <a>#1</a><span class="top-20-year">('24)</span>
        <a>#2</a><span class="top-20-year">('25)</span>
      </span></div>
      <div class="majorSection">
        <div class="majorWinner"><b>1</b> x Major winner</div>
        <div class="majorMVP"><b>1</b> x Major MVP</div>
      </div>
      <div class="trophySection"><div class="trophyRow">
        <div class="trophy"><div class="mvp-count">11</div></div>
        <a class="trophy" href="/events/1/major"><span class="trophyDescription majorTrophy" title="Shanghai Major 2024"></span></a>
        <a class="trophy" href="/events/2/iem"><span class="trophyDescription" title="IEM Katowice 2024"></span></a>
        <a class="trophy" href="/news/3/top"><span class="trophyDescription" title="#1 best player in 24"></span></a>
      </div></div>
      <div class="playerpage-container"><div class="player-stat">
        <b>Rating 3.0</b><span class="statsVal"><p>1.53</p></span>
      </div></div>
    </div>
    """

    def test_player_honors_are_parsed_and_formatted(self):
        page = BeautifulSoup(self.HTML, "lxml")
        player = HltvClient._parse_player_page(page, 21167, "donk")

        self.assertEqual(player["top20"], [{"year": 2024, "rank": 1}, {"year": 2025, "rank": 2}])
        self.assertEqual(player["major_wins"], 1)
        self.assertEqual(player["major_mvps"], 1)
        self.assertEqual(player["total_trophies"], 2)
        self.assertEqual(player["total_mvps"], 11)

        text = format_player(player)
        self.assertIn("HLTV TOP20：2024 #1、2025 #2", text)
        self.assertIn("Major 1 冠（1 次 MVP）", text)
        self.assertIn("赛事冠军 2 次", text)
        self.assertIn("最近冠军：Shanghai Major 2024、IEM Katowice 2024", text)


if __name__ == "__main__":
    unittest.main()
