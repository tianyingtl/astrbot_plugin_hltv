"""Shared search aliases. Historical clubs remain separate from roster successors."""

import re
from typing import Any


TEAM_ALIAS_GROUPS = {
    "Natus Vincere": ("NAVI", "Na'Vi", "娜维", "天生赢家"),
    "fnatic": ("FNC", "FNTC", "F队", "大Fnatic"),
    "Ninjas in Pyjamas": ("NIP", "睡衣忍者", "忍者"),
    "Vitality": ("VIT", "Team Vitality", "蜜蜂", "小蜜蜂"),
    "Spirit": ("Team Spirit", "TS", "绿龙", "雪碧"),
    "Falcons": ("Team Falcons", "猎鹰", "猎鹰队"),
    "Astralis": ("AST", "A队", "A星", "阿斯特拉利斯"),
    "Liquid": ("Team Liquid", "TL", "液体", "液体队", "蓝马"),
    "MOUZ": ("mousesports", "老鼠", "鼠队", "老鼠队"),
    "The MongolZ": ("MongolZ", "蒙古", "蒙古人", "蒙古队"),
    "100 Thieves": ("100T", "百盗", "百盗战队", "100大盗"),
    "Virtus.pro": ("VP", "Virtus Pro", "熊队", "北极熊"),
    "Complexity": ("COL", "Complexity Gaming", "复杂度"),
    "Eternal Fire": ("EF", "永恒之火", "永恒烈焰"),
    "Rare Atom": ("RA", "稀有原子"),
    "TYLOO": ("天禄", "天禄战队"),
    "Lynn Vision": ("LV", "LVG", "Lynn Vision Gaming", "林恩愿景", "林视"),
    "Bilibili Gaming": ("BLG", "哔哩哔哩", "哔哩哔哩电竞"),
    "GamerLegion": ("GL", "Gamer Legion", "玩家军团"),
    "HEROIC": ("英勇", "英雄队"),
    "FURIA": ("FURIA Esports", "黑豹", "黑豹队"),
    "G2": ("G2 Esports", "G二", "G2战队"),
    "FaZe": ("FaZe Clan", "银河战舰"),
    "Cloud9": ("C9", "Cloud 9", "云九"),
    "ENCE": ("恩斯",),
    "BIG": ("Berlin International Gaming", "德国战车"),
    "OG": ("OG Esports",),
    "Aurora": ("Aurora Gaming", "极光"),
    "3DMAX": ("3D", "3D MAX"),
    "paiN": ("paiN Gaming", "痛苦"),
    "Imperial": ("Imperial Esports", "帝国"),
    "MIBR": ("Made in Brazil", "巴西制造"),
    "SAW": ("电锯",),
    "BetBoom": ("BB", "BetBoom Team"),
    "9z": ("9z Team",),
    "FlyQuest": ("FQ", "FLY"),
    "Wildcard": ("Wildcard Gaming", "外卡"),
    "NRG": ("NRG Esports",),
    "Passion UA": ("PassionUA",),
    "Gambit": ("Gambit Gaming", "Gambit Esports", "红星"),
    "Envy": ("EnVyUs", "Team EnVyUs", "Team Envy", "NV", "nV队"),
    "Dignitas": ("DIG", "Team Dignitas", "大眼", "大眼队"),
    "Renegades": ("RNG", "牛仔", "牛仔队"),
    "North": ("北狮", "北狮队"),
    "HellRaisers": ("HR", "地狱使者", "地狱男爵"),
    "FlipSid3": ("FlipSid3 Tactics", "Flipside", "F3"),
    "LDLC": ("Team LDLC", "LDLC OL"),
    "Titan": ("泰坦", "泰坦队"),
    "VeryGames": ("Very Games",),
    "SK": ("SK Gaming", "SK战队"),
    "Luminosity": ("LG", "Luminosity Gaming", "光辉"),
    "Immortals": ("IMT", "不朽", "不朽者"),
    "GODSENT": ("God Sent", "神之队"),
    "OpTic": ("OpTic Gaming", "绿墙"),
    "TSM": ("Team SoloMid", "SoloMid"),
    "Evil Geniuses": ("EG", "邪恶天才"),
    "iBUYPOWER": ("IBP", "IBUY POWER"),
    "Epsilon": ("Epsilon Esports",),
    "Copenhagen Flames": ("CPH Flames", "CPHF", "哥本哈根火焰"),
    "Sprout": ("Sprout Esports", "小树苗", "树苗"),
    "Space Soldiers": ("SS", "太空士兵", "太空战士"),
    "Movistar Riders": ("Movistar", "MR", "骑士"),
    "ViCi": ("VG", "Vici Gaming", "VG电竞"),
    "Flash": ("Flash Gaming", "FG", "闪电"),
    "MVP PK": ("MVPPK",),
    "NAVI Junior": ("NAVI青训", "NAVI二队"),
    "NAVI Youth": ("NAVI青年队",),
    "Spirit Academy": ("绿龙青训", "雪碧青训"),
    "MOUZ NXT": ("老鼠青训", "鼠队青训"),
    "Young Ninjas": ("NIP青训", "小忍者"),
}

