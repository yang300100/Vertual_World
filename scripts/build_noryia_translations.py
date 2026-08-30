"""生成 Noryia 人文数据的中文展示译名，保留原始名称便于追溯。"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/worldbuilding/maps/map_new/data"
OUTPUT = ROOT / "docs/worldbuilding/maps/map_new/data/translations.json"

STATE_NAMES = {
    "Engai": "恩盖",
    "Biok": "比奥克",
    "Srach": "斯拉克",
    "Grur": "格鲁尔",
    "Vreagab": "弗瑞阿加布",
    "Brat": "布拉特",
    "Zrio": "泽里奥",
    "Bug": "巴格",
    "Olerin": "奥勒林",
    "Kyathlulil": "凯斯鲁利尔",
    "Shemnaselu": "谢姆纳塞卢",
    "Milstar": "米尔斯塔",
    "Giruzar": "吉鲁扎尔",
    "Karion": "卡里昂",
    "Eartland": "厄特兰",
    "Solka": "索尔卡",
    "Tolnarg": "托尔纳格",
    "Digez": "迪盖兹",
    "Yethmel": "叶斯梅尔",
    "Dawnwalia": "唐瓦利亚",
}
CAPITAL_NAMES = {
    "Phax": "法克斯",
    "Phig": "菲格",
    "Zel": "泽尔",
    "Duzec": "杜泽克",
    "Vreagab": "弗瑞阿加布",
    "Brat": "布拉特",
    "Zrio": "泽里奥",
    "Bug": "巴格",
    "Dretheas": "德雷西亚斯",
    "Elase": "埃利斯",
    "Genosanil": "格诺萨尼尔",
    "Cryshield": "克赖志尔",
    "Memruch": "梅姆鲁赫",
    "Myths": "米斯",
    "Drayhost": "德雷霍斯特",
    "Bothar": "博塔尔",
    "Tolnarg": "托尔纳格",
    "Razinb": "拉津布",
    "Eliemaran": "埃利马兰",
    "Wilhalmi": "维尔哈米",
}
FORM_NAMES = {
    "Republic": "共和国",
    "Empire": "帝国",
    "Kingdom": "王国",
    "Grand Duchy": "大公国",
    "Duchy": "公国",
    "Principality": "侯国",
    "Protectorate": "保护国",
    "Satrapy": "总督领",
    "Theocracy": "神权国",
    "Brotherhood": "兄弟会",
    "Generic": "政体",
}


def transliterate(value: str) -> str:
    """对生成器随机专名作稳定音译，不试图改变其世界观含义。"""

    groups = {
        "sch": "施",
        "sh": "什",
        "ch": "奇",
        "ph": "菲",
        "th": "斯",
        "qu": "夸",
        "ck": "克",
        "zh": "日",
        "gh": "格",
        "ng": "恩",
        "ee": "伊",
        "oo": "乌",
        "ai": "艾",
        "ay": "艾",
        "au": "奥",
        "ea": "伊",
        "ou": "欧",
        "ia": "亚",
    }
    letters = {
        "a": "阿",
        "b": "布",
        "c": "克",
        "d": "德",
        "e": "伊",
        "f": "弗",
        "g": "格",
        "h": "赫",
        "i": "伊",
        "j": "杰",
        "k": "卡",
        "l": "尔",
        "m": "姆",
        "n": "恩",
        "o": "奥",
        "p": "普",
        "q": "奇",
        "r": "尔",
        "s": "斯",
        "t": "特",
        "u": "乌",
        "v": "维",
        "w": "沃",
        "x": "西",
        "y": "伊",
        "z": "泽",
    }
    text = value.strip()
    result: list[str] = []
    index = 0
    lowered = text.lower()
    while index < len(lowered):
        if lowered[index] in " -'":
            index += 1
            continue
        group = next((key for key in groups if lowered.startswith(key, index)), None)
        if group:
            result.append(groups[group])
            index += len(group)
        else:
            result.append(letters.get(lowered[index], lowered[index]))
            index += 1
    translated = "".join(result)
    return re.sub(r"(.)\1+", r"\1", translated) or "未命名"


def display_name(value: str, mapping: dict[str, str] | None = None) -> str:
    if mapping and value in mapping:
        return mapping[value]
    return transliterate(value)


def read_csv(name: str) -> list[dict[str, str]]:
    with (DATA / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    burgs = read_csv("Noryia Burgs 2026-08-29-11-38.csv")
    states = read_csv("Noryia States 2026-08-29-11-38.csv")
    provinces = read_csv("Noryia Provinces 2026-08-29-11-39.csv")
    rivers = read_csv("Noryia Rivers 2026-08-29-11-39.csv")
    routes = read_csv("Noryia Routes 2026-08-29-11-39.csv")
    result = {
        "source": "Noryia 人文地理导出",
        "policy": "城镇详细地图图片保持原样；source_name 永久保留，display_name 用于前端与运行时。",
        "states": {
            row["State"]: {
                "source_name": row["State"],
                "display_name": display_name(row["State"], STATE_NAMES),
                "form_display": FORM_NAMES.get(row["Form"], row["Form"]),
                "full_name_display": (
                    f"{display_name(row['State'], STATE_NAMES)}"
                    f"{FORM_NAMES.get(row['Form'], '政体')}"
                ),
            }
            for row in states
        },
        "provinces": {
            row["Province"]: {
                "source_name": row["Province"],
                "display_name": display_name(row["Province"]),
            }
            for row in provinces
            if row["Province"]
        },
        "cities": {
            row["Id"]: {
                "source_name": row["Burg"],
                "display_name": display_name(row["Burg"], CAPITAL_NAMES),
                "state_display": display_name(row["State"], STATE_NAMES),
                "province_display": display_name(row["Province"]),
            }
            for row in burgs
        },
        "rivers": {
            row["Id"]: {"source_name": row["River"], "display_name": display_name(row["River"])}
            for row in rivers
        },
        "routes": {
            row["Id"]: {"source_name": row["Route"], "display_name": display_name(row["Route"])}
            for row in routes
        },
        "forms": FORM_NAMES,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
