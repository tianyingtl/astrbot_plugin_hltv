"""Team / player profile card rendering with Pillow."""

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKGROUND = ROOT / "assets" / "card_base.png"
DEFAULT_OUTPUT_DIR = Path.home() / ".astrbot_plugin_hltv" / "cards"
CARD_SIZE = (1200, 760)

INK = (247, 244, 245, 255)
MUTED = (196, 187, 191, 255)
ACCENT = (238, 157, 180, 255)
LINE = (255, 255, 255, 55)
GOOD = (132, 210, 170, 255)
BAD = (239, 137, 151, 255)


class RenderError(Exception):
    pass


def _font_candidates(bold: bool) -> list[Path | str]:
    custom = os.getenv("HLTV_CARD_FONT_BOLD" if bold else "HLTV_CARD_FONT")
    names = [Path(custom)] if custom else []
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


def _base_canvas(background_path: Path) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    if not background_path.is_file():
        raise RenderError(f"Card background does not exist: {background_path}")
    with Image.open(background_path) as source:
        background = ImageOps.fit(
            source.convert("RGB"), CARD_SIZE, Image.Resampling.LANCZOS, centering=(0.5, 0.43)
        )
    background = ImageEnhance.Color(background).enhance(0.78).convert("RGBA")
    veil = Image.new("RGBA", CARD_SIZE, (9, 8, 12, 34))
    background = Image.alpha_composite(background, veil)

    gradient = Image.new("RGBA", CARD_SIZE)
    pixels = gradient.load()
    for x in range(CARD_SIZE[0]):
        if x < 720:
            alpha = 242
        else:
            alpha = max(52, int(242 - (x - 720) * 0.42))
        for y in range(CARD_SIZE[1]):
            pixels[x, y] = (12, 11, 16, alpha)
    canvas = Image.alpha_composite(background, gradient)
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rectangle((0, 0, CARD_SIZE[0] - 1, CARD_SIZE[1] - 1), outline=(255, 255, 255, 38), width=2)
    draw.rectangle((68, 58, 74, 112), fill=ACCENT)
    return canvas, draw


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str) -> None:
    draw.text(xy, text.upper(), font=_font(18, True), fill=ACCENT)


def _divider(draw: ImageDraw.ImageDraw, y: int, width: int = 690) -> None:
    draw.line((70, y, width, y), fill=LINE, width=2)


def _metric(
    draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str, width: int
) -> None:
    _label(draw, (x, y), label)
    font = _fit_font(draw, value, 46, width, bold=True, minimum=26)
    draw.text((x, y + 25), _ellipsize(draw, value, font, width), font=font, fill=INK)


def _safe_name(value: Any) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "unknown")).strip("_")
    return cleaned[:80] or "unknown"


