"""消息格式化层：把数据层的结构渲染成纯文本回复。

全部是无状态纯函数，字段访问一律防御式——上游解析失败时字段可能
缺失或为哨兵值（None/'None'/'0'/''），格式化层不应因此抛异常或把
Python 哨兵漏给用户。战队/选手图片卡由 renderer.py 负责，本文件保留文本回退。
"""

from typing import Any


def _clip(items: list, max_items: int) -> tuple[list, int]:
    if max_items > 0 and len(items) > max_items:
        return items[:max_items], len(items) - max_items
    return items, 0


def _omitted_line(omitted: int) -> str:
    return f"\n…… 另有 {omitted} 条未显示" if omitted else ""


def _val(v: Any, fallback: str = "未知") -> str:
    """归一化哨兵值（None / 'None' / 空串）。"""
    s = "" if v is None else str(v).strip()
    if s in ("", "None"):
        return fallback
    return s


def _flag(cc: str) -> str:
    """两位 ISO 国家码 → 国旗 emoji。"""
    cc = (cc or "").upper()
    if len(cc) == 2 and cc.isalpha():
        return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc)
    return ""


# ---------------------------------------------------------------- 比赛列表


def _match_sort_key(m: dict) -> tuple:
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
    stars = "★" * int(m.get("rating") or 0)
    time_ = str(m.get("time", ""))
    # late = 已过预定开赛时间但未标记 live（延迟或刚开打）
    late = "⏳" if m.get("late") else ""
    when = f"[{time_}{late}] " if time_ and time_ != "LIVE" else ""
    line = f"· {when}{m.get('team1') or 'TBD'} vs {m.get('team2') or 'TBD'}  {stars}".rstrip()
    event = str(m.get("event", "")).strip()
    return f"{line}\n  {event}" if event else line


def _stars_hint(min_stars: int) -> str:
    return f"，≥{'★' * min_stars}" if min_stars > 0 else ""


def _render_match_list(
    title: str,
    matches: list[dict],
    max_items: int,
    empty_text: str,
    note: str = "",
) -> str:
    if not matches:
        return empty_text
    ordered = sorted(matches, key=_match_sort_key)
    shown, omitted = _clip(ordered, max_items)
    lines = [title]
    if note:
        lines.append(note)
    current_date = None
    for m in shown:
        d = str(m.get("date", "?"))
        if d != current_date:
            current_date = d
            lines.append("🔴 直播中" if d == "LIVE" else f"📆 {d}")
        lines.append(_match_line(m))
    return "\n".join(lines) + _omitted_line(omitted)


def format_matches(
    matches: list[dict],
    days: int,
    max_items: int,
    min_stars: int = 0,
    keywords_on: bool = False,
    note: str = "",
) -> str:
    # 指令层带自动回退（过滤为空时改查全量），走到空文案说明真的一场都没有，
    # 不必再提示调过滤配置
    return _render_match_list(
        f"📅 近 {days} 天大赛（共 {len(matches)} 场{_stars_hint(min_stars)}）",
        matches,
        max_items,
        f"📅 未来 {days} 天没有任何比赛。",
        note,
    )


def format_today(
    matches: list[dict],
    max_items: int,
    min_stars: int = 0,
    keywords_on: bool = False,
    note: str = "",
) -> str:
    return _render_match_list(
        f"📅 今日赛程（共 {len(matches)} 场{_stars_hint(min_stars)}）",
        matches,
        max_items,
        "📅 未来 24 小时没有任何比赛。",
        note,
    )


def format_live(
    matches: list[dict], note: str = "", delayed: list[dict] | None = None
) -> str:
    if not matches and not delayed:
        return "🔴 当前没有正在进行的比赛。"
    lines = []
    if matches:
        lines.append(f"🔴 LIVE CENTER  |  {len(matches)} 场进行中")
        if note:
            lines.append(note)
        for index, m in enumerate(matches, start=1):
            stars = "★" * int(m.get("rating") or 0)
            team1 = str(m.get("team1") or "?")
            team2 = str(m.get("team2") or "?")
            series = str(m.get("maps_score") or "0:0")
            event = str(m.get("event", "")).strip()
            map_name = str(m.get("current_map_name") or "").strip()
            current_score = str(m.get("current_score") or "").strip()
            legacy_current = str(m.get("current_map") or "").strip()
            if map_name and current_score:
                small = f"{map_name}   {team1} {current_score} {team2}"
            elif map_name:
                small = f"{map_name}   比分暂未同步"
            elif legacy_current:
                small = legacy_current.removeprefix("当前 ")
            else:
                small = "当前地图暂未同步"
            lines.extend(
                [
                    "━━━━━━━━━━━━━━━━━━━━",
                    f"MATCH {index:02d}  |  LIVE  {stars}".rstrip(),
                    f"小局  {small}",
                    f"大局  {team1} {series} {team2}",
                    f"赛事  {event or '赛事信息暂缺'}",
                ]
            )
    if delayed:
        if lines:
            lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("⏳ 已过开赛时间（延迟或刚开打，暂无直播数据）")
        for m in delayed[:5]:
            lines.append(_match_line(m))
    return "\n".join(lines)


