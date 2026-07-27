import sys
import tempfile
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from bs4 import BeautifulSoup
from PIL import Image, ImageChops, ImageFont


astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.logger = types.SimpleNamespace(error=lambda *args: None, warning=lambda *args: None)
astrbot.api = astrbot_api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", astrbot_api)

from core.client import HltvClient, HltvError
from core.formatter import (
    format_live,
    format_map_started,
    format_match_finished,
    format_news,
    format_news_detail,
    format_player,
    news_titles,
)
from core.renderer import (
    BACKGROUND_POOL,
    BUNDLED_FONT,
    BUNDLED_FONT_BOLD,
    CARD_BASE_BACKGROUND,
    CARD_SIZE,
    PLAYER_CARD_SIZE,
    TOP20_CARD_SIZE,
    WIDE_BACKGROUND,
    _pick_background,
    render_events_card,
    render_live_card,
    render_matches_card,
    render_news_card,
    render_player_card,
    render_ranking_card,
    render_results_card,
    render_team_card,
    render_top20_card,
    render_top20_player_card,
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


class NewsTitleTests(unittest.TestCase):
    def test_news_keeps_chinese_and_english_titles(self):
        item = {
            "title": "Spirit complete a flawless series",
            "title_zh": "Spirit 完成零失误系列赛",
            "featured": True,
        }

        self.assertEqual(
            news_titles(item),
            ("Spirit 完成零失误系列赛", "Spirit complete a flawless series"),
        )
        listing = format_news([item], 10)
        detail = format_news_detail(
            item["title_zh"],
            ["正文"],
            "https://www.hltv.org/news/1/example",
            original_title=item["title"],
        )
        self.assertLess(listing.index(item["title_zh"]), listing.index(item["title"]))
        self.assertIn(f"EN: {item['title']}", listing)
        self.assertIn(f"EN: {item['title']}", detail)

    def test_failed_translation_does_not_duplicate_english_title(self):
        title = "Translation service unavailable"
        item = {"title": title, "title_zh": title}

        self.assertEqual(news_titles(item), (title, ""))
        self.assertEqual(format_news([item], 10).count(title), 1)


class Top20Tests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _archive(year: int, ranks: range, *, final: bool = False):
        links = "".join(
            f'<a href="/news/{40000 + rank}/top-20-players-of-{year}-player-{rank}-{rank}">'
            f'Top 20 players of {year}: Player {rank} ({rank}) 2024-01-01 10 comments</a>'
            for rank in ranks
        )
        if final:
            links += (
                f'<a href="/news/49999/top-20-players-of-{year}-final-list">'
                f'Top 20 players of {year}: final list</a>'
            )
        return BeautifulSoup(links, "lxml")

    async def test_top20_falls_back_to_official_image_when_fivee_fails(self):
        january = BeautifulSoup(
            """
            <a href="/news/40000/unrelated">Other news</a>
            <a href="/news/40002/top-20-players-of-2023-final-three-to-be-unveiled">
              Top 20 players of 2023: final three to be unveiled
            </a>
            <a href="/news/40001/top-20-players-of-2023-final-list">
              Top 20 players of 2023: final list
            </a>
            """,
            "lxml",
        )
        article = BeautifulSoup(
            """
            <meta property="og:image" content="https://img-cdn.hltv.org/gallerypicture/cover.jpg">
            <article class="newsitem">
              <img src="/img/static/flags/30x20/DK.gif" class="flag">
              <figure>
                <img data-src="https://img-cdn.hltv.org/gallerypicture/top20-2023.png"
                     alt="Top 20 players of 2023 final ranking">
              </figure>
            </article>
            """,
            "lxml",
        )
        expected = Path("D:/temp222/top20_2023.png")
        client = HltvClient(cache_ttl=300)

        async def download(url, *_args, **_kwargs):
            if "oss.5eplay.com" in url:
                raise HltvError("5E 图片暂时不可用。")
            return expected

        client._download_top20_image = AsyncMock(side_effect=download)

        class FakeHltv:
            USE_PROXY = False
            session = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def _fetch(self, url):
                return article if "/news/40001/" in url else january

        fake_hltv = FakeHltv()
        with patch("core.client.Hltv", return_value=fake_hltv):
            result = await client.get_top20(2023)

        self.assertEqual(result["image_path"], expected)
        self.assertEqual(result["players"], [])
        self.assertEqual(
            client._parse_top20_article_url(january, 2023),
            "https://www.hltv.org/news/40001/top-20-players-of-2023-final-list",
        )
        self.assertEqual(
            client._parse_top20_image_url(article, 2023),
            "https://img-cdn.hltv.org/gallerypicture/top20-2023.png",
        )
        self.assertEqual(client._download_top20_image.await_count, 2)
        self.assertEqual(
            client._download_top20_image.await_args_list[0].args[:2],
            (
                "https://oss.5eplay.com/editor/20241225/3c187f9d1fb3d64caa1ae4b6b2ae31df.png",
                2023,
            ),
        )
        self.assertEqual(
            client._download_top20_image.await_args_list[0].kwargs["referer"],
            "https://csgo.5eplay.com/article/241225dr0172",
        )
        self.assertEqual(
            client._download_top20_image.await_args_list[1],
            call(
                "https://img-cdn.hltv.org/gallerypicture/top20-2023.png",
                2023,
                referer="https://www.hltv.org/news/40001/top-20-players-of-2023-final-list",
                session=fake_hltv.session,
                proxy=None,
            ),
        )

    async def test_historical_top20_uses_fivee_finished_poster(self):
        posters = {
            2013: (
                "https://csgo.5eplay.com/article/241203sredm0",
                "https://oss.5eplay.com/editor/20241203/cefd72f358ccaf2e07fb17df5f7a01e4.png",
            ),
            2014: (
                "https://csgo.5eplay.com/article/241203lys127",
                "https://oss.5eplay.com/editor/20241204/fb37ad1a8245978a3ce7171f0f7cfdf3.png",
            ),
            2015: (
                "https://csgo.5eplay.com/article/241203epzqv5",
                "https://oss.5eplay.com/editor/20241208/c79e22058dd2bbe7ffb88482db054a44.png",
            ),
        }
        client = HltvClient(cache_ttl=0)
        for year, (article_url, image_url) in posters.items():
            with self.subTest(year=year):
                expected = Path(f"D:/cache/top20-{year}.png")
                client._download_top20_image = AsyncMock(return_value=expected)
                with patch("core.client.Hltv", None):
                    result = await client.get_top20(year)

                self.assertEqual(result["image_path"], expected)
                self.assertEqual(result["players"], [])
                client._download_top20_image.assert_awaited_once_with(
                    image_url,
                    year,
                    referer=article_url,
                )

    def test_old_top20_video_recap_and_twitter_image_are_resolved(self):
        archive = BeautifulSoup(
            '<a href="/news/14029/video-top-20-players-of-2014">Video</a>',
            "lxml",
        )
        article = BeautifulSoup(
            '<blockquote class="twitter-tweet"><img '
            'src="http://pbs.twimg.com/media/B77G8-0IIAAEGLZ.png"></blockquote>',
            "lxml",
        )

        self.assertEqual(
            HltvClient._parse_top20_article_url(archive, 2014),
            "https://www.hltv.org/news/14029/video-top-20-players-of-2014",
        )
        self.assertEqual(
            HltvClient._parse_top20_image_url(article, 2014),
            "https://pbs.twimg.com/media/B77G8-0IIAAEGLZ.png",
        )

    def test_top20_archive_keeps_player_country(self):
        archive = BeautifulSoup(
            '<a href="/news/14010/top-20-players-of-2014-get-right-1">'
            '<img class="newsflag flag" title="Sweden">'
            'Top 20 players of 2014: GeT_RiGhT (1)</a>',
            "lxml",
        )

        players = HltvClient._parse_top20_players([archive], 2014)

        self.assertEqual(players[0]["country"], "Sweden")

    async def test_top20_falls_back_to_archive_ranking_when_cdn_is_forbidden(self):
        january = self._archive(2023, range(1, 9), final=True)
        december = self._archive(2023, range(9, 21))
        article = BeautifulSoup(
            """
            <figure>
              <img src="https://img-cdn.hltv.org/gallerypicture/top20-2023.png"
                   alt="Top 20 players of 2023 final ranking">
            </figure>
            """,
            "lxml",
        )
        client = HltvClient(cache_ttl=300)
        client._download_top20_image = AsyncMock(
            side_effect=HltvError("HLTV TOP20 图片下载失败（HTTP 403）。")
        )

        class FakeHltv:
            USE_PROXY = False
            session = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def _fetch(self, url):
                if url.endswith("/december"):
                    return december
                if "/news/49999/" in url:
                    return article
                return january

        with patch("core.client.Hltv", return_value=FakeHltv()):
            result = await client.get_top20(2023)

        self.assertIsNone(result["image_path"])
        self.assertEqual(len(result["players"]), 20)
        self.assertEqual(result["players"][0]["rank"], 1)
        self.assertEqual(result["players"][0]["name"], "Player 1")
        self.assertEqual(result["players"][-1]["rank"], 20)
        self.assertEqual(result["players"][-1]["name"], "Player 20")

    async def test_top20_uses_archive_when_year_has_no_final_list_article(self):
        january = self._archive(2023, range(1, 16))
        december = self._archive(2023, range(16, 21))
        client = HltvClient(cache_ttl=300)
        client._download_top20_image = AsyncMock(
            side_effect=HltvError("5E 图片暂时不可用。")
        )

        class FakeHltv:
            USE_PROXY = False
            session = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def _fetch(self, url):
                return december if url.endswith("/december") else january

        with patch("core.client.Hltv", return_value=FakeHltv()):
            result = await client.get_top20(2023)

        self.assertIsNone(result["image_path"])
        self.assertEqual(len(result["players"]), 20)
        client._download_top20_image.assert_awaited_once()

    async def test_top20_player_returns_article_and_falls_back_when_image_is_forbidden(self):
        january = BeautifulSoup(
            """
            <a href="/news/43505/top-20-players-of-2025-niko-18">
              Top 20 players of 2025: NiKo (18) 2025-12-26 285 comments
            </a>
            """,
            "lxml",
        )
        article = BeautifulSoup(
            """
            <meta property="og:description" content="NiKo sets a new Top 20 record.">
            <meta property="og:image" content="https://img-cdn.hltv.org/gallerypicture/share.jpg">
            <article class="newsitem standard-box">
              <h1 class="headline">Top 20 players of 2025: NiKo (18)</h1>
              <div class="newstext-con">
                <div class="image-con"><img class="image"
                  src="https://img-cdn.hltv.org/gallerypicture/niko-top18.png"></div>
                <a class="news-read-more-1"><img class="news-read-more-image"
                  src="https://img-cdn.hltv.org/gallerypicture/unrelated.jpg"></a>
              </div>
            </article>
            """,
            "lxml",
        )
        client = HltvClient(cache_ttl=300)
        client._download_top20_image = AsyncMock(
            side_effect=HltvError("HLTV TOP20 图片下载失败（HTTP 403）。")
        )

        class FakeHltv:
            USE_PROXY = False
            session = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def _fetch(self, url):
                return article if "/news/43505/" in url else january

        fake_hltv = FakeHltv()
        with patch("core.client.Hltv", return_value=fake_hltv):
            result = await client.get_top20_player(2025, 18)

        self.assertEqual(result["name"], "NiKo")
        self.assertEqual(result["rank"], 18)
        self.assertEqual(result["title"], "Top 20 players of 2025: NiKo (18)")
        self.assertEqual(result["description"], "NiKo sets a new Top 20 record.")
        self.assertEqual(
            result["url"],
            "https://www.hltv.org/news/43505/top-20-players-of-2025-niko-18",
        )
        self.assertIsNone(result["image_path"])
        client._download_top20_image.assert_awaited_once_with(
            "https://img-cdn.hltv.org/gallerypicture/niko-top18.png",
            2025,
            referer=result["url"],
            session=fake_hltv.session,
            proxy=None,
        )

    def test_browser_fingerprint_session_downloads_official_cdn_image(self):
        output = BytesIO()
        Image.new("RGB", (800, 900), "white").save(output, "JPEG")
        image_body = output.getvalue()
        article_url = "https://www.hltv.org/news/43505/top-20-players-of-2025-niko-18"
        image_url = "https://img-cdn.hltv.org/gallerypicture/niko-top18.png"

        class FakeResponse:
            def __init__(self, url, body):
                self.status_code = 200
                self.url = url
                self.content = body

        class FakeCurlSession:
            instance = None

            def __init__(self, **kwargs):
                self.options = kwargs
                self.calls = []
                self.closed = False
                FakeCurlSession.instance = self

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                body = image_body if url == image_url else b"<html></html>"
                return FakeResponse(url, body)

            def close(self):
                self.closed = True

        with patch("core.client.CurlSession", FakeCurlSession, create=True):
            body = HltvClient._download_top20_image_browser(
                image_url,
                referer=article_url,
                proxy=None,
                timeout=30,
            )

        session = FakeCurlSession.instance
        self.assertEqual(body, image_body)
        self.assertEqual(session.options["impersonate"], "chrome")
        self.assertEqual([call[0] for call in session.calls], [image_url])
        self.assertEqual(session.calls[0][1]["referer"], article_url)
        self.assertTrue(session.closed)

    async def test_protected_image_hosts_use_browser_fingerprint_route(self):
        output = BytesIO()
        Image.new("RGB", (800, 900), "white").save(output, "JPEG")
        image_body = output.getvalue()
        article_url = "https://www.hltv.org/news/1/top-20-test"
        image_urls = (
            "https://img-cdn.hltv.org/gallerypicture/browser-route-test.png",
            "https://pbs.twimg.com/media/annual-recap.png",
            "https://oss.5eplay.com/img/20160111/player.jpeg",
        )

        for image_url in image_urls:
            with (
                self.subTest(image_url=image_url),
                tempfile.TemporaryDirectory() as tmp,
                patch("core.client.CurlSession", object()),
                patch.object(Path, "home", return_value=Path(tmp)),
                patch.object(
                    HltvClient,
                    "_download_top20_image_browser",
                    return_value=image_body,
                ) as browser_download,
            ):
                result = await HltvClient(cache_ttl=0)._download_top20_image(
                    image_url,
                    2025,
                    referer=article_url,
                )

            self.assertEqual(result.suffix, ".jpg")
            browser_download.assert_called_once_with(
                image_url,
                referer=article_url,
                proxy=None,
                timeout=30,
            )

    def test_top20_player_fallback_card_renders(self):
        item = {
            "year": 2025,
            "rank": 18,
            "name": "NiKo",
            "title": "Top 20 players of 2025: NiKo (18)",
            "description": "A stable season with several deep tournament runs.",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = render_top20_player_card(
                item,
                background_path=CARD_BASE_BACKGROUND,
                output_dir=Path(tmp),
            )
            with Image.open(output) as card:
                self.assertEqual(card.size, CARD_SIZE)
                self.assertIsNotNone(card.getbbox())

    def test_top20_fallback_card_renders_all_twenty_players(self):
        players = [
            {"rank": rank, "name": f"Player {rank}"}
            for rank in range(1, 21)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output = render_top20_card(
                players,
                2023,
                background_path=CARD_BASE_BACKGROUND,
                output_dir=Path(tmp),
            )
            with Image.open(output) as card:
                self.assertEqual(card.size, TOP20_CARD_SIZE)
                self.assertIsNotNone(card.getbbox())


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
    def test_all_four_user_backgrounds_are_randomized(self):
        self.assertEqual(len(BACKGROUND_POOL), 4)
        self.assertTrue(all(path.is_file() for path in BACKGROUND_POOL))
        with patch("core.renderer.random.choice", return_value=CARD_BASE_BACKGROUND) as choose:
            self.assertEqual(_pick_background(None), CARD_BASE_BACKGROUND)
            choose.assert_called_once_with(BACKGROUND_POOL)
        self.assertEqual(_pick_background(WIDE_BACKGROUND), WIDE_BACKGROUND)

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
            {
                "title": f"An English HLTV headline used to test bilingual layout {index}",
                "title_zh": f"这是一条用于测试双语标题排版的 HLTV 新闻 {index}",
            }
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
