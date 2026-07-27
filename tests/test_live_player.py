import sys
import tempfile
import types
import unittest
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image, ImageChops, ImageFont


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
from core.renderer import (
    BUNDLED_FONT,
    BUNDLED_FONT_BOLD,
    CARD_SIZE,
    PLAYER_CARD_SIZE,
    render_events_card,
    render_live_card,
    render_matches_card,
    render_news_card,
    render_player_card,
    render_ranking_card,
    render_results_card,
    render_team_card,
)
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
            [{"team1": "100 Thieves", "team2": "Spirit"}]
        )

        self.assertLess(text.index("小局"), text.index("大局"))
        self.assertIn("当前地图暂未同步", text)
        self.assertIn("大局  100 Thieves vs Spirit  ·  比分暂未同步", text)
        self.assertNotIn("0:0", text)


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

    def test_finished_ratings_wrap_two_players_per_line(self):
        notice = format_match_finished(
            {
                "team1": "100 Thieves",
                "team2": "Spirit",
                "maps_score": "1:2",
                "rating_version": "3.0",
                "ratings": [
                    {
                        "team": "Spirit",
                        "players": [
                            {"nickname": f"player{index}", "rating": "1.00"}
                            for index in range(1, 6)
                        ],
                    }
                ],
            }
        )
        rating_lines = [line for line in notice.splitlines() if line.startswith("  ")]

        self.assertEqual(len(rating_lines), 3)
        self.assertTrue(all(line.count("1.00") <= 2 for line in rating_lines))


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


class TeamTests(unittest.IsolatedAsyncioTestCase):
    async def test_100t_alias_selects_current_ranked_team(self):
        client = HltvClient(cache_ttl=0)
        selected = {}

        async def top_teams(max_teams=50):
            return [
                {"id": "999", "title": "100 Thieves Academy"},
                {"id": "777", "title": "100 Thieves"},
            ]

        async def details(team_id, title):
            selected.update({"id": team_id, "title": title})
            return selected

        client.get_top_teams = top_teams
        client.get_team_details = details

        team = await client.find_team("100t")

        self.assertEqual(team, {"id": "777", "title": "100 Thieves"})

    def test_team_history_uses_real_opponent_score_and_result(self):
        html = """
        <h1 class="profile-team-name">100 Thieves</h1>
        <div id="matchesBox"><table>
          <tr class="team-row">
            <td><span data-unix="1785081600000"></span></td>
            <td><div class="team-flex lost"></div></td>
            <td><a class="team-name">100 Thieves</a><a class="team-name">Spirit</a></td>
            <td><span class="score">1</span><span class="score">2</span></td>
          </tr>
          <tr class="team-row">
            <td><span data-unix="1784995200000"></span></td>
            <td><div class="team-flex"></div></td>
            <td><a class="team-name">100 Thieves</a><a class="team-name">MOUZ</a></td>
            <td><span class="score">2</span><span class="score">0</span></td>
          </tr>
        </table></div>
        """

        team = HltvClient._parse_team_page(BeautifulSoup(html, "lxml"), "100 Thieves")

        self.assertEqual(
            [(item["opp"], item["score"], item["won"]) for item in team["recent"]],
            [("Spirit", "1-2", False), ("MOUZ", "2-0", True)],
        )