def format_map_started(snapshot: dict) -> str:
    team1 = str(snapshot.get("team1") or "?")
    team2 = str(snapshot.get("team2") or "?")
    name = str(snapshot.get("current_map_name") or "新地图")
    index = int(snapshot.get("active_map_index") or 0)
    total = int(snapshot.get("map_total") or 0)
    series = str(snapshot.get("maps_score") or "0:0")
    position = f"MAP {index}/{total}" if total else f"MAP {index}"
    current = str(snapshot.get("current_score") or "")
    small = f"{team1} {current} {team2}" if current else "比分暂未同步"
    event = str(snapshot.get("event") or "赛事信息暂缺")
    return "\n".join(
        [
            f"🗺️ {position}  |  {name} 已开始",
            "━━━━━━━━━━━━━━━━━━━━",
            f"小局  {small}",
            f"大局  {team1} {series} {team2}",
            f"赛事  {event}",
        ]
    )


def format_match_finished(snapshot: dict) -> str:
    team1 = str(snapshot.get("team1") or "?")
    team2 = str(snapshot.get("team2") or "?")
    series = str(snapshot.get("maps_score") or "?:?")
    version = str(snapshot.get("rating_version") or "")
    event = str(snapshot.get("event") or "赛事信息暂缺")
    lines = [
        "🏁 MATCH FINISHED",
        "━━━━━━━━━━━━━━━━━━━━",
        f"赛果  {team1} {series} {team2}",
        f"赛事  {event}",
    ]
    ratings = list(snapshot.get("ratings") or [])
    if ratings:
        lines.extend(["━━━━━━━━━━━━━━━━━━━━", f"RATING {version}".rstrip()])
        for team in ratings:
            players = "  |  ".join(
                f"{item.get('nickname', '?')} {item.get('rating', '?')}"
                for item in team.get("players", [])
            )
            lines.append(f"{team.get('team', '?')}\n{players or '暂未同步'}")
    else:
        lines.append("Rating 数据暂未同步。")
    return "\n".join(lines)


def format_team_not_live(name: str, upcoming: dict | None) -> str:
    """/hltv live <队名>：该队不在直播中时的答复。"""
    if upcoming is None:
        return f"「{name}」当前没有正在进行的比赛，未来 24 小时也没有已安排的场次。"
    when = (
        "已过预定开赛时间，可能延迟或即将开打"
        if upcoming.get("late")
        else f"{upcoming.get('date', '?')} {upcoming.get('time', '?')} 开赛"
    )
    return f"「{name}」暂未开打（{when}）：\n{_match_line(upcoming)}"


# ---------------------------------------------------------------- 赛果/排名/赛事


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


def format_ranking(
    teams: list[dict], max_items: int, title: str = "🏆 HLTV 战队排名", show_region: bool = False
) -> str:
    if not teams:
        return "🏆 暂无排名数据。"
    shown, omitted = _clip(teams, max_items)
    lines = [title]
    for t in shown:
        region = str(t.get("region", "")).strip()
        region_str = f" [{region}]" if show_region and region else ""
        lines.append(
            f"#{t.get('rank', '?')} {t.get('title', '?')}"
            f" — {_val(t.get('points'), '?')} 分{region_str}"
        )
    return "\n".join(lines) + _omitted_line(omitted)


def _event_date(s: Any) -> str:
    parts = str(s).split("-")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return f"{parts[1]}月{parts[0]}日"
    return str(s)


def format_events(events: list[dict], max_items: int) -> str:
    if not events:
        return "🎪 暂无赛事数据。"
    shown, omitted = _clip(events, max_items)
    lines = ["🎪 近期赛事"]
    for e in shown:
        lines.append(
            f"· {e.get('title', '?')}"
            f"  [{_event_date(e.get('start_date', '?'))} ~ {_event_date(e.get('end_date', '?'))}]"
        )
    return "\n".join(lines) + _omitted_line(omitted)


# ---------------------------------------------------------------- 战队/选手


