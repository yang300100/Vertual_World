"""独立验证：从 SillyTavern PNG/JSON 的角色书识别人物并试提取资料。

仅用于观察样本效果，不连接项目的导入 API、数据库或 NPC 提示词。
用法：python scripts/probe_worldbook_characters.py 角色卡.png [--person 人名]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import struct
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 8 * 1024 * 1024
MAX_CARD_JSON_BYTES = 256 * 1024
NAME_LINE = re.compile(r"^\s*(?:name|姓名)\s*[:：]\s*(.+?)\s*$", re.I | re.M)
SFW_START = re.compile(r"^\s*#\s*SFW\b.*$", re.I | re.M)
SFW_END = re.compile(r"^\s*#\s*.*NSFW.*$", re.I | re.M)
SECTION = re.compile(r"^\s*#\s+(.+?)\s*$", re.M)
SECTIONS_TO_FIELDS = {
    "人物背景": "background",
    "外貌特征": "appearance",
    "性格与行为": "personality",
    "沟通特征": "communication",
}


def read_card(path: Path) -> dict:
    """Yuki 只解码这次验证需要的角色卡数据。"""
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("文件为空或超过 8 MiB")
    if path.suffix.lower() == ".png":
        if not raw.startswith(PNG_SIGNATURE):
            raise ValueError("不是有效的 PNG")
        position = len(PNG_SIGNATURE)
        metadata: dict[str, bytes] = {}
        while position + 12 <= len(raw):
            length = struct.unpack_from(">I", raw, position)[0]
            position += 4
            if length > MAX_BYTES or position + length + 8 > len(raw):
                raise ValueError("PNG 区块不完整或过大")
            kind = raw[position:position + 4]
            payload = raw[position + 4:position + 4 + length]
            crc = struct.unpack_from(">I", raw, position + 4 + length)[0]
            if binascii.crc32(kind + payload) & 0xFFFFFFFF != crc:
                raise ValueError("PNG 区块校验失败")
            position += 8 + length
            if kind == b"IEND":
                break
            if kind == b"tEXt" and b"\x00" in payload:
                key, value = payload.split(b"\x00", 1)
                if key in (b"chara", b"ccv2", b"ccv3"):
                    metadata[key.decode("ascii")] = value
            if kind == b"zTXt" and b"\x00" in payload:
                key, value = payload.split(b"\x00", 1)
                if key in (b"chara", b"ccv2", b"ccv3") and value.startswith(b"\x00"):
                    decoder = zlib.decompressobj()
                    decoded = decoder.decompress(value[1:], MAX_CARD_JSON_BYTES + 1)
                    if len(decoded) > MAX_CARD_JSON_BYTES or not decoder.eof:
                        raise ValueError("压缩的角色卡元数据过大或不完整")
                    metadata[key.decode("ascii")] = decoded
        else:
            raise ValueError("PNG 文件不完整")
        encoded = next(
            (metadata[key] for key in ("ccv3", "ccv2", "chara") if key in metadata), None,
        )
        if encoded is None:
            raise ValueError("未找到角色卡元数据")
        raw = base64.b64decode(encoded, validate=True)
    elif path.suffix.lower() != ".json":
        raise ValueError("只支持 PNG 或 JSON")
    if len(raw) > MAX_CARD_JSON_BYTES:
        raise ValueError("角色卡文本超过 256 KiB")
    card = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(card, dict) or not isinstance(card.get("data", card), dict):
        raise ValueError("角色卡结构无效")
    return card["data"] if "data" in card else card


def candidates(data: dict) -> tuple[list[dict], list[int]]:
    """Yuki 只把标题、别名和正文姓名互相印证的条目当作人物。"""
    entries = (data.get("character_book") or {}).get("entries") or []
    found: list[dict] = []
    other: list[int] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            other.append(index)
            continue
        title = str(entry.get("comment") or "").strip()
        aliases = [str(key).strip() for key in entry.get("keys", []) if isinstance(key, str)]
        content = str(entry.get("content") or "")
        match = NAME_LINE.search("\n".join(content.splitlines()[:35]))
        stated_name = match.group(1).strip() if match else ""
        names = [title, *aliases]
        if not stated_name or stated_name not in names or "{{user}}" in stated_name:
            other.append(index)
            continue
        found.append({
            "name": stated_name,
            "aliases": [alias for alias in aliases if alias != stated_name],
            "entry_index": index,
            "enabled": entry.get("enabled", True) is True,
            "constant": entry.get("constant", False) is True,
        })
    # 补充条目须由标题或关键词明确指向人物；只在正文提到名字不算归属。
    for person in found:
        name = person["name"]
        suggested = []
        for index in other:
            entry = entries[index]
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("comment") or "").strip()
            keys = [str(key).strip() for key in entry.get("keys", []) if isinstance(key, str)]
            if title.startswith(name) or name in keys:
                suggested.append(index)
        person["suggested_entry_indices"] = suggested
    return found, other


def extract_sfw_profile(entry: dict) -> dict | None:
    """Yuki 仅试取明确标注的普通人物设定，原文没有的栏目保持为空。"""
    content = str(entry.get("content") or "")
    start = SFW_START.search(content)
    if not start:
        return None
    end = SFW_END.search(content, start.end())
    sfw = content[start.end():end.start() if end else len(content)]
    parts = list(SECTION.finditer(sfw))
    fields: dict[str, str] = {}
    for key in ("name", "age", "gender"):
        match = re.search(rf"^\s*{key}\s*:\s*(.+?)\s*$", sfw, re.I | re.M)
        if match:
            fields[key] = match.group(1).strip()
    identities = re.search(r"^\s*identities\s*:\s*\n((?:\s*-\s*[^\n]+\n?)+)", sfw, re.I | re.M)
    if identities:
        fields["identities"] = identities.group(1).strip()
    for index, part in enumerate(parts):
        next_start = parts[index + 1].start() if index + 1 < len(parts) else len(sfw)
        title = part.group(1).split("(", 1)[0].strip()
        if title in SECTIONS_TO_FIELDS:
            fields[SECTIONS_TO_FIELDS[title]] = sfw[part.end():next_start].strip()
    return fields


def inspect(path: Path, person: str | None, include_entries: list[int] | None = None) -> dict:
    data = read_card(path)
    people, other = candidates(data)
    result = {
        "file": path.name,
        "worldbook_entries": len((data.get("character_book") or {}).get("entries") or []),
        "candidates": people,
        "unassigned_entry_indices": other,
    }
    if person is not None:
        selected = [item for item in people if person == item["name"] or person in item["aliases"]]
        if len(selected) != 1:
            raise ValueError(f"人物「{person}」未唯一匹配到条目")
        item = selected[0]
        source_indices = [item["entry_index"], *(include_entries or [])]
        if len(source_indices) != len(set(source_indices)) or any(
            index not in item["suggested_entry_indices"] for index in source_indices[1:]
        ):
            raise ValueError("补充条目未明确归属于所选人物")
        fields: dict[str, str] = {}
        review_indices = []
        for index in source_indices:
            entry = data["character_book"]["entries"][index]
            part = extract_sfw_profile(entry)
            if part is None:
                review_indices.append(index)
                continue
            for key, value in part.items():
                if value and value not in fields.get(key, ""):
                    fields[key] = "\n\n".join(filter(None, (fields.get(key), value)))
        result["selected"] = {
            "name": item["name"],
            "source_entry_indices": source_indices,
            "manual_review_entry_indices": review_indices,
            "status": "extracted_sfw" if fields and not review_indices else "manual_review",
            "fields": fields,
            "empty_card_fields": ["scenario", "mes_example", "first_mes", "creator_notes"],
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("card", type=Path)
    parser.add_argument("--person", help="按人物姓名或别名选择；仅输出明确标注的普通人物设定")
    parser.add_argument(
        "--include-entry", type=int, action="append", default=[],
        help="确认合并到所选人物的补充条目编号",
    )
    args = parser.parse_args()
    result = inspect(args.card, args.person, args.include_entry)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
