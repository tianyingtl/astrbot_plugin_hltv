import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock
from PIL import Image, ImageChops

from test_live_player import HltvClient, HltvError
from core.aliases import (
    PLAYER_ALIAS_GROUPS,
    PLAYER_ALIASES,
    TEAM_ALIAS_GROUPS,
    TEAM_ALIASES,
    normalize_team_name,
    resolve_player_name,
    select_player,
)
from core.formatter import format_event_player_stats
from core.knowledge import HltvKnowledgeService, match_match_query, match_team_query
from core.renderer import render_event_stats_card, WIDE_BACKGROUND


class AliasTests(unittest.TestCase):
    def test_aliases_have_no_conflicting_targets(self):
        for groups, lookup in (
            (TEAM_ALIAS_GROUPS, TEAM_ALIASES),
            (PLAYER_ALIAS_GROUPS, PLAYER_ALIASES),
        ):
            seen = {}
            for canonical, aliases in groups.items():
                for alias in (canonical, *aliases):
                    key = normalize_team_name(alias)
                    with self.subTest(alias=alias):
                        self.assertTrue(key)
                        self.assertEqual(seen.setdefault(key, canonical), canonical)
                        self.assertEqual(lookup[key], canonical)

    def test_team_aliases_work_in_both_directions_for_match_filters(self):
        for canonical, aliases in TEAM_ALIAS_GROUPS.items():
            for alias in aliases:
                with self.subTest(alias=alias):
                    self.assertTrue(match_team_query(alias, {
                        'team1': canonical, 'team2': 'Unrelated Opponent',
                    }))
                    self.assertTrue(match_team_query(canonical, {
                        'team1': alias, 'team2': 'Unrelated Opponent',
                    }))

    def test_short_names_do_not_match_other_teams_or_academies(self):
        for query, title in (
            ('红星', 'Gambit Youngsters'), ('NAVI', 'NAVI Junior'),
            ('绿龙', 'Spirit Academy'), ('老鼠', 'MOUZ NXT'),
            ('GL', 'Eternal Fire'), ('VP', 'VP.Prodigy'),
            ('SK', 'SKADE'), ('G2', 'G2 Ares'),
            ('C9', 'Gambit'), ('MIBR', 'SK'),
        ):
            with self.subTest(query=query, title=title):
                match = {'team1': title, 'team2': 'Unrelated Opponent'}
                self.assertFalse(match_team_query(query, match))
                self.assertFalse(match_match_query(query, match))
        self.assertTrue(match_match_query('电竞世界杯', {'event': 'Esports World Cup 2026'}))
        self.assertTrue(match_team_query('Unlisted', {'team1': 'Unlisted Esports'}))

    def test_player_aliases_resolve_and_unknown_queries_survive(self):
        for canonical, aliases in PLAYER_ALIAS_GROUPS.items():
            for alias in aliases:
                with self.subTest(alias=alias):
                    self.assertEqual(
                        normalize_team_name(resolve_player_name(alias)),
                        normalize_team_name(canonical),
                    )
        self.assertEqual(resolve_player_name('  Unknown Newcomer  '), 'Unknown Newcomer')

    def test_exact_player_wins_over_substrings_and_preserves_niko_case(self):
        players = [{'nickname': name} for name in ('rainwaker', 'rain', 'niko', 'NiKo')]
        self.assertEqual(select_player('雨神', players)['nickname'], 'rain')
        self.assertEqual(select_player('尼公子', players)['nickname'], 'NiKo')
        self.assertEqual(select_player('niko', players)['nickname'], 'niko')
        self.assertIsNone(select_player('雨神', players[:1]))
        self.assertIsNone(select_player('', players))

    def test_stats_text_adds_and_highlights_alias_target_outside_limit(self):
        result = {'players': [
            {'rank': 1, 'nickname': 'rainwaker'},
            {'rank': 2, 'nickname': 'rain'},
        ]}
        text = format_event_player_stats(result, 1, '雨神')
        self.assertIn('▶ #2 rain', text)
        self.assertNotIn('▶ #1', text)
        self.assertNotIn('不在', text)
        self.assertNotIn('▶', format_event_player_stats(result, 1))

    def test_stats_image_alias_has_same_rows_and_highlight_as_official_name(self):
        result = {'event': {'title': 'Test Event'}, 'players': [
            {'rank': 1, 'nickname': 'rainwaker'},
            {'rank': 2, 'nickname': 'rain'},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            options = dict(max_items=1, output_dir=Path(directory), background_path=WIDE_BACKGROUND)
            alias = render_event_stats_card(result, player_name='雨神', **options)
            official = render_event_stats_card(result, player_name='rain', **options)
            plain = render_event_stats_card(result, **options)
            with Image.open(alias) as a, Image.open(official) as b, Image.open(plain) as c:
                self.assertIsNone(ImageChops.difference(a, b).getbbox())
                self.assertEqual(a.height, c.height + 64)


class AliasClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_team_aliases_resolve_ranked_canonical_names_without_search(self):
        client = HltvClient(cache_ttl=0)
        teams = [{'id': str(i), 'title': name} for i, name in enumerate(TEAM_ALIAS_GROUPS)]
        client.get_top_teams = AsyncMock(return_value=teams)
        client._search = AsyncMock(side_effect=AssertionError('Unexpected search'))
        client.get_team_details = AsyncMock(side_effect=lambda tid, name: {'title': name})
        for canonical, aliases in TEAM_ALIAS_GROUPS.items():
            for alias in (canonical, *aliases):
                with self.subTest(alias=alias):
                    self.assertEqual((await client.find_team(alias))['title'], canonical)

    async def test_historical_team_search_not_short_circuited_by_ranked_academy(self):
        client = HltvClient(cache_ttl=0)
        client.get_top_teams = AsyncMock(return_value=[{'id': '2', 'title': 'Gambit Youngsters'}])
        client._search = AsyncMock(return_value={'teams': [
            {'id': '2', 'name': 'Gambit Youngsters'}, {'id': '1', 'name': 'Gambit'},
        ]})
        client.get_team_details = AsyncMock(return_value={'title': 'Gambit'})
        await client.find_team('红星')
        client._search.assert_awaited_once_with('Gambit')
        client.get_team_details.assert_awaited_once_with('1', 'Gambit')

    async def test_missing_known_team_does_not_return_another_team(self):
        client = HltvClient(cache_ttl=0)
        client.get_top_teams = AsyncMock(return_value=[])
        client._search = AsyncMock(return_value={'teams': [{'id': '2', 'name': 'SKADE'}]})
        client.get_team_details = AsyncMock(return_value={'title': 'SKADE'})
        with self.assertRaises(HltvError):
            await client.find_team('SK')

    async def test_player_search_uses_canonical_name_and_best_result(self):
        client = HltvClient(cache_ttl=0)
        client._search = AsyncMock(return_value={'players': [
            {'id': '2', 'nickName': 'rainwaker'}, {'id': '1', 'nickName': 'rain'},
        ]})
        client.get_player_details = AsyncMock(return_value={'nickname': 'rain'})
        await client.find_player('雨神')
        client._search.assert_awaited_once_with('rain')
        client.get_player_details.assert_awaited_once_with('1', 'rain')

    async def test_player_fallback_uses_same_alias_and_exact_priority(self):
        client = HltvClient(cache_ttl=0)
        client._search = AsyncMock(side_effect=HltvError('Unavailable'))
        client._call = AsyncMock(return_value=[
            {'id': '2', 'nickname': 'rainwaker'}, {'id': '1', 'nickname': 'rain'},
        ])
        client.get_player_details = AsyncMock(return_value={'nickname': 'rain'})
        await client.find_player('雨神')
        client.get_player_details.assert_awaited_once_with('1', 'rain')

    async def test_llm_player_query_uses_shared_aliases(self):
        client = HltvClient(cache_ttl=0)
        client._search = AsyncMock(return_value={'players': [{'id': '1', 'name': 'Twistzz'}]})
        client.get_player_details = AsyncMock(return_value={'nickname': 'Twistzz'})
        service = HltvKnowledgeService(
            client, max_items=10, default_days=1, live_score_loader=AsyncMock(),
        )
        await service.query('player', '总监')
        client._search.assert_awaited_once_with('Twistzz')
