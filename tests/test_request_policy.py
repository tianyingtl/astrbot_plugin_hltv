import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web

from test_live_player import HltvClient, HltvError


class Response:
    def __init__(self, status=200, text="<html>match data</html>", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class RequestPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 100.0
        self.calls = []
        self.responses = []
        self.client = HltvClient()
        self.client._http_session = types.SimpleNamespace(
            get=self.get, close=AsyncMock(), closed=False
        )
        self.time_patch = patch(
            "core.client.time",
            types.SimpleNamespace(monotonic=lambda: self.clock, time=lambda: 0),
        )
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    async def get(self, url, **kwargs):
        self.calls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def test_background_blocked_site_stops_other_background_requests(self):
        self.responses = [Response(403), Response()]
        async with self.client.background_request():
            with self.assertRaises(HltvError):
                await self.client._fetch_page("https://www.hltv.org/matches/1/test")
            for url in ["matches/1/test", "matches/2/test", "news"] * 5:
                with self.assertRaises(HltvError):
                    await self.client._fetch_page(f"https://www.hltv.org/{url}")
        self.assertEqual(len(self.calls), 1)
        self.assertGreaterEqual(self.client._retry_at - self.clock, 900)
        self.clock = self.client._retry_at + 1
        await self.client._fetch_page("https://www.hltv.org/")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.client._request_failures, 0)

    async def test_manual_query_can_probe_during_background_cooldown(self):
        self.responses = [Response(403), Response()]
        async with self.client.background_request():
            with self.assertRaises(HltvError):
                await self.client._fetch_page("https://www.hltv.org/matches/1/test")
        self.assertGreater(self.client._retry_at, self.clock)

        page = await self.client._fetch_page("https://www.hltv.org/")

        self.assertEqual(page.get_text(" ", strip=True), "match data")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.client._retry_at, 0)

    async def test_consecutive_blocks_back_off_and_honor_retry_after(self):
        self.responses = [Response(429, headers={"Retry-After": "7200"}), Response(403)]
        async with self.client.background_request():
            with self.assertRaises(HltvError):
                await self.client._fetch_page("https://www.hltv.org/")
        self.assertGreaterEqual(self.client._retry_at - self.clock, 7200)
        self.clock = self.client._retry_at + 1
        async with self.client.background_request():
            with self.assertRaises(HltvError):
                await self.client._fetch_page("https://www.hltv.org/")
        self.assertGreaterEqual(self.client._retry_at - self.clock, 1800)

    async def test_network_failure_and_challenge_page_are_not_success(self):
        self.responses = [asyncio.TimeoutError(), Response(text='<title>Just a moment...</title>')]
        async with self.client.background_request():
            with self.assertRaises(HltvError):
                await self.client._fetch_page("https://www.hltv.org/")
        self.assertGreaterEqual(self.client._retry_at - self.clock, 60)
        self.clock = self.client._retry_at + 1
        async with self.client.background_request():
            with self.assertRaises(HltvError):
                await self.client._fetch_page("https://www.hltv.org/")
        self.assertGreaterEqual(self.client._retry_at - self.clock, 900)

    async def test_match_page_is_reused_for_a_minute_even_with_result_cache_disabled(self):
        self.client._cache_ttl = 0
        self.responses = [Response(), Response()]
        url = "https://www.hltv.org/matches/1/test"
        first = await self.client._fetch_page(url)
        self.clock += 11
        self.assertIs(await self.client._fetch_page(url), first)
        self.assertEqual(len(self.calls), 1)
        self.clock += 50
        await self.client._fetch_page(url)
        self.assertEqual(len(self.calls), 2)

    async def test_uncached_pages_are_spaced_and_session_is_closed(self):
        self.responses = [Response(), Response()]
        await self.client._fetch_page("https://www.hltv.org/")
        with patch("core.client.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.client._fetch_page("https://www.hltv.org/news")
        self.assertGreaterEqual(sleep.await_args.args[0], 3)
        await self.client.close()
        self.client._http_session.close.assert_awaited_once()

    async def test_view_and_watch_share_snapshot(self):
        self.client._fetch_raw = AsyncMock(return_value=object())
        self.client._parse_match_snapshot = lambda _: {"status": "finished"}
        await self.client.get_match_snapshot(1, "https://www.hltv.org/matches/1/test")
        await self.client.get_match_snapshot(1, "https://www.hltv.org/matches/1/test", watch=True)
        self.client._fetch_raw.assert_awaited_once()

    async def test_not_found_does_not_block_other_pages(self):
        self.responses = [Response(404), Response()]
        with self.assertRaises(HltvError):
            await self.client._fetch_page("https://www.hltv.org/matches/1/test")
        self.assertEqual(self.client._retry_at, 0)
        self.clock += 5
        await self.client._fetch_page("https://www.hltv.org/")

    async def test_retry_after_http_date(self):
        self.responses = [Response(429, headers={"Retry-After": "Thu, 01 Jan 1970 02:00:00 GMT"})]
        async with self.client.background_request():
            with self.assertRaises(HltvError):
                await self.client._fetch_page("https://www.hltv.org/")
        self.assertGreaterEqual(self.client._retry_at - self.clock, 7200)

    async def test_library_fetch_uses_same_policy(self):
        self.responses = [Response(403)]
        async with self.client.background_request():
            with self.assertRaises(HltvError):
                await self.client.get_results(days=1, min_stars=0)
            with self.assertRaises(HltvError):
                await self.client.get_events()
        self.assertEqual(len(self.calls), 1)

    async def test_successful_proxy_is_retained_after_retry(self):
        self.client._proxy_list = ["http://first:1", "http://second:2"]
        get = AsyncMock(side_effect=[asyncio.TimeoutError(), Response(), Response()])
        self.client._http_session.get = get
        async with self.client.background_request():
            with self.assertRaises(HltvError):
                await self.client._fetch_page("https://www.hltv.org/")
        self.clock = self.client._retry_at + 1
        await self.client._fetch_page("https://www.hltv.org/")
        self.clock += 5
        await self.client._fetch_page("https://www.hltv.org/news")
        self.assertEqual(
            [item.kwargs["proxy"] for item in get.await_args_list],
            ["http://first:1", "http://second:2", "http://second:2"],
        )

    async def test_scorebot_refresh_does_not_reload_html_every_ten_seconds(self):
        self.responses = [Response()]
        self.client._parse_match_snapshot = lambda _: {"status": "live", "maps": []}
        self.client._parse_scorebot_config = lambda _: {"list_id": "1"}
        self.client._fetch_scorebot = AsyncMock(side_effect=[{"rounds": 1}, {"rounds": 2}])
        self.client.summarize_scorebot = lambda score, *_args: score
        url = "https://www.hltv.org/matches/1/test"
        first = await self.client.get_match_snapshot(1, url, watch=True)
        self.clock += 11
        second = await self.client.get_match_snapshot(1, url, watch=True)
        self.assertEqual((first["rounds"], second["rounds"]), (1, 2))
        self.assertEqual(len(self.calls), 1)


class BrowserTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_transport_reuses_cookies_and_tcp_connection(self):
        requests = []

        async def handle(request):
            requests.append((request.cookies.get("test_session"), request.transport))
            response = web.Response(text="<html><title>Match</title></html>")
            response.set_cookie("test_session", "retained")
            return response

        app = web.Application()
        app.router.add_get("/{path}", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = HltvClient()
        try:
            await client._fetch_page(f"http://127.0.0.1:{port}/first")
            client._next_request_at = 0
            await client._fetch_page(f"http://127.0.0.1:{port}/second")
        finally:
            await client.close()
            await runner.cleanup()
        self.assertEqual([item[0] for item in requests], [None, "retained"])
        self.assertIs(requests[0][1], requests[1][1])