class RendererTests(unittest.TestCase):
    def test_bundled_fonts_include_chinese_ui_glyphs(self):
        for path in (BUNDLED_FONT, BUNDLED_FONT_BOLD):
            font = ImageFont.truetype(str(path), 32)
            missing = bytes(font.getmask("\U0010ffff"))
            for character in "战队档案选手冠军数据中心全球赛事Sørensen":
                self.assertNotEqual(bytes(font.getmask(character)), missing, character)

    def test_team_and_player_cards_are_nonblank(self):
        team = {
            "title": "Layout Stress Team International",
            "valve_rank": "18",
            "world_rank": "23",
            "age": "24.1",
            "players": [{"name": f"Player {index}", "cc": "CN"} for index in range(1, 7)],
            "coach": "Very Long Coach Name",
            "weeks_top30": "42",
            "recent": [
                {"won": index % 2 == 0, "opp": f"Opponent {index}", "score": "2:1"}
                for index in range(5)
            ],
            "trophies": [f"Championship {index}" for index in range(1, 7)],
        }
        player = {
            "nickname": "ZywOo",
            "name": "Mathieu Herbaut",
            "team": "Vitality",
            "nationality": "France",
            "age": "25",
            "rating": "1.33",
            "rating_label": "Rating 3.0",
            "major_wins": 3,
            "major_mvps": 3,
            "total_trophies": 27,
            "total_mvps": 32,
            "top20": [
                {"year": 2019, "rank": 1},
                {"year": 2020, "rank": 1},
                {"year": 2021, "rank": 2},
                {"year": 2022, "rank": 2},
                {"year": 2023, "rank": 1},
                {"year": 2024, "rank": 3},
                {"year": 2025, "rank": 1},
            ],
            "championships": [
                {"name": f"Championship Event {index}", "major": index == 4}
                for index in range(1, 11)
            ],
            "mvp_events": [f"MVP Event {index}" for index in range(1, 9)],
        }
        with tempfile.TemporaryDirectory() as temp:
            paths = [
                render_team_card(team, output_dir=Path(temp)),
                render_player_card(player, output_dir=Path(temp)),
            ]
            for index, path in enumerate(paths):
                with Image.open(path) as image:
                    expected = CARD_SIZE if index == 0 else PLAYER_CARD_SIZE
                    self.assertEqual(image.size, expected)
                    flat = Image.new("RGB", image.size, image.getpixel((0, 0)))
                    self.assertIsNotNone(ImageChops.difference(image, flat).getbbox())

    def test_list_cards_are_nonblank(self):
        matches = [
            {
                "date": "27-07-2026",
                "time": "18:00",
                "team1": f"Team Alpha {index}",
                "team2": f"Team Beta {index}",
                "event": "IEM Cologne Major 2026",
                "rating": 4,
                "current_map_name": "Ancient",
                "current_score": "7:5",
                "maps_score": "1:0",
            }
            for index in range(10)
        ]
        results = [
            {**match, "score1": "2", "score2": "1"} for match in matches
        ]
        ranking = [
            {"rank": index, "title": f"Ranked Team {index}", "points": 1000 - index, "region": "Europe"}
            for index in range(1, 11)
        ]
        events = [
            {"title": f"Event {index}", "start_date": "07-27", "end_date": "08-02"}
            for index in range(1, 11)
        ]
        news = [
            {"title_zh": f"这是一条用于测试长标题排版的 HLTV 新闻 {index}"}
            for index in range(1, 11)
        ]
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            paths = [
                render_matches_card(matches, "今日赛程", output_dir=output),
                render_live_card(matches[:4], output_dir=output),
                render_results_card(results, "近期赛果", output_dir=output),
                render_ranking_card(ranking, "Valve VRS 排名", show_region=True, output_dir=output),
                render_events_card(events, output_dir=output),
                render_news_card(news, output_dir=output),
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
      <h1 class="playerNickname">ZywOo</h1>
      <div class="playerRealname"><img title="France"> Mathieu Herbaut</div>
      <div class="playerInfoRow playerAge"><span class="listRight">25 years</span></div>
      <div class="playerInfoRow playerTeam"><a href="/team/9565/vitality">Vitality</a></div>
      <div class="playerInfoRow playerTop20"><span class="top20ListRight">
        <a href="/news/31000/top-20-players-of-2020-zywoo-1">#1</a><span class="top-20-year">('20)</span>
        <a href="/news/33000/top-20-players-of-2021-zywoo-2">#2</a><span class="top-20-year">('21)</span>
      </span></div>
      <div class="majorSection">
        <div class="majorWinner"><b>3</b> x Major winner</div>
        <div class="majorMVP"><b>3</b> x Major MVP</div>
      </div>
      <div class="trophySection"><div class="trophyRow">
        <div class="trophy"><div class="trophyHolder"><span class="trophyDescription" title="MVP winner at:&#10;Paris Major 2023&#10;IEM Cologne 2024"><img src="/img/static/event/mvpOld.png"><div class="mvp-count">2</div></span></div></div>
        <a class="trophy" href="/news/1/top"><div class="trophyHolder"><span class="trophyDescription" title="#1 best player in 19"><img src="/img/static/event/trophies/2019/1.png"></span></div></a>
        <a class="trophy" href="/events/1/major"><div class="trophyHolder"><span class="trophyDescription majorTrophy" title="Paris Major 2023"><img src="/img/static/event/trophies/major.png"></span></div></a>
        <a class="trophy" href="/events/2/iem"><div class="trophyHolder"><span class="trophyDescription" title="IEM Cologne 2024"><img src="https://img-cdn.hltv.org/eventtrophy/test.png"></span></div></a>
      </div></div>
      <div class="playerpage-container"><div class="player-stat">
        <b>Rating 3.0</b><span class="statsVal"><p>1.33</p></span>
      </div></div>
    </div>
    """

    def test_player_honors_are_parsed_and_formatted(self):
        page = BeautifulSoup(self.HTML, "lxml")
        player = HltvClient._parse_player_page(page, 11893, "ZywOo")

        self.assertEqual(
            [(item["year"], item["rank"]) for item in player["top20"]],
            [(2019, 1), (2020, 1), (2021, 2)],
        )
        self.assertEqual(player["major_wins"], 3)
        self.assertEqual(player["major_mvps"], 3)
        self.assertEqual(player["total_trophies"], 2)
        self.assertEqual(player["total_mvps"], 2)
        self.assertEqual(player["mvp_events"], ["Paris Major 2023", "IEM Cologne 2024"])

        text = format_player(player)
        self.assertIn("HLTV TOP20：2019 #1、2020 #1、2021 #2", text)
        self.assertIn("Major 3 冠（3 次 MVP）", text)
        self.assertIn("赛事冠军 2 次", text)
        self.assertIn("最近冠军：Paris Major 2023、IEM Cologne 2024", text)


if __name__ == "__main__":
    unittest.main()
