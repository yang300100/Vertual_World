"""通过词条组合批量登记背景 NPC，不调用语言模型。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from world_engine.database import Database
from world_engine.npc_generation import (
    generate_npc_positions,
    generate_npc_profiles,
    generate_town_duty_profiles,
)
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用本地词条批量生成背景 NPC")
    parser.add_argument("--count", type=int, default=16, help="本次新增数量，默认 16")
    parser.add_argument("--seed", default="noryia-local-fill-v1", help="固定随机种子")
    parser.add_argument("--world-id", help="目标世界，默认选择最近运行中的世界")
    parser.add_argument("--location-id", help="出生地点，默认使用玩家当前地点")
    parser.add_argument(
        "--town-duties",
        action="store_true",
        help="补齐第一阶段十项城镇基础事务岗位，忽略 --count",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    database = Database(Path("data/world.db"))
    database.initialize()
    registry = WorldElementRegistry()

    with database.write() as connection:
        world = connection.execute(
            """
            SELECT id, name FROM worlds
            WHERE id = COALESCE(?, id) AND status = 'running'
            ORDER BY updated_at DESC LIMIT 1
            """,
            (args.world_id,),
        ).fetchone()
        if world is None:
            raise SystemExit("没有可运行的目标世界")
        player = connection.execute(
            """
            SELECT id, location_id, current_location_id FROM characters
            WHERE world_id = ? AND is_player = 1
            """,
            (world["id"],),
        ).fetchone()
        if player is None:
            raise SystemExit("目标世界没有玩家角色，不能提供可审计的登记来源")
        location_id = args.location_id or player["current_location_id"] or player["location_id"]
        location = connection.execute(
            """
            SELECT id, name, longitude, latitude FROM locations
            WHERE id = ? AND world_id = ? AND is_active = 1
            """,
            (location_id, world["id"]),
        ).fetchone()
        if location is None:
            raise SystemExit("指定地点不属于目标世界或已经退役")
        source_event = connection.execute(
            """
            SELECT id FROM world_events
            WHERE world_id = ? AND (actor_id = ? OR target_id = ?)
            ORDER BY occurred_at DESC, created_at DESC LIMIT 1
            """,
            (world["id"], player["id"], player["id"]),
        ).fetchone()
        if source_event is None:
            raise SystemExit("玩家尚无可作为登记来源的事件")
        existing_names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM characters WHERE world_id = ?", (world["id"],)
            ).fetchall()
        }
        if args.town_duties:
            existing_identities = {
                row["identity"] or ""
                for row in connection.execute(
                    """
                    SELECT identity FROM characters
                    WHERE world_id = ? AND current_location_id = ? AND is_player = 0
                    """,
                    (world["id"], location["id"]),
                ).fetchall()
            }
            profiles = generate_town_duty_profiles(
                seed=args.seed,
                excluded_names=existing_names,
                existing_identities=existing_identities,
            )
        else:
            profiles = generate_npc_profiles(
                seed=args.seed,
                count=args.count,
                excluded_names=existing_names,
            )
        positions = generate_npc_positions(
            seed=args.seed,
            profiles=profiles,
            center_longitude=float(location["longitude"]),
            center_latitude=float(location["latitude"]),
        )
        created: list[dict[str, str]] = []
        for profile in profiles:
            longitude, latitude = positions[profile.name]
            request = ElementRegistrationSubmit.model_validate(
                {
                    "requested_by_character_id": player["id"],
                    "source_event_id": source_event["id"],
                    "idempotency_key": f"npc-batch:{args.seed}:{profile.idempotency_suffix}",
                    "payload": {
                        "element_type": "character_arrival",
                        "name": profile.name,
                        "identity": profile.identity,
                        "location_id": location["id"],
                        "traits": list(profile.traits),
                        "goals": list(profile.goals),
                        "species": "human",
                        "activation_policy": "distance",
                        "longitude": longitude,
                        "latitude": latitude,
                    },
                }
            )
            registration = registry.submit(
                connection, world_id=world["id"], request=request
            )
            created.append(
                {
                    "name": profile.name,
                    "identity": profile.identity,
                    "registration_id": registration.id,
                    "status": registration.status.value,
                }
            )

    print(
        json.dumps(
            {
                "world_id": world["id"],
                "world_name": world["name"],
                "location_id": location["id"],
                "location_name": location["name"],
                "seed": args.seed,
                "mode": "town_duties" if args.town_duties else "general",
                "created": created,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
