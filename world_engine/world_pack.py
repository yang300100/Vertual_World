"""从 docs/world.json 加载世界观，并把存档固定到世界观 ID。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from world_engine.config import Settings
from world_engine.database import Database
from world_engine.repository import WorldRepository, to_iso, utc_now


class WorldPackError(ValueError):
    """世界观文件缺失、不合法，或与同 ID 存档冲突。"""


def _inside(root: Path, value: str, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or re.match(r"^[A-Za-z]:", value):
        raise WorldPackError(f"世界观路径无效：{value!r}")
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise WorldPackError(f"世界观路径越界：{value}")
    if (not path.is_dir() if directory else not path.is_file()):
        raise WorldPackError(f"世界观路径不存在：{value}")
    return path


def _json_file(path: Path, expected: type) -> object:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorldPackError(f"无法读取世界观文件 {path.name}：{exc}") from exc
    if not isinstance(data, expected):
        raise WorldPackError(f"世界观文件 {path.name} 的顶层类型应为 {expected.__name__}")
    return data


def _unique_keys(items: list, label: str) -> set[str]:
    keys: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"].strip():
            raise WorldPackError(f"{label}每条记录必须有非空 key")
        if item["key"] in keys:
            raise WorldPackError(f"{label}的 key 重复：{item['key']}")
        keys.add(item["key"])
    return keys


@dataclass(frozen=True)
class WorldPack:
    root: Path
    id: str
    revision: int
    name: str
    start_time: datetime
    locations: list[dict]
    characters: list[dict]
    species: list[dict]
    maps: list[dict]
    map_features: list[dict]
    knowledge_paths: tuple[Path, ...]
    knowledge_excluded_filenames: tuple[str, ...]
    assets_root: Path
    map_layers: list[dict[str, str]]
    player: dict
    fingerprint: str


def load_world_pack(root: Path) -> WorldPack:
    root = root.resolve()
    manifest_path = _inside(root, "world.json")
    manifest = _json_file(manifest_path, dict)
    if type(manifest.get("format_version")) is not int or manifest["format_version"] != 1:
        raise WorldPackError("world.json 的 format_version 必须为 1")
    pack_id = manifest.get("id")
    if not isinstance(pack_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", pack_id):
        raise WorldPackError("世界观 id 只能使用小写字母、数字和连字符")
    revision = manifest.get("revision")
    if type(revision) is not int or revision < 1:
        raise WorldPackError("世界观 revision 必须为正整数")
    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WorldPackError("世界观 name 不能为空")
    try:
        start_time = datetime.fromisoformat(manifest["start_time"])
        if start_time.tzinfo is None:
            raise ValueError("缺少时区")
    except (KeyError, TypeError, ValueError) as exc:
        raise WorldPackError("世界观 start_time 必须为带时区的 ISO 时间") from exc
    required_paths = ("locations", "characters", "maps", "species", "assets_root")
    for key in required_paths:
        if key not in manifest:
            raise WorldPackError(f"world.json 缺少必需路径：{key}")
    data_paths = {key: _inside(root, manifest[key]) for key in ("locations", "characters", "maps", "species")}
    if "map_features" in manifest:
        data_paths["map_features"] = _inside(root, manifest["map_features"])
    locations = _json_file(data_paths["locations"], list)
    characters = _json_file(data_paths["characters"], list)
    species = _json_file(data_paths["species"], list)
    maps = _json_file(data_paths["maps"], list)
    features = _json_file(data_paths["map_features"], list) if "map_features" in data_paths else []
    location_keys = _unique_keys(locations, "地点")
    _unique_keys(characters, "人物")
    species_ids = set()
    for item in species:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or item["id"] in species_ids:
            raise WorldPackError("species 中的 id 必须非空且唯一")
        if not isinstance(item.get("display_name"), str) or not item["display_name"] or type(item.get("gestation_world_days")) is not int or item["gestation_world_days"] < 0 or type(item.get("adult_age_world_years")) is not int or item["adult_age_world_years"] < 0:
            raise WorldPackError(f"物种 {item['id']} 的名称或年龄参数无效")
        if not isinstance(item.get("compatible_species", [item["id"]]), list) or not all(isinstance(value, str) for value in item.get("compatible_species", [item["id"]])):
            raise WorldPackError(f"物种 {item['id']} 的 compatible_species 必须是数组")
        species_ids.add(item["id"])
    if not species_ids:
        raise WorldPackError("世界观至少需要一个物种")
    _unique_keys(maps, "地图")
    _unique_keys(features, "地图要素")
    if not locations:
        raise WorldPackError("世界观至少需要一个地点")
    for location in locations:
        if not isinstance(location.get("name"), str) or not location["name"]:
            raise WorldPackError("地点 name 不能为空")
        if not isinstance(location.get("kind", "place"), str) or not isinstance(location.get("resources", {}), dict):
            raise WorldPackError(f"地点资料无效：{location['key']}")
        lon, lat = location.get("longitude"), location.get("latitude")
        if type(lon) not in (int, float) or not -180 <= lon <= 180 or type(lat) not in (int, float) or not -90 <= lat <= 90:
            raise WorldPackError(f"地点坐标无效：{location['key']}")
        if type(location.get("area_radius_km", 1)) not in (int, float) or location.get("area_radius_km", 1) < 0:
            raise WorldPackError(f"地点范围无效：{location['key']}")
    if len({item["name"] for item in locations}) != len(locations):
        raise WorldPackError("地点名称不可重复")
    parent_by_key = {item["key"]: item.get("parent") for item in locations}
    for key, parent in parent_by_key.items():
        visited = {key}
        while parent is not None:
            if parent not in parent_by_key or parent in visited:
                raise WorldPackError(f"地点 {key} 的 parent 不存在或形成循环")
            visited.add(parent)
            parent = parent_by_key[parent]
    for character in characters:
        if character.get("location") not in location_keys:
            raise WorldPackError(f"人物 {character['key']} 引用了不存在的地点")
        if not isinstance(character.get("name"), str) or not character["name"]:
            raise WorldPackError("人物 name 不能为空")
        if character.get("species", "human") not in species_ids:
            raise WorldPackError(f"人物 {character['key']} 引用了不存在的物种")
        if any(type(character.get(field, default)) is not int or not low <= character.get(field, default) <= high for field, default, low, high in (("energy", 80, 0, 100), ("satiety", 80, 0, 100), ("money", 0, 0, 10**12))):
            raise WorldPackError(f"人物 {character['key']} 的状态值无效")
        if any(not isinstance(character.get(field, []), list) or not all(isinstance(value, str) for value in character.get(field, [])) for field in ("traits", "goals")):
            raise WorldPackError(f"人物 {character['key']} 的 traits/goals 必须为文字数组")
    if len({item["name"] for item in characters}) != len(characters):
        raise WorldPackError("人物名称不可重复")
    assets_root = _inside(root, manifest["assets_root"], directory=True)
    for item in maps:
        _inside(assets_root, item.get("asset_path"))
        if not isinstance(item.get("name"), str) or not item["name"]:
            raise WorldPackError(f"地图 {item['key']} 缺少 name")
        if not isinstance(item.get("width_pixels"), int) or item["width_pixels"] <= 0 or not isinstance(item.get("height_pixels"), int) or item["height_pixels"] <= 0:
            raise WorldPackError(f"地图尺寸无效：{item['key']}")
        west, east = item.get("min_longitude", -180), item.get("max_longitude", 180)
        south, north = item.get("min_latitude", -90), item.get("max_latitude", 90)
        if any(type(value) not in (int, float) for value in (west, east, south, north)) or not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise WorldPackError(f"地图边界无效：{item['key']}")
        if item.get("location") is not None and item["location"] not in location_keys:
            raise WorldPackError(f"地图 {item['key']} 引用了不存在的地点")
    if len({item["asset_path"] for item in maps}) != len(maps):
        raise WorldPackError("地图 asset_path 不可重复")
    for item in features:
        if item.get("location") is not None and item["location"] not in location_keys:
            raise WorldPackError(f"地图要素 {item['key']} 引用了不存在的地点")
        if not isinstance(item.get("name"), str) or type(item.get("longitude")) not in (int, float) or type(item.get("latitude")) not in (int, float) or not -180 <= item["longitude"] <= 180 or not -90 <= item["latitude"] <= 90:
            raise WorldPackError(f"地图要素无效：{item['key']}")
    raw_knowledge = manifest.get("knowledge", [])
    if not isinstance(raw_knowledge, list):
        raise WorldPackError("knowledge 必须为目录路径数组")
    knowledge_paths = tuple(_inside(root, value, directory=True) for value in raw_knowledge)
    excluded_filenames = manifest.get("knowledge_exclude_files", [])
    if not isinstance(excluded_filenames, list) or not all(isinstance(name, str) and Path(name).name == name for name in excluded_filenames):
        raise WorldPackError("knowledge_exclude_files 必须是文件名数组")
    raw_layers = manifest.get("map_layers", [])
    if not isinstance(raw_layers, list):
        raise WorldPackError("map_layers 必须为路径或图层对象数组")
    layers = []
    for value in raw_layers:
        asset_path = value if isinstance(value, str) else value.get("asset_path") if isinstance(value, dict) else None
        asset = _inside(assets_root, asset_path)
        layers.append({"asset_path": asset_path, "label": value.get("label", asset.stem) if isinstance(value, dict) else asset.stem, "format": asset.suffix.lstrip(".").lower()})
    player = manifest.get("player", {"species": "human", "money": 0, "skills": [], "items": []})
    if not isinstance(player, dict) or not isinstance(player.get("species"), str) or not isinstance(player.get("money"), int) or player["money"] < 0 or not isinstance(player.get("skills"), list) or not all(isinstance(skill, str) for skill in player["skills"]) or not isinstance(player.get("items"), list):
        raise WorldPackError("player 的 species、money、skills、items 配置无效")
    if player["species"] not in species_ids:
        raise WorldPackError("player 引用了不存在的物种")
    for item in player["items"]:
        if not isinstance(item, dict) or any(not isinstance(item.get(field), str) or not item[field] for field in ("type_id", "name", "category")) or type(item.get("quantity")) is not int or item["quantity"] < 0 or item.get("container", "character_inventory") not in {"character_inventory", "character_equipment"}:
            raise WorldPackError("player.items 中有无效物品")
    digest = hashlib.sha256()
    signed_files = {manifest_path, *data_paths.values()}
    for knowledge in knowledge_paths:
        for path in knowledge.rglob("*.md"):
            if path.is_file():
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    raise WorldPackError(f"世界观知识路径越界：{path}")
                signed_files.add(resolved)
    for path in sorted(signed_files):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return WorldPack(root, pack_id, revision, name, start_time, locations, characters, species, maps, features, knowledge_paths, tuple(excluded_filenames), assets_root, layers, player, digest.hexdigest())


def resolve_pack_settings(settings: Settings) -> tuple[Settings, WorldPack | None]:
    if settings.world_pack_directory is None:
        return settings, None
    pack = load_world_pack(settings.world_pack_directory)
    database_path = settings.database_path if pack.id == "iserra-noryia" else settings.database_path.parent / "world-saves" / pack.id / settings.database_path.name
    return replace(settings, database_path=database_path, knowledge_paths=pack.knowledge_paths, knowledge_excluded_filenames=pack.knowledge_excluded_filenames), pack


def check_pack_save(database: Database, pack: WorldPack) -> None:
    """先只读核对包与存档，避免冲突时迁移过程改动旧档。"""
    if not database.path.is_file():
        return
    connection = sqlite3.connect(database.path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        metadata_table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='world_pack_metadata'").fetchone()
        if metadata_table is None:
            old_worlds = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worlds'").fetchone()
            if old_worlds and connection.execute("SELECT 1 FROM worlds LIMIT 1").fetchone() and pack.id != "iserra-noryia":
                raise WorldPackError("已有旧存档未登记世界观 ID，不能绑定到其他世界观")
            return
        row = connection.execute("SELECT * FROM world_pack_metadata WHERE id=1").fetchone()
        if row is not None and (row["pack_id"], row["revision"], row["fingerprint"]) != (pack.id, pack.revision, pack.fingerprint):
            raise WorldPackError(f"世界观 {pack.id} 与已有存档冲突或不兼容；请恢复原文件，或使用新的 id 创建独立世界")
    finally:
        connection.close()


def ensure_pack_save(database: Database, pack: WorldPack) -> None:
    with database.write() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS world_pack_metadata (id INTEGER PRIMARY KEY CHECK(id=1), pack_id TEXT NOT NULL, revision INTEGER NOT NULL, fingerprint TEXT NOT NULL)")
        row = connection.execute("SELECT * FROM world_pack_metadata WHERE id=1").fetchone()
        if row is None:
            connection.execute("INSERT INTO world_pack_metadata VALUES (1,?,?,?)", (pack.id, pack.revision, pack.fingerprint))
        elif (row["pack_id"], row["revision"], row["fingerprint"]) != (pack.id, pack.revision, pack.fingerprint):
            raise WorldPackError(f"世界观 {pack.id} 与已有存档冲突或不兼容；请恢复原文件，或使用新的 id 创建独立世界")


def ensure_pack_world(database: Database, settings: Settings, pack: WorldPack) -> None:
    """新包第一次启动时给哥哥建一个世界；旧档则原样继续。"""
    with database.write() as connection:
        if connection.execute("SELECT 1 FROM worlds LIMIT 1").fetchone() is not None:
            return
        repository = WorldRepository()
        world_id = repository.create_world(
            connection, name=pack.name, minutes_per_tick=settings.minutes_per_tick,
            time_scale=settings.default_time_scale,
            adjudication_interval_minutes=settings.adjudication_interval_minutes,
            seed_demo=False, seed_maps=False, start_time=pack.start_time,
        )
        seed_world_from_pack(connection, world_id, pack)
        from world_engine.food_supply import seed_food_supply

        seed_food_supply(connection, world_id)


def seed_world_from_pack(connection: sqlite3.Connection, world_id: str, pack: WorldPack) -> None:
    now = to_iso(utc_now())
    for item in pack.species:
        connection.execute("INSERT OR IGNORE INTO species_profiles(id,display_name,gestation_world_days,adult_age_world_years,compatible_species_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (item["id"], item["display_name"], item["gestation_world_days"], item["adult_age_world_years"], json.dumps(item.get("compatible_species", [item["id"]]), ensure_ascii=False), now, now))
    location_ids = {item["key"]: str(uuid5(NAMESPACE_URL, f"{world_id}:location:{item['key']}")) for item in pack.locations}
    for item in pack.locations:
        connection.execute("INSERT INTO locations(id,world_id,name,kind,resources_json,longitude,latitude,area_radius_km,area_priority) VALUES (?,?,?,?,?,?,?,?,?)", (location_ids[item["key"]], world_id, item["name"], item.get("kind", "place"), json.dumps(item.get("resources", {}), ensure_ascii=False), item["longitude"], item["latitude"], item.get("area_radius_km", 1), item.get("area_priority", 0)))
    for item in pack.locations:
        if item.get("parent") is not None:
            connection.execute("UPDATE locations SET parent_location_id=? WHERE id=?", (location_ids[item["parent"]], location_ids[item["key"]]))
    for item in pack.characters:
        location = next(location for location in pack.locations if location["key"] == item["location"])
        connection.execute("INSERT INTO characters(id,world_id,name,species,location_id,energy,satiety,money,traits_json,goals_json,identity,is_core,longitude,latitude,current_location_id,activation_policy,activation_state,activation_reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (str(uuid5(NAMESPACE_URL, f"{world_id}:character:{item['key']}")), world_id, item["name"], item.get("species", "human"), location_ids[item["location"]], item.get("energy", 80), item.get("satiety", 80), item.get("money", 0), json.dumps(item.get("traits", []), ensure_ascii=False), json.dumps(item.get("goals", []), ensure_ascii=False), item.get("identity"), int(bool(item.get("is_core", True))), location["longitude"], location["latitude"], location_ids[item["location"]], "persistent" if item.get("is_core", True) else "distance", "active" if item.get("is_core", True) else "background", "core" if item.get("is_core", True) else "background", now, now))
    for item in pack.maps:
        connection.execute("INSERT INTO world_maps(id,world_id,name,kind,asset_path,min_longitude,max_longitude,min_latitude,max_latitude,width_pixels,height_pixels,zoom_level,location_id,map_role) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"{world_id}:map:{item['key']}", world_id, item["name"], item.get("kind", "terrain"), item["asset_path"], item.get("min_longitude", -180), item.get("max_longitude", 180), item.get("min_latitude", -90), item.get("max_latitude", 90), item["width_pixels"], item["height_pixels"], item.get("zoom_level", 0), location_ids.get(item.get("location")), item.get("map_role", "world")))
    for item in pack.map_features:
        connection.execute("INSERT INTO map_features(id,world_id,name,feature_type,longitude,latitude,location_id,is_known,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"{world_id}:feature:{item['key']}", world_id, item["name"], item.get("feature_type", "landmark"), item["longitude"], item["latitude"], location_ids.get(item.get("location")), int(bool(item.get("is_known", True))), json.dumps(item.get("metadata", {}), ensure_ascii=False), now, now))
    from world_engine.elements import WorldElementCatalog

    WorldElementCatalog().synchronize_world(connection, world_id=world_id)