PLAYER_ALIAS_GROUPS = {
    "s1mple": ("simple", "森破", "森哥", "S1"),
    "ZywOo": ("载物", "大番薯"),
    "NiKo": ("尼公子", "尼扣"),
    "m0NESY": ("monesy", "小孩", "小孩哥"),
    "sh1ro": ("shiro", "西罗"),
    "electroNic": ("电子哥", "电子", "elec"),
    "b1t": ("bit", "比特"),
    "ropz": ("rop", "爱沙尼亚拳击手"),
    "Twistzz": ("总监", "发型总监"),
    "rain": ("雨神", "雨男", "雨哥"),
    "broky": ("寒王", "西蓝花", "西兰花"),
    "karrigan": ("大表哥", "表哥", "凯瑞根"),
    "device": ("dev1ce", "阿汤哥", "汤哥"),
    "dupreeh": ("阿杜", "杜普瑞"),
    "Magisk": ("Magiskb0Y", "魔男"),
    "Xyp9x": ("九爷", "9爷", "Xyp"),
    "gla1ve": ("glaive", "格雷武"),
    "apEX": ("憨豆", "豆豆"),
    "Spinx": ("自旋",),
    "flameZ": ("小火苗",),
    "mezii": ("梅西",),
    "Aleksib": ("小李子",),
    "HooXi": ("呼吸", "呼吸哥"),
    "huNter-": ("hunter", "大堂哥"),
    "Snax": ("憨憨",),
    "pashaBiceps": ("pasha", "帕莎", "肌肉男"),
    "NEO": ("尼奥",),
    "TaZ": ("塔兹",),
    "olofmeister": ("olof", "奥拉夫"),
    "f0rest": ("forest", "森林哥", "森林"),
    "GeT_RiGhT": ("GTR", "狗头人"),
    "flusha": ("鼠标抬起", "抬鼠标"),
    "JW": ("猪猪", "猪头"),
    "KRIMZ": ("大光头",),
    "pronax": ("普罗纳克斯",),
    "kennyS": ("K神", "肯尼S", "肯尼"),
    "shox": ("shoxie", "法国陈赫"),
    "Happy": ("开心", "快乐男"),
    "NBK-": ("NBK",),
    "ScreaM": ("爆头哥", "一发男"),
    "Ex6TenZ": ("比利时吴彦祖",),
    "RpK": ("法国坦克", "坦克"),
    "SmithZz": ("史密斯",),
    "GuardiaN": ("瓜点", "瓜皇"),
    "oskar": ("奥斯卡", "捷克大叔"),
    "allu": ("阿鲁", "芬兰狙神"),
    "chrisJ": ("荷兰狙神",),
    "FalleN": ("教父", "巴西教父", "巴西狙神"),
    "coldzera": ("cold", "冷神"),
    "fer": ("F皇",),
    "TACO": ("塔可",),
    "boltz": ("博尔茨",),
    "KSCERATO": ("卡神", "卡斯瑞托"),
    "arT": ("艺术家",),
    "EliGE": ("鸡哥", "憨鸡", "油腻鸡"),
    "NAF": ("NAF-FLY", "树懒"),
    "Stewie2K": ("stew", "stewie", "烟中恶鬼", "烟男"),
    "nitr0": ("nitro", "美国队长"),
    "Skadoodle": ("Ska", "世嘉"),
    "autimatic": ("自动哥", "自动人"),
    "shroud": ("寿衣",),
    "jks": ("澳洲野兔",),
    "jkaem": ("杰克姆",),
    "XANTARES": ("X神", "土耳其狠人"),
    "woxic": ("左手狙",),
    "tabseN": ("德国战神",),
    "smooya": ("鼠鼠",),
    "Jame": ("JAME TIME", "上帝", "耶稣"),
    "AdreN": ("哈萨克斯坦吴彦祖",),
    "Dosia": ("D神", "赌神"),
    "mou": ("哈萨克斯坦狙神",),
    "Hobbit": ("霍比特", "霍比特人"),
    "BnTeT": ("印尼火神",),
    "Summer": ("fancy1", "白菜"),
    "somebody": ("小鬼",),
    "Attacker": ("攻击者",),
    "Mo": ("CaptainMo", "MO队", "馍", "馍队"),
}


