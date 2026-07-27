"""HLTV query card rendering with Pillow."""

import hashlib
import os
import random
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from .formatter import news_titles


ROOT = Path(__file__).resolve().parent.parent
WIDE_BACKGROUND = ROOT / "assets" / "backgrounds" / "chiaki_wide.png"
SMILE_BACKGROUND = ROOT / "assets" / "backgrounds" / "chiaki_smile.jpg"
PORTRAIT_BACKGROUND = ROOT / "assets" / "backgrounds" / "chiaki_portrait.jpg"
CARD_BASE_BACKGROUND = ROOT / "assets" / "card_base.png"
BACKGROUND_POOL = (
    WIDE_BACKGROUND,
    SMILE_BACKGROUND,
    PORTRAIT_BACKGROUND,
    CARD_BASE_BACKGROUND,
)
DEFAULT_OUTPUT_DIR = Path.home() / ".astrbot_plugin_hltv" / "cards"
BUNDLED_FONT = ROOT / "assets" / "fonts" / "HLTVCardSans-Regular.otf"
BUNDLED_FONT_BOLD = ROOT / "assets" / "fonts" / "HLTVCardSans-Bold.otf"
CARD_SIZE = (1600, 1000)
PLAYER_CARD_SIZE = (1600, 1400)
TOP20_CARD_SIZE = (1600, 1600)

INK = (247, 244, 245, 255)
MUTED = (196, 187, 191, 255)
ACCENT = (238, 157, 180, 255)
LINE = (255, 255, 255, 55)
GOOD = (132, 210, 170, 255)
BAD = (239, 137, 151, 255)


class RenderError(Exception):
    pass


def _pick_background(background_path: Path | None) -> Path:
    if background_path is not None:
        return Path(background_path)
    return random.choice(BACKGROUND_POOL)


def _font_candidates(bold: bool) -> list[Path | str]:
    custom = os.getenv("HLTV_CARD_FONT_BOLD" if bold else "HLTV_CARD_FONT")
    names = [Path(custom)] if custom else []
    names.append(BUNDLED_FONT_BOLD if bold else BUNDLED_FONT)
    if bold:
        names.extend(
            [
                Path("C:/Windows/Fonts/msyhbd.ttc"),
                Path("C:/Windows/Fonts/Dengb.ttf"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                "DejaVuSans-Bold.ttf",
            ]
        )
    else:
        names.extend(
            [
                Path("C:/Windows/Fonts/msyh.ttc"),
                Path("C:/Windows/Fonts/Deng.ttf"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                "DejaVuSans.ttf",
            ]
        )
    return names


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for candidate in _font_candidates(bold):
        try:
            return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    raise RenderError("No usable TrueType font found for profile card rendering.")


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    size: int,
    max_width: int,
    *,
    bold: bool = False,
    minimum: int = 18,
) -> ImageFont.FreeTypeFont:
    for current in range(size, minimum - 1, -2):
        font = _font(current, bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
    return _font(minimum, bold)


def _ellipsize(
    draw: ImageDraw.ImageDraw, text: Any, font: ImageFont.FreeTypeFont, max_width: int
) -> str:
    value = str(text or "").strip()
    if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
        return value
    while value and draw.textbbox((0, 0), value + "...", font=font)[2] > max_width:
        value = value[:-1]
    return value.rstrip() + "..."


def _base_canvas(
    background_path: Path,
    size: tuple[int, int] = CARD_SIZE,
    *,
    centering: tuple[float, float] = (0.5, 0.43),
) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    if not background_path.is_file():
        raise RenderError(f"Card background does not exist: {background_path}")
    with Image.open(background_path) as source:
        background = ImageOps.fit(
            source.convert("RGB"),
            size,
            Image.Resampling.LANCZOS,
            centering=centering,
        )
    background = ImageEnhance.Color(background).enhance(0.82).convert("RGBA")
    canvas = Image.alpha_composite(
        background, Image.new("RGBA", size, (10, 9, 13, 158))
    )
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rectangle(
        (0, 0, size[0] - 1, size[1] - 1),
        outline=(255, 255, 255, 55),
        width=2,
    )
    draw.rectangle((72, 64, 80, 132), fill=ACCENT)
    return canvas, draw


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str) -> None:
    draw.text(xy, text.upper(), font=_font(22, True), fill=ACCENT)


def _divider(
    draw: ImageDraw.ImageDraw, y: int, start: int = 72, end: int = 1528
) -> None:
    draw.line((start, y, end, y), fill=LINE, width=2)


def _metric(
    draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str, width: int
) -> None:
    _label(draw, (x, y), label)
    font = _fit_font(draw, value, 60, width, bold=True, minimum=34)
    draw.text((x, y + 32), _ellipsize(draw, value, font, width), font=font, fill=INK)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: Any,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int = 2,
) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    words = value.split()
    units = words if len(words) > 1 else list(value)
    separator = " " if len(words) > 1 else ""
    lines: list[str] = []
    current = ""
    for unit in units:
        candidate = unit if not current else f"{current}{separator}{unit}"
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = unit
            if len(lines) == max_lines:
                lines[-1] = _ellipsize(draw, lines[-1] + "...", font, max_width)
                return lines
        else:
            current = candidate
    if current and len(lines) < max_lines:
        lines.append(_ellipsize(draw, current, font, max_width))
    return lines