def format_team(team: dict) -> str:
    lines = [f"🛡️ {_val(team.get('title'), '?')}"]

    ranks = []
    valve = _val(team.get("valve_rank"), "")
    world = _val(team.get("world_rank"), "")
    if valve:
        ranks.append(f"Valve #{valve}")
    if world:
        ranks.append(f"HLTV #{world}")
    lines.append(f"排名：{' | '.join(ranks) if ranks else '未上榜'}")

    players = team.get("players") or []
    if players:
        roster = "  ".join(
            f"{p.get('name', '?')}{_flag(p.get('cc', ''))}" for p in players
        )
        lines.append(f"阵容：{roster}")

    coach = _val(team.get("coach"), "")
    if coach:
        lines.append(f"教练：{coach}")

    stats = []
    age = _val(team.get("age"), "")
    if age and age != "0":
        stats.append(f"平均年龄 {age}")
    weeks = _val(team.get("weeks_top30"), "")
    if weeks:
        stats.append(f"Top30 周数 {weeks}")
    if stats:
        lines.append("、".join(stats))

    trophies = team.get("trophies") or []
    if trophies:
        head = "、".join(trophies[:3])
        more = f" 等 {len(trophies)} 座" if len(trophies) > 3 else ""
        lines.append(f"🏆 奖杯：{head}{more}")

    recent = team.get("recent") or []
    if recent:
        lines.append("近期比赛：")
        for r in recent[:3]:
            mark = "✅" if r.get("won") else "❌"
            date = f"{r['date']} " if r.get("date") else ""
            score = f" {r['score']}" if r.get("score") else ""
            lines.append(f"{mark} {date}vs {r.get('opp', '?')}{score}")
    return "\n".join(lines)


def format_player(player: dict) -> str:
    lines = [
        f"🎯 {_val(player.get('nickname'), '?')}（{_val(player.get('name'), '?')}）",
        f"战队：{_val(player.get('team'))}",
        f"国籍：{_val(player.get('nationality'))}  年龄：{_val(player.get('age'), '?')}",
    ]

    rating = _val(player.get("rating"), "")
    if rating:
        lines.append(f"{_val(player.get('rating_label'), 'Rating')}：{rating}")

    top20 = player.get("top20") or []
    if top20:
        ranking = "、".join(
            f"{item.get('year', '?')} #{item.get('rank', '?')}" for item in top20
        )
        lines.append(f"🥇 HLTV TOP20：{ranking}")

    major_wins = int(player.get("major_wins") or 0)
    major_mvps = int(player.get("major_mvps") or 0)
    trophies = int(player.get("total_trophies") or 0)
    mvps = int(player.get("total_mvps") or 0)
    honors = []
    if major_wins or major_mvps:
        major = f"Major {major_wins} 冠"
        if major_mvps:
            major += f"（{major_mvps} 次 MVP）"
        honors.append(major)
    if trophies:
        honors.append(f"赛事冠军 {trophies} 次")
    if mvps:
        honors.append(f"赛事 MVP {mvps} 次")
    lines.append(f"🏆 荣誉：{' | '.join(honors) if honors else '暂未收录'}")

    championships = player.get("championships") or []
    if championships:
        latest = "、".join(str(item.get("name") or "?") for item in championships[:3])
        more = " 等" if len(championships) > 3 else ""
        lines.append(f"最近冠军：{latest}{more}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 新闻


def format_news(items: list[dict], max_items: int) -> str:
    if not items:
        return "📰 今天还没有新闻。"
    shown, omitted = _clip(items, max_items)
    lines = ["📰 HLTV 今日新闻"]
    for i, it in enumerate(shown, start=1):
        tag = "🔥" if it.get("featured") else ""
        title = _val(it.get("title_zh") or it.get("title"), "?")
        lines.append(f"{i}. {tag}{title}")
    lines.append("👉 发送 /hltv news 序号 查看详情")
    return "\n".join(lines) + _omitted_line(omitted)


def format_news_detail(title: str, paragraphs: list[str], url: str) -> str:
    lines = [f"📰 {title}" if title else "📰 新闻详情"]
    if paragraphs:
        lines.append("")
        lines.extend(paragraphs)
    lines.append("")
    lines.append(f"原文：{url}")
    return "\n".join(lines)


HELP_TEXT = """🎮 HLTV 查询插件
/hltv today — 今日赛程（大赛）
/hltv matches [天数] — 近期大赛
/hltv live [队名] — 直播小局/大局比分（带队名会自动订阅）
/hltv live 取消 — 取消你的直播提醒
/hltv results [天数] — 近期赛果
/hltv ranking — Valve VRS 排名（默认全球）
/hltv ranking asia|europe|americas — 地区 VRS 排名
/hltv ranking hltv — HLTV 自家排名
/hltv events — 近期赛事
/hltv team <名称> — 战队详情图片卡（任意战队，支持空格）
/hltv player <昵称> — 选手生涯荣誉图片卡
/hltv news [序号] — 今日新闻（带序号看详情，自动翻译）
/hltv sub — 在本会话订阅每日赛程推送（unsub 退订）
/hltv help — 显示本帮助
所有子指令均可用中文，如：
/hltv 今日、/hltv 排名 亚洲、/hltv 战队 spirit、/hltv 新闻 2
群里直接发 /hltv 指令即可，无需 @ 机器人"""
