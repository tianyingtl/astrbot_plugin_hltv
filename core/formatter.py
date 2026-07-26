"""消息格式化层：把 hltv-async-api 的原始数据渲染成纯文本回复。

全部是无状态纯函数，字段访问一律用 .get() 防御——HLTV 页面结构变化时
库的返回字段可能缺失，格式化层不应因此抛异常。
想换输出形式（图片卡片、转发消息等）时只改本文件。
"""

from typing import Any


def _clip(items: list, max_items: int) -> tuple[list, int]:
    """截断列表，返回 (截断后的列表, 被省略的条数)。"""
    if max_items > 0 and len(items) > max_items:
        return items[:max_items], len(items) - max_items
    return items, 0


def _omitted_line(omitted: int) -> str:
    return f"\n…… 另有 {omitted} 条未显示" if omitted else ""


def _match_sort_key(m: dict) -> tuple:
    """直播中排最前，其余按日期（DD-MM-YYYY → 可排序的 ISO 形式）+ 时间。"""
    d = str(m.get("date", ""))
    if d == "LIVE":
        return (0, "", "")
    try:
        day, month, year = d.split("-")
        iso = f"{year}-{month}-{day}"
    except ValueError:
        iso = "9999-99-99"
    return (1, iso, str(m.get("time", "")))


def _match_line(m: dict) -> str:
    """单场比赛（日期在分组标题里，这里只带时间）。"""
    stars = "★" * int(m.get("rating") or 0)
    time_ = str(m.get("time", ""))
    when = f"[{time_}] " if time_ and time_ != "LIVE" else ""
    line = f"· {when}{m.get('team1', 'TBD')} vs {m.get('team2', 'TBD')}  {stars}".rstrip()
    event = str(m.get("event", "")).strip()
    return f"{line}\n  {event}" if event else line


def _stars_hint(min_stars: int) -> str:
    return f"，≥{'★' * min_stars}" if min_stars > 0 else ""


def _render_match_list(
    title: str, matches: list[dict], max_items: int, empty_text: str
) -> str:
    """比赛列表统一渲染：排序 → 截断 → 按日期分组（直播中置顶）。"""
    if not matches:
        return empty_text
    ordered = sorted(matches, key=_match_sort_key)
    shown, omitted = _clip(ordered, max_items)
    lines = [title]
    current_date = None
    for m in shown:
        d = str(m.get("date", "?"))
        if d != current_date:
            current_date = d
            lines.append("🔴 直播中" if d == "LIVE" else f"📆 {d}")
        lines.append(_match_line(m))
    return "\n".join(lines) + _omitted_line(omitted)


def _empty_hint(min_stars: int) -> str:
    return "（可在配置中调低 min_stars 星级门槛）" if min_stars > 0 else ""


def format_matches(matches: list[dict], days: int, max_items: int, min_stars: int = 0) -> str:
    return _render_match_list(
        f"📅 近 {days} 天大赛（共 {len(matches)} 场{_stars_hint(min_stars)}）",
        matches,
        max_items,
        f"📅 近 {days} 天没有符合条件的比赛{_empty_hint(min_stars)}。",
    )


def format_today(matches: list[dict], max_items: int, min_stars: int = 0) -> str:
    return _render_match_list(
        f"📅 今日赛程（共 {len(matches)} 场{_stars_hint(min_stars)}）",
        matches,
        max_items,
        f"📅 今天没有符合条件的比赛{_empty_hint(min_stars)}。",
    )


def format_live(matches: list[dict]) -> str:
    if not matches:
        return "🔴 当前没有正在进行的比赛。"
    lines = ["🔴 正在进行的比赛"]
    for m in matches:
        lines.append(
            f"· {m.get('team1', '?')} vs {m.get('team2', '?')}"
            f"  ({m.get('event', '')})"
        )
    return "\n".join(lines)