def _paste_icon(
    canvas: Image.Image,
    path: Any,
    box: tuple[int, int, int, int],
) -> bool:
    candidate = Path(str(path or ""))
    if not candidate.is_file():
        return False
    try:
        with Image.open(candidate) as source:
            icon = source.convert("RGBA")
            icon.thumbnail((box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS)
        x = box[0] + (box[2] - box[0] - icon.width) // 2
        y = box[1] + (box[3] - box[1] - icon.height) // 2
        canvas.alpha_composite(icon, (x, y))
        return True
    except OSError:
        return False


def _draw_top_badge(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    rank: Any,
) -> None:
    x1, y1, x2, y2 = box
    center = (x1 + x2) // 2
    draw.polygon(
        [
            (center - 48, y1 + 12),
            (center + 48, y1 + 12),
            (center + 34, y1 + 72),
            (center + 13, y1 + 91),
            (center - 13, y1 + 91),
            (center - 34, y1 + 72),
        ],
        fill=(238, 157, 180, 215),
        outline=(255, 234, 241, 230),
    )
    draw.rectangle((center - 9, y1 + 88, center + 9, y1 + 105), fill=ACCENT)
    draw.rounded_rectangle(
        (center - 36, y1 + 103, center + 36, y1 + 116),
        radius=5,
        fill=(255, 234, 241, 230),
    )
    value = f"#{rank}"
    font = _fit_font(draw, value, 34, 78, bold=True, minimum=22)
    bounds = draw.textbbox((0, 0), value, font=font)
    draw.text(
        (center - (bounds[2] - bounds[0]) // 2, y1 + 33),
        value,
        font=font,
        fill=(35, 24, 30, 255),
    )


def _section_header(
    draw: ImageDraw.ImageDraw, title: str, subtitle: str = ""
) -> None:
    _label(draw, (98, 69), "HLTV / 数据中心")
    title_font = _fit_font(draw, title, 64, 1280, bold=True, minimum=38)
    draw.text((72, 126), _ellipsize(draw, title, title_font, 1280), font=title_font, fill=INK)
    if subtitle:
        draw.text(
            (76, 210),
            _ellipsize(draw, subtitle, _font(25), 1370),
            font=_font(25),
            fill=MUTED,
        )
    _divider(draw, 260)


def _save(canvas: Image.Image, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / filename
    canvas.convert("RGB").save(output, "PNG", optimize=True)
    return output


def _safe_name(value: Any) -> str:
    raw = str(value or "unknown")
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", raw).strip("_")[:60]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}_{digest}" if cleaned else digest


def render_team_card(
    team: dict,
    *,
    background_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(_pick_background(background_path))
    title = str(team.get("title") or "Unknown Team")
    _label(draw, (98, 69), "HLTV / 战队档案")
    title_font = _fit_font(draw, title, 84, 1200, bold=True, minimum=48)
    draw.text(
        (72, 126),
        _ellipsize(draw, title, title_font, 1200),
        font=title_font,
        fill=INK,
    )

    valve = str(team.get("valve_rank") or "UNRANKED")
    hltv = str(team.get("world_rank") or "UNRANKED")
    _metric(
        draw, 72, 270, "Valve 排名", f"#{valve}" if valve.isdigit() else valve, 260
    )
    _metric(
        draw, 420, 270, "HLTV 排名", f"#{hltv}" if hltv.isdigit() else hltv, 260
    )
    age = str(team.get("age") or "-")
    _metric(draw, 768, 270, "平均年龄", age, 260)
    trophies = list(team.get("trophies") or [])
    _metric(draw, 1116, 270, "奖杯", str(len(trophies)), 260)

    _divider(draw, 420)
    _label(draw, (72, 454), "现役阵容")
    players = list(team.get("players") or [])[:6]
    roster_font = _font(30, True)
    for index, player in enumerate(players):
        col, row = index % 3, index // 3
        x, y = 72 + col * 496, 502 + row * 54
        cc = str(player.get("cc") or "").upper()
        name = str(player.get("name") or "?")
        suffix = f"  /  {cc}" if cc else ""
        draw.text(
            (x, y),
            _ellipsize(draw, name + suffix, roster_font, 430),
            font=roster_font,
            fill=INK,
        )

    coach = str(team.get("coach") or "暂无")
    weeks = str(team.get("weeks_top30") or "-")
    meta_font = _font(25)
    draw.text((72, 626), f"教练  {coach}", font=meta_font, fill=MUTED)
    draw.text((560, 626), f"TOP 30 周数  {weeks}", font=meta_font, fill=MUTED)

    _divider(draw, 680)
    _label(draw, (72, 714), "近期战绩")
    recent = list(team.get("recent") or [])[:5]
    form_font = _font(25, True)
    for index, result in enumerate(recent):
        y = 762 + index * 42
        won = bool(result.get("won"))
        draw.rectangle((72, y + 8, 86, y + 22), fill=GOOD if won else BAD)
        date = f"{result.get('date')}  " if result.get("date") else ""
        score = f"  {result.get('score')}" if result.get("score") else ""
        line = f"{date}{'胜' if won else '负'}  vs  {result.get('opp') or '?'}{score}"
        draw.text(
            (102, y),
            _ellipsize(draw, line, form_font, 630),
            font=form_font,
            fill=INK,
        )

    _label(draw, (820, 714), "奖杯")
    trophy_font = _font(24)
    if trophies:
        for index, trophy in enumerate(trophies[:6]):
            y = 760 + index * 36
            draw.text((820, y), f"{index + 1:02d}", font=_font(19, True), fill=ACCENT)
            draw.text(
                (884, y - 3),
                _ellipsize(draw, trophy, trophy_font, 620),
                font=trophy_font,
                fill=INK,
            )
    else:
        draw.text((820, 762), "暂无", font=trophy_font, fill=MUTED)

    return _save(canvas, output_dir, f"team_{_safe_name(title)}.png")


def render_player_card(
    player: dict,
    *,
    background_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(
        _pick_background(background_path), PLAYER_CARD_SIZE, centering=(0.5, 0.38)
    )
    nickname = str(player.get("nickname") or "Unknown Player")
    _label(draw, (98, 69), "HLTV / 选手档案")
    title_font = _fit_font(draw, nickname, 84, 1200, bold=True, minimum=50)
    draw.text(
        (72, 116),
        _ellipsize(draw, nickname, title_font, 1200),
        font=title_font,
        fill=INK,
    )

    real_name = str(player.get("name") or "姓名暂无")
    real_font = _fit_font(draw, real_name, 32, 900, minimum=24)
    draw.text(
        (76, 210),
        _ellipsize(draw, real_name, real_font, 900),
        font=real_font,
        fill=MUTED,
    )
    facts = "  /  ".join(
        value
        for value in (
            str(player.get("team") or "暂无战队"),
            str(player.get("nationality") or "国籍未知"),
            f"{player.get('age')} 岁" if player.get("age") else "",
        )
        if value
    )
    facts_font = _font(27, True)
    draw.text(
        (76, 258),
        _ellipsize(draw, facts, facts_font, 1200),
        font=facts_font,
        fill=INK,
    )

    rating = str(player.get("rating") or "-")
    rating_label = str(player.get("rating_label") or "Rating")
    _metric(draw, 72, 326, rating_label, rating, 260)
    _metric(
        draw,
        420,
        326,
        "Major 冠军",
        str(int(player.get("major_wins") or 0)),
        260,
    )
    _metric(
        draw,
        768,
        326,
        "赛事冠军",
        str(int(player.get("total_trophies") or 0)),
        260,
    )
    _metric(
        draw, 1116, 326, "MVP", str(int(player.get("total_mvps") or 0)), 260
    )

    _divider(draw, 468)
    _label(draw, (72, 498), "HLTV TOP 20 奖杯")
    top20 = list(player.get("top20") or [])
    if top20:
        cell_width = 182
        for index, item in enumerate(top20[:16]):
            col, row = index % 8, index // 8
            x, y = 72 + col * cell_width, 538 + row * 144
            icon_box = (x + 30, y, x + 124, y + 100)
            if not _paste_icon(canvas, item.get("icon_path"), icon_box):
                _draw_top_badge(
                    draw,
                    (x + 20, y, x + 134, y + 120),
                    item.get("rank", "?"),
                )
            label = f"{item.get('year')}  #{item.get('rank')}"
            label_font = _fit_font(draw, label, 23, 162, bold=True, minimum=18)
            bounds = draw.textbbox((0, 0), label, font=label_font)
            draw.text(
                (x + (162 - (bounds[2] - bounds[0])) // 2, y + 112),
                label,
                font=label_font,
                fill=INK,
            )
    else:
        draw.text((72, 554), "暂无入选", font=_font(28, True), fill=MUTED)

    _divider(draw, 842)
    championships = list(player.get("championships") or [])[:10]
    _label(draw, (72, 872), "赛事冠军")
    champ_font = _font(23)
    for index, item in enumerate(championships):
        col, row = index // 5, index % 5
        x, y = 72 + col * 748, 914 + row * 34
        name = str(item.get("name") or "?")
        prefix = "MAJOR" if item.get("major") else f"{index + 1:02d}"
        draw.text((x, y + 1), prefix, font=_font(18, True), fill=ACCENT)
        draw.text(
            (x + 92, y - 3),
            _ellipsize(draw, name, champ_font, 620),
            font=champ_font,
            fill=INK,
        )
    if not championships:
        draw.text((72, 916), "暂无", font=champ_font, fill=MUTED)

    _divider(draw, 1100)
    total_mvps = int(player.get("total_mvps") or 0)
    major_mvps = int(player.get("major_mvps") or 0)
    _label(
        draw,
        (72, 1130),
        f"MVP 荣誉  /  {total_mvps} 次  /  Major {major_mvps} 次",
    )
    mvp_box = (72, 1172, 226, 1340)
    if not _paste_icon(canvas, player.get("mvp_icon_path"), mvp_box):
        draw.ellipse(
            mvp_box,
            fill=(238, 157, 180, 205),
            outline=(255, 236, 242, 230),
            width=3,
        )
        count_text = str(total_mvps)
        count_font = _fit_font(draw, count_text, 56, 120, bold=True, minimum=36)
        bounds = draw.textbbox((0, 0), count_text, font=count_font)
        draw.text(
            (149 - (bounds[2] - bounds[0]) // 2, 1202),
            count_text,
            font=count_font,
            fill=(31, 22, 27, 255),
        )
        draw.text(
            (118, 1270),
            "MVP",
            font=_font(24, True),
            fill=(31, 22, 27, 255),
        )

    events = list(player.get("mvp_events") or [])[:8]
    if events:
        event_font = _font(22, True)
        for index, event in enumerate(events):
            col, row = index // 4, index % 4
            x, y = 270 + col * 644, 1174 + row * 43
            draw.text(
                (x, y), f"{index + 1:02d}", font=_font(18, True), fill=ACCENT
            )
            draw.text(
                (x + 54, y - 3),
                _ellipsize(draw, event, event_font, 545),
                font=event_font,
                fill=INK,
            )
    else:
        draw.text((270, 1180), "暂无 MVP 赛事记录", font=_font(24), fill=MUTED)

    return _save(canvas, output_dir, f"player_{_safe_name(nickname)}.png")


def render_matches_card(
    matches: list[dict],
    title: str,
    *,
    subtitle: str = "",
    background_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(_pick_background(background_path))
    _section_header(draw, title, subtitle)
    shown = list(matches)[:10]
    if not shown:
        draw.text((72, 330), "当前没有可显示的比赛", font=_font(34, True), fill=MUTED)
    team_font = _font(27, True)
    meta_font = _font(21)
    for index, match in enumerate(shown):
        col, row = index // 5, index % 5
        x, y = 72 + col * 748, 292 + row * 132
        if row:
            draw.line((x, y - 14, x + 680, y - 14), fill=LINE, width=1)
        date = str(match.get("date") or "").strip()
        time = str(match.get("time") or "").strip()
        when = "  ".join(value for value in (date, time) if value)
        draw.text((x, y), f"{index + 1:02d}", font=_font(20, True), fill=ACCENT)
        draw.text((x + 58, y), when or "时间待定", font=meta_font, fill=MUTED)
        rating = int(match.get("rating") or 0)
        if rating:
            draw.text((x + 548, y), f"LEVEL {rating}", font=_font(18, True), fill=ACCENT)
        teams = f"{match.get('team1') or 'TBD'}  vs  {match.get('team2') or 'TBD'}"
        draw.text(
            (x + 58, y + 34),
            _ellipsize(draw, teams, team_font, 620),
            font=team_font,
            fill=INK,
        )
        draw.text(
            (x + 58, y + 72),
            _ellipsize(draw, match.get("event") or "赛事信息暂缺", meta_font, 620),
            font=meta_font,
            fill=MUTED,
        )
    return _save(canvas, output_dir, f"matches_{_safe_name(title)}.png")


def render_live_card(
    matches: list[dict],
    *,
    note: str = "",
    footer: str = "",
    background_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(_pick_background(background_path))
    _section_header(draw, "LIVE CENTER", note or f"{len(matches)} 场比赛进行中")
    shown = list(matches)[:4]
    if not shown:
        draw.text((72, 330), "当前没有正在进行的比赛", font=_font(34, True), fill=MUTED)
    for index, match in enumerate(shown):
        col, row = index % 2, index // 2
        x, y = 72 + col * 748, 294 + row * 300
        draw.rounded_rectangle(
            (x, y, x + 680, y + 260),
            radius=6,
            fill=(16, 13, 19, 96),
            outline=(255, 255, 255, 50),
            width=2,
        )
        rating = int(match.get("rating") or 0)
        draw.text((x + 28, y + 22), f"MATCH {index + 1:02d}  /  LIVE", font=_font(21, True), fill=ACCENT)
        if rating:
            draw.text((x + 548, y + 22), f"LV {rating}", font=_font(19, True), fill=ACCENT)
        team1 = str(match.get("team1") or "?")
        team2 = str(match.get("team2") or "?")
        map_name = str(match.get("current_map_name") or "").strip()
        current = str(match.get("current_score") or "").strip()
        legacy = str(match.get("current_map") or "").strip().removeprefix("当前 ")
        if map_name and current:
            small = f"{map_name}   {team1} {current} {team2}"
        elif map_name:
            small = f"{map_name}   比分暂未同步"
        else:
            small = legacy or "当前地图暂未同步"
        draw.text((x + 28, y + 78), "小局", font=_font(20, True), fill=MUTED)
        small_font = _fit_font(draw, small, 31, 558, bold=True, minimum=22)
        draw.text(
            (x + 94, y + 72),
            _ellipsize(draw, small, small_font, 558),
            font=small_font,
            fill=INK,
        )
        series = str(match.get("maps_score") or "").strip()
        large = f"{team1}  {series}  {team2}" if series else f"{team1}  vs  {team2}"
        draw.text((x + 28, y + 132), "大局", font=_font(20, True), fill=MUTED)
        large_font = _fit_font(draw, large, 29, 558, bold=True, minimum=20)
        draw.text((x + 94, y + 126), _ellipsize(draw, large, large_font, 558), font=large_font, fill=INK)
        event = str(match.get("event") or "赛事信息暂缺")
        draw.text((x + 28, y + 194), "赛事", font=_font(20, True), fill=MUTED)
        draw.text((x + 94, y + 192), _ellipsize(draw, event, _font(22), 558), font=_font(22), fill=MUTED)
    if footer:
        draw.line((72, 930, 1528, 930), fill=LINE, width=2)
        draw.text((72, 950), _ellipsize(draw, footer, _font(22, True), 1456), font=_font(22, True), fill=ACCENT)
    return _save(canvas, output_dir, "live_center.png")


def render_results_card(
    results: list[dict],
    title: str,
    *,
    background_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(_pick_background(background_path))
    _section_header(draw, title, f"共 {len(results)} 场已结束比赛")
    shown = list(results)[:10]
    if not shown:
        draw.text((72, 330), "当前没有可显示的赛果", font=_font(34, True), fill=MUTED)
    for index, result in enumerate(shown):
        col, row = index // 5, index % 5
        x, y = 72 + col * 748, 292 + row * 132
        if row:
            draw.line((x, y - 14, x + 680, y - 14), fill=LINE, width=1)
        draw.text((x, y), f"{index + 1:02d}", font=_font(20, True), fill=ACCENT)
        draw.text((x + 58, y), str(result.get("date") or "日期待定"), font=_font(21), fill=MUTED)
        team1, team2 = str(result.get("team1") or "?"), str(result.get("team2") or "?")
        score = f"{result.get('score1', '?')} : {result.get('score2', '?')}"
        line = f"{team1}   {score}   {team2}"
        line_font = _fit_font(draw, line, 29, 622, bold=True, minimum=20)
        draw.text((x + 58, y + 34), _ellipsize(draw, line, line_font, 622), font=line_font, fill=INK)
        draw.text(
            (x + 58, y + 74),
            _ellipsize(draw, result.get("event") or "赛事信息暂缺", _font(21), 622),
            font=_font(21),
            fill=MUTED,
        )
    return _save(canvas, output_dir, f"results_{_safe_name(title)}.png")


def render_ranking_card(
    teams: list[dict],
    title: str,
    *,
    show_region: bool = False,
    background_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(_pick_background(background_path))
    _section_header(draw, title, f"展示前 {min(len(teams), 10)} 名")
    shown = list(teams)[:10]
    if not shown:
        draw.text((72, 330), "当前没有排名数据", font=_font(34, True), fill=MUTED)
    for index, team in enumerate(shown):
        col, row = index // 5, index % 5
        x, y = 72 + col * 748, 292 + row * 132
        if row:
            draw.line((x, y - 14, x + 680, y - 14), fill=LINE, width=1)
        rank = str(team.get("rank") or index + 1).lstrip("#")
        rank_font = _fit_font(draw, f"#{rank}", 48, 120, bold=True, minimum=32)
        draw.text((x, y + 18), f"#{rank}", font=rank_font, fill=ACCENT)
        name = str(team.get("title") or "?")
        name_font = _fit_font(draw, name, 31, 510, bold=True, minimum=22)
        draw.text((x + 140, y + 10), _ellipsize(draw, name, name_font, 510), font=name_font, fill=INK)
        points = str(team.get("points") or "-")
        region = str(team.get("region") or "").strip()
        meta = f"{points} 分"
        if show_region and region:
            meta += f"  /  {region}"
        draw.text((x + 142, y + 59), meta, font=_font(22), fill=MUTED)
    return _save(canvas, output_dir, f"ranking_{_safe_name(title)}.png")


def render_top20_card(
    players: list[dict],
    year: int,
    *,
    background_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    gold = (244, 190, 118, 255)
    gold_soft = (244, 190, 118, 105)
    selected_background = _pick_background(background_path)
    if not selected_background.is_file():
        raise RenderError(f"Card background does not exist: {selected_background}")
    with Image.open(selected_background) as source:
        background = ImageOps.fit(
            source.convert("RGB"), TOP20_CARD_SIZE, Image.Resampling.LANCZOS
        )
    background = ImageEnhance.Color(background).enhance(0.28).convert("RGBA")
    canvas = Image.alpha_composite(
        background, Image.new("RGBA", TOP20_CARD_SIZE, (4, 5, 7, 210))
    )
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rectangle((0, 0, 1599, 1599), outline=gold_soft, width=2)
    draw.text((38, 34), "TOP 20 PLAYERS", font=_font(76, True), fill=gold)
    draw.text((38, 112), f"OF {year}", font=_font(70, True), fill=gold)
    draw.text((1190, 72), "HLTV", font=_font(34, True), fill=INK)
    draw.text((1190, 116), "ANNUAL RANKING", font=_font(22, True), fill=gold)
    draw.line((38, 214, 1562, 214), fill=gold_soft, width=2)

    ranked = {
        int(player.get("rank") or 0): player
        for player in players
        if str(player.get("rank") or "").isdigit()
    }
    cell_width, cell_height = 368, 238
    for rank in range(1, 21):
        row, col = divmod(rank - 1, 4)
        x = 36 + col * 386
        y = 244 + row * 256
        player = ranked.get(rank, {})
        name = str(player.get("name") or "PENDING")
        image_path = Path(str(player.get("image_path") or ""))
        has_photo = False
        if image_path.is_file():
            try:
                with Image.open(image_path) as source:
                    photo = ImageOps.fit(
                        source.convert("RGB"),
                        (cell_width, cell_height),
                        Image.Resampling.LANCZOS,
                        centering=(0.58, 0.38),
                    ).convert("RGBA")
                photo = ImageEnhance.Color(photo).enhance(0.78)
                canvas.alpha_composite(photo, (x, y))
                has_photo = True
            except OSError:
                pass
        if not has_photo:
            fallback_pool = [path for path in BACKGROUND_POOL if path.is_file()]
            if fallback_pool:
                seed = int(
                    hashlib.sha256(f"{year}:{rank}:{name}".encode()).hexdigest()[:8],
                    16,
                )
                fallback_path = fallback_pool[seed % len(fallback_pool)]
                centering = (
                    0.42 + (seed % 17) / 100,
                    0.32 + ((seed >> 8) % 17) / 100,
                )
                with Image.open(fallback_path) as source:
                    photo = ImageOps.fit(
                        source.convert("RGB"),
                        (cell_width, cell_height),
                        Image.Resampling.LANCZOS,
                        centering=centering,
                    ).convert("RGBA")
                photo = ImageEnhance.Color(photo).enhance(0.72)
                canvas.alpha_composite(photo, (x, y))
            else:
                draw.rectangle(
                    (x, y, x + cell_width, y + cell_height),
                    fill=(22, 23, 28, 255),
                )

        draw.rectangle(
            (x, y + 164, x + cell_width, y + cell_height),
            fill=(3, 4, 6, 210),
        )
        border = gold if rank <= 3 else (244, 190, 118, 175)
        draw.rectangle(
            (x, y, x + cell_width, y + cell_height),
            outline=border,
            width=3 if rank <= 3 else 2,
        )
        draw.rectangle((x + 12, y + 12, x + 78, y + 52), fill=gold)
        draw.text(
            (x + 22, y + 17),
            f"#{rank}",
            font=_font(24, True),
            fill=(11, 10, 10, 255),
        )
        name_font = _fit_font(
            draw, name.upper(), 38, cell_width - 28, bold=True, minimum=25
        )
        draw.text(
            (x + 14, y + 171),
            _ellipsize(draw, name.upper(), name_font, cell_width - 28),
            font=name_font,
            fill=INK,
        )
        country = str(player.get("country") or "HLTV PLAYER").upper()
        draw.text(
            (x + 16, y + 211),
            _ellipsize(draw, country, _font(18, True), cell_width - 32),
            font=_font(18, True),
            fill=gold,
        )

    draw.text((38, 1540), "HLTV.ORG", font=_font(22, True), fill=gold)
    draw.text(
        (1110, 1540), "IMAGERY  5EPLAY", font=_font(22, True), fill=MUTED
    )
    return _save(canvas, output_dir, f"top20_{year}.png")


def render_top20_player_card(
    item: dict,
    *,
    background_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(
        _pick_background(background_path), centering=(0.5, 0.35)
    )
    year = int(item.get("year") or 0)
    rank = int(item.get("rank") or 0)
    name = str(item.get("name") or "?")
    _section_header(draw, f"HLTV TOP 20 / {year}", "年度选手排名  ·  个人专题")

    rank_text = f"#{rank:02d}"
    rank_font = _fit_font(draw, rank_text, 190, 430, bold=True, minimum=120)
    draw.text((72, 310), rank_text, font=rank_font, fill=ACCENT)
    _label(draw, (82, 545), "ANNUAL RANKING")
    draw.text((80, 586), str(year), font=_font(46, True), fill=INK)
    draw.line((560, 310, 560, 900), fill=LINE, width=2)

    name_font = _fit_font(draw, name, 92, 860, bold=True, minimum=52)
    draw.text((630, 300), name, font=name_font, fill=INK)
    _label(draw, (636, 430), "HLTV ARTICLE")

    title = str(item.get("title") or "").strip()
    title_font = _font(32, True)
    for index, line in enumerate(_wrap_text(draw, title, title_font, 850, 2)):
        draw.text((630, 470 + index * 44), line, font=title_font, fill=INK)

    description = str(item.get("description") or "").strip()
    if description:
        _label(draw, (636, 590), "SUMMARY")
        summary_font = _font(27)
        summary = (
            re.sub(r"\s+", "", description)
            if re.search(r"[\u3400-\u9fff]", description)
            else description
        )
        for index, line in enumerate(
            _wrap_text(draw, summary, summary_font, 850, 5)
        ):
            draw.text(
                (630, 632 + index * 42),
                line,
                font=summary_font,
                fill=MUTED,
            )
    draw.text((630, 890), "HLTV.ORG", font=_font(22, True), fill=ACCENT)
    return _save(
        canvas,
        output_dir,
        f"top20_{year}_{rank}_{_safe_name(name)}.png",
    )


def render_events_card(
    events: list[dict],
    *,
    background_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(
        _pick_background(background_path), centering=(0.5, 0.35)
    )
    _section_header(draw, "近期赛事", f"展示 {min(len(events), 10)} 项")
    shown = list(events)[:10]
    if not shown:
        draw.text((72, 330), "当前没有赛事数据", font=_font(34, True), fill=MUTED)
    for index, event in enumerate(shown):
        col, row = index // 5, index % 5
        x, y = 72 + col * 748, 292 + row * 132
        if row:
            draw.line((x, y - 14, x + 680, y - 14), fill=LINE, width=1)
        draw.text((x, y + 18), f"{index + 1:02d}", font=_font(23, True), fill=ACCENT)
        title = str(event.get("title") or "?")
        title_font = _fit_font(draw, title, 29, 610, bold=True, minimum=21)
        draw.text((x + 60, y + 8), _ellipsize(draw, title, title_font, 610), font=title_font, fill=INK)
        dates = f"{event.get('start_date') or '?'}  -  {event.get('end_date') or '?'}"
        draw.text((x + 60, y + 57), dates, font=_font(22), fill=MUTED)
    return _save(canvas, output_dir, "events.png")


def render_news_card(
    items: list[dict],
    *,
    background_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(
        _pick_background(background_path), centering=(0.5, 0.35)
    )
    _section_header(draw, "HLTV 今日新闻", f"展示 {min(len(items), 10)} 条，使用 /hltv news 序号查看详情")
    shown = list(items)[:10]
    if not shown:
        draw.text((72, 330), "今天还没有新闻", font=_font(34, True), fill=MUTED)
    for index, item in enumerate(shown):
        col, row = index // 5, index % 5
        x, y = 72 + col * 748, 292 + row * 132
        if row:
            draw.line((x, y - 14, x + 680, y - 14), fill=LINE, width=1)
        draw.text((x, y + 8), f"{index + 1:02d}", font=_font(21, True), fill=ACCENT)
        title, source = news_titles(item)
        title_font = _font(23 if source else 25, True)
        line_gap = 30 if source else 34
        for line_index, line in enumerate(_wrap_text(draw, title, title_font, 610, 2)):
            draw.text(
                (x + 60, y + 3 + line_index * line_gap),
                line,
                font=title_font,
                fill=INK,
            )
        if source:
            source_font = _font(18)
            source_width = 560 if item.get("featured") else 610
            draw.text(
                (x + 60, y + 75),
                _ellipsize(draw, source, source_font, source_width),
                font=source_font,
                fill=MUTED,
            )
        if item.get("featured"):
            draw.rectangle((x + 644, y + 92, x + 680, y + 98), fill=ACCENT)
    return _save(canvas, output_dir, "news.png")
