import sys
import types
import unittest

from bs4 import BeautifulSoup


astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.logger = types.SimpleNamespace(error=lambda *args: None, warning=lambda *args: None)
astrbot.api = astrbot_api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", astrbot_api)

from core.client import HltvClient
from core.formatter import format_live, format_player


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

        self.assertLess(text.index("当前 Ancient 4:8"), text.index("Wildcard 0:1 MongolZ"))
        self.assertIn("Wildcard 0:1 MongolZ  ★★★", text)


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