def render_team_card(
    team: dict,
    *,
    background_path: Path = DEFAULT_BACKGROUND,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(background_path)
    title = str(team.get("title") or "Unknown Team")
    _label(draw, (92, 61), "HLTV / 战队档案")
    title_font = _fit_font(draw, title, 62, 610, bold=True, minimum=36)
    draw.text((70, 112), _ellipsize(draw, title, title_font, 610), font=title_font, fill=INK)

    valve = str(team.get("valve_rank") or "UNRANKED")
    hltv = str(team.get("world_rank") or "UNRANKED")
    _metric(draw, 70, 207, "Valve 排名", f"#{valve}" if valve.isdigit() else valve, 205)
    _metric(draw, 305, 207, "HLTV 排名", f"#{hltv}" if hltv.isdigit() else hltv, 205)
    age = str(team.get("age") or "-")
    _metric(draw, 540, 207, "平均年龄", age, 145)

    _divider(draw, 302)
    _label(draw, (70, 329), "现役阵容")
    players = list(team.get("players") or [])[:6]
    roster_font = _font(25, True)
    for index, player in enumerate(players):
        col, row = index % 2, index // 2
        x, y = 70 + col * 315, 366 + row * 43
        cc = str(player.get("cc") or "").upper()
        name = str(player.get("name") or "?")
        suffix = f"  /  {cc}" if cc else ""
        draw.text((x, y), _ellipsize(draw, name + suffix, roster_font, 285), font=roster_font, fill=INK)

    coach = str(team.get("coach") or "暂无")
    weeks = str(team.get("weeks_top30") or "-")
    meta_font = _font(20)
    draw.text((70, 500), f"教练  {coach}", font=meta_font, fill=MUTED)
    draw.text((390, 500), f"TOP 30 周数  {weeks}", font=meta_font, fill=MUTED)

    _divider(draw, 540)
    _label(draw, (70, 565), "近期战绩")
    recent = list(team.get("recent") or [])[:3]
    form_font = _font(21, True)
    for index, result in enumerate(recent):
        y = 602 + index * 34
        won = bool(result.get("won"))
        draw.rectangle((70, y + 7, 81, y + 18), fill=GOOD if won else BAD)
        date = f"{result.get('date')}  " if result.get("date") else ""
        score = f"  {result.get('score')}" if result.get("score") else ""
        line = f"{date}{'胜' if won else '负'}  vs  {result.get('opp') or '?'}{score}"
        draw.text((94, y), _ellipsize(draw, line, form_font, 590), font=form_font, fill=INK)

    trophies = list(team.get("trophies") or [])
    if trophies:
        trophy_font = _font(18)
        latest = " / ".join(str(item) for item in trophies[:2])
        draw.text((70, 713), _ellipsize(draw, f"奖杯  {latest}", trophy_font, 660), font=trophy_font, fill=MUTED)

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"team_{_safe_name(title)}.png"
    canvas.convert("RGB").save(output, "PNG", optimize=True)
    return output


def render_player_card(
    player: dict,
    *,
    background_path: Path = DEFAULT_BACKGROUND,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    canvas, draw = _base_canvas(background_path)
    nickname = str(player.get("nickname") or "Unknown Player")
    _label(draw, (92, 61), "HLTV / 选手档案")
    title_font = _fit_font(draw, nickname, 66, 600, bold=True, minimum=38)
    draw.text((70, 108), _ellipsize(draw, nickname, title_font, 600), font=title_font, fill=INK)

    real_name = str(player.get("name") or "姓名暂无")
    real_font = _fit_font(draw, real_name, 25, 600, minimum=19)
    draw.text((72, 181), _ellipsize(draw, real_name, real_font, 600), font=real_font, fill=MUTED)
    facts = "  /  ".join(
        value
        for value in (
            str(player.get("team") or "暂无战队"),
            str(player.get("nationality") or "国籍未知"),
            f"{player.get('age')} 岁" if player.get("age") else "",
        )
        if value
    )
    facts_font = _font(20, True)
    draw.text((72, 224), _ellipsize(draw, facts, facts_font, 610), font=facts_font, fill=INK)

    rating = str(player.get("rating") or "-")
    rating_label = str(player.get("rating_label") or "Rating")
    _metric(draw, 70, 292, rating_label, rating, 175)
    _metric(draw, 270, 292, "Major 冠军", str(int(player.get("major_wins") or 0)), 150)
    _metric(draw, 445, 292, "赛事冠军", str(int(player.get("total_trophies") or 0)), 120)
    _metric(draw, 590, 292, "MVP", str(int(player.get("total_mvps") or 0)), 100)

    _divider(draw, 405)
    _label(draw, (70, 435), "HLTV TOP 20 历史")
    top20 = list(player.get("top20") or [])
    top_text = "  /  ".join(
        f"{item.get('year')}  #{item.get('rank')}" for item in top20[-6:]
    ) or "暂无入选"
    top_font = _fit_font(draw, top_text, 25, 620, bold=True, minimum=19)
    draw.text((70, 472), _ellipsize(draw, top_text, top_font, 620), font=top_font, fill=INK)

    _label(draw, (70, 530), "生涯荣誉")
    major_mvps = int(player.get("major_mvps") or 0)
    highlight = f"MAJOR MVP  {major_mvps}   /   赛事 MVP  {int(player.get('total_mvps') or 0)}"
    draw.text((70, 566), highlight, font=_font(23, True), fill=INK)

    championships = list(player.get("championships") or [])[:3]
    _label(draw, (70, 625), "最近冠军")
    champ_font = _font(19)
    for index, item in enumerate(championships):
        name = str(item.get("name") or "?")
        prefix = "MAJOR" if item.get("major") else f"0{index + 1}"
        draw.text((70, 660 + index * 28), prefix, font=_font(16, True), fill=ACCENT)
        draw.text((145, 657 + index * 28), _ellipsize(draw, name, champ_font, 540), font=champ_font, fill=INK)

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"player_{_safe_name(nickname)}.png"
    canvas.convert("RGB").save(output, "PNG", optimize=True)
    return output