def normalize_team_name(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", str(value or "").lower())


TEAM_ALIASES = {
    normalize_team_name(alias): canonical
    for canonical, aliases in TEAM_ALIAS_GROUPS.items()
    for alias in (canonical, *aliases)
}
PLAYER_ALIASES = {
    normalize_team_name(alias): canonical
    for canonical, aliases in PLAYER_ALIAS_GROUPS.items()
    for alias in (canonical, *aliases)
}


def team_query_variants(value: Any) -> tuple[str, ...]:
    original = str(value or "").strip()
    canonical = TEAM_ALIASES.get(normalize_team_name(original), original)
    return tuple(dict.fromkeys(item for item in (original, canonical) if item))


def team_name_matches(query: Any, candidate: Any) -> bool:
    needle = normalize_team_name(query)
    haystack = normalize_team_name(candidate)
    if not needle or not haystack:
        return False
    if needle in TEAM_ALIASES:
        # Known clubs must not match their academies or a roster's former club.
        return TEAM_ALIASES[needle] == TEAM_ALIASES.get(haystack, candidate)
    return needle in haystack or haystack in needle


def resolve_player_name(value: Any) -> str:
    original = str(value or "").strip()
    canonical = PLAYER_ALIASES.get(normalize_team_name(original), original)
    # Preserve exact spelling when it distinguishes official handles (NiKo/niko).
    return original if normalize_team_name(original) == normalize_team_name(canonical) else canonical


def player_nickname(player: dict) -> str:
    return str(player.get("nickName") or player.get("nickname") or player.get("name") or "")


def select_player(query: Any, players: list[dict]) -> dict | None:
    resolved = resolve_player_name(query)
    needle = normalize_team_name(resolved)
    if not needle:
        return None
    candidates = []
    for player in players:
        if not isinstance(player, dict):
            continue
        nick = player_nickname(player)
        key = normalize_team_name(resolve_player_name(nick))
        if key == needle or (needle not in PLAYER_ALIASES and needle in key):
            candidates.append(player)
    return min(
        candidates,
        key=lambda player: (
            player_nickname(player) != resolved,
            normalize_team_name(resolve_player_name(player_nickname(player))) != needle,
            len(player_nickname(player)),
        ),
        default=None,
    )