def format_results(results: list[dict], days: int, max_items: int) -> str:
    if not results:
        return f"🏁 近 {days} 天没有查到赛果。"
    shown, omitted = _clip(results, max_items)
    lines = [f"🏁 近 {days} 天赛果（共 {len(results)} 场）"]
    for r in shown:
        date = str(r.get("date") or "").strip()
        date_str = f"[{date}] " if date else ""
        lines.append(
            f"· {date_str}{r.get('team1', '?')} {r.get('score1', '?')} : "
            f"{r.get('score2', '?')} {r.get('team2', '?')}\n"
            f"  {r.get('event', '')}"
        )
    return "\n".join(lines) + _omitted_line(omitted)


def format_ranking(teams: list[dict], max_items: int) -> str:
    if not teams:
        return "🏆 暂无排名数据。"
    shown, omitted = _clip(teams, max_items)
    lines = ["🏆 HLTV 战队排名"]
    for t in shown:
        change = str(t.get("change", "")).strip()
        change_str = f" ({change})" if change and change not in ("0", "-") else ""
        lines.append(
            f"#{t.get('rank', '?')} {t.get('title', '?')}"
            f" — {t.get('points', '?')} 分{change_str}"
        )
    return "\n".join(lines) + _omitted_line(omitted)


def format_events(events: list[dict], max_items: int) -> str:
    if not events:
        return "🎪 暂无赛事数据。"
    shown, omitted = _clip(events, max_items)
    lines = ["🎪 近期赛事"]
    for e in shown:
        lines.append(
            f"· {e.get('title', '?')}"
            f"  [{e.get('start_date', '?')} ~ {e.get('end_date', '?')}]"
        )
    return "\n".join(lines) + _omitted_line(omitted)


def format_team(team: dict) -> str:
    players = team.get("players") or {}
    if isinstance(players, dict):
        roster = "、".join(str(p) for p in players.keys())
    else:
        roster = "、".join(str(p) for p in players)
    lines = [
        f"🛡️ {team.get('title', '?')}（世界排名 #{team.get('rank', '?')}）",
        f"阵容：{roster or '未知'}",
        f"教练：{team.get('coach', '未知')}",
        f"平均年龄：{team.get('age', '未知')}",
        f"近期奖杯：{team.get('last_trophy', '无')}",
        f"奖杯总数：{team.get('total_trophies', '?')}",
    ]
    return "\n".join(lines)


def format_player(player: dict) -> str:
    lines = [
        f"🎯 {player.get('nickname', '?')}（{player.get('name', '?')}）",
        f"战队：{player.get('team', '未知')}",
        f"国籍：{player.get('nationality', '未知')}  年龄：{player.get('age', '?')}",
        f"Rating: {player.get('rating', '?')}  KPR: {player.get('kpr', '?')}"
        f"  爆头率: {player.get('hs', '?')}",
        f"MVP 数：{player.get('total_mvps', '?')}"
        f"  奖杯总数：{player.get('total_trophies', '?')}",
    ]
    return "\n".join(lines)


def format_news(news: list[dict], max_items: int) -> str:
    # get_last_news 返回按日期分组的结构：[{date, f_news: [...], news: [...]}]
    lines = ["📰 HLTV 新闻"]
    count = 0
    for day in news:
        for item in list(day.get("f_news") or []) + list(day.get("news") or []):
            if max_items > 0 and count >= max_items:
                break
            title = _news_title(item)
            if title:
                lines.append(f"· {title}")
                count += 1
    if count == 0:
        return "📰 今天还没有新闻。"
    return "\n".join(lines)


def _news_title(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("title") or item.get("text") or "").strip()
    return str(item).strip()


HELP_TEXT = """🎮 HLTV 查询插件
/hltv today — 今日赛程（大赛）
/hltv matches [天数] — 近期大赛
/hltv live — 正在进行的比赛
/hltv results [天数] — 近期赛果
/hltv ranking — 战队世界排名 Top50
/hltv events — 近期赛事
/hltv team <名称> — 战队信息（任意战队，支持空格）
/hltv player <昵称> — 选手信息（任意选手）
/hltv news — 今日新闻
/hltv help — 显示本帮助
所有子指令均可用中文，如：
/hltv 今日、/hltv 比赛 3、/hltv 战队 spirit、/hltv 选手 donk"""
