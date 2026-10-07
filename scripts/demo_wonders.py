"""哥哥的独立奇观验收：初始事件之后，只用正常时钟与玩家接口。"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.clock import WorldClockService
from world_engine.config import Settings
from world_engine.database import Database
from world_engine.photos import PhotoService
from world_engine.registration import WorldElementRegistry
from world_engine.registration.models import CharacterArrivalSpec, ElementRegistrationSubmit
from world_engine.repository import WorldRepository, from_iso, utc_now
from world_engine.wonders import WonderService, WonderSpec


class ForbiddenImageClient:
    def __init__(self):
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        raise AssertionError("本次验收禁止生图")


def settings_for(root):
    return Settings(
        database_path=root / "world.db",
        decision_provider="rules",
        world_pack_directory=None,
        deepseek_api_key=None,
        image_api_key=None,
        media_directory=root / "media",
    )


def run(root):
    if (root / "world.db").exists():
        raise ValueError("演示目录已有存档，请指定新目录，哥哥的旧存档不能覆盖。")
    root.mkdir(parents=True, exist_ok=True)
    settings = settings_for(root)
    database = Database(settings.database_path)
    database.initialize()
    repo = WorldRepository()
    with database.write() as c:
        wid = repo.create_world(
            c,
            name="诺瑞亚奇观能力验收",
            minutes_per_tick=60,
            time_scale=60,
            seed_maps=False,
            adjudication_interval_minutes=720,
        )
        loc = dict(
            c.execute("SELECT * FROM locations WHERE world_id=? ORDER BY name", (wid,)).fetchone()
        )
        c.execute("UPDATE locations SET name='联合空港',kind='city' WHERE id=?", (loc["id"],))
        people = c.execute(
            "SELECT * FROM characters WHERE world_id=? ORDER BY name", (wid,)
        ).fetchall()
        first, second, journalist = [p["id"] for p in people[:3]]
        for cid, name, identity, goals in (
            (first, "维尔哈米驻港代表", "联合工程代表", ["希望入住云间联合城"]),
            (second, "德雷霍斯特驻港代表", "联合工程代表", ["观察联合工程进展"]),
        ):
            c.execute(
                "UPDATE characters SET name=?,identity=?,"
                "goals_json=?,longitude=?,latitude=?,loca"
                "tion_id=?,current_location_id=? WHERE id"
                "=?",
                (
                    name,
                    identity,
                    json.dumps(goals, ensure_ascii=False),
                    loc["longitude"],
                    loc["latitude"],
                    loc["id"],
                    loc["id"],
                    cid,
                ),
            )
        remote = "wonder-news-town"
        c.execute(
            "INSERT INTO locations(id,world_id,name,k"
            "ind,longitude,latitude,area_radius_km) V"
            "ALUES (?,?,'地面驿城','town',?,?,1)",
            (remote, wid, loc["longitude"] + 0.25, loc["latitude"]),
        )
        c.execute(
            "UPDATE characters SET name='地面驿城记者',iden"
            "tity='消息灵通的记者',goals_json='[]',location_"
            "id=?,current_location_id=?,longitude=?,l"
            "atitude=? WHERE id=?",
            (remote, remote, loc["longitude"] + 0.25, loc["latitude"], journalist),
        )
        isolated = "wonder-isolated-town"
        c.execute(
            "INSERT INTO locations(id,world_id,name,k"
            "ind,longitude,latitude,area_radius_km) V"
            "ALUES (?,?,'远方山居','village',?,?,1)",
            (isolated, wid, loc["longitude"] + 15, loc["latitude"] + 5),
        )
        player = repo.create_player_character(
            c,
            world_id=wid,
            name="奇观旅人",
            identity="旅行者",
            location_id=loc["id"],
            traits=["好奇"],
            goal="参观云间联合城",
        )
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[0]
        )
        initial = WonderService.offer(
            c,
            wid,
            WonderSpec(
                name="云间联合城",
                ground_location_id=loc["id"],
                longitude=loc["longitude"],
                latitude=loc["latitude"],
                construction_hours=6,
            ),
            [
                {"character_id": first, "polity": "维尔哈米", "authorized": True},
                {"character_id": second, "polity": "德雷霍斯特", "authorized": True},
            ],
            at,
        )
        hermit = (
            WorldElementRegistry()
            .submit(
                c,
                world_id=wid,
                request=ElementRegistrationSubmit(
                    source_event_id=initial,
                    idempotency_key="isolated-resident",
                    payload=CharacterArrivalSpec(
                        name="远方隐居者", identity="隐居者", location_id=isolated
                    ),
                ),
            )
            .result_entity_id
        )
    image = ForbiddenImageClient()
    api_app = create_app(
        settings, photo_service_override=PhotoService(settings, image_client=image)
    )
    logging.getLogger("virtual-world.worker").setLevel(logging.ERROR)
    timeline = []
    photos = {}
    checks = {}
    clock = WorldClockService(database, settings)

    def heartbeat(hours=0.5):
        result = clock.heartbeat(wid, elapsed_seconds=hours * 60, real_now=utc_now())
        with database.read() as c:
            wonders = WonderService.view(c, wid)
            timeline.append(
                {
                    "world_time": c.execute(
                        "SELECT w.current_time FROM worlds w WHERE id=?", (wid,)
                    ).fetchone()[0],
                    "wonders": wonders,
                    "heartbeat_id": result.heartbeat_id,
                }
            )

    def post(client, path, body):
        response = client.post(f"/api/worlds/{wid}/" + path, json=body)
        if not 200 <= response.status_code < 300:
            raise AssertionError(f"{path}: {response.status_code} {response.text}")
        return response.json()

    def move(client, lon, lat):
        scene = client.get(f"/api/worlds/{wid}/game-scene").json()
        if (
            abs(scene["player"]["longitude"] - lon) + abs(scene["player"]["latitude"] - lat)
            < 0.00001
        ):
            return
        post(client, "player/move", {"destination_longitude": lon, "destination_latitude": lat})
        for _ in range(8):
            heartbeat(0.25)
            with database.read() as c:
                moving = c.execute(
                    "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                    (player.id,),
                ).fetchone()
            if not moving:
                return
        raise AssertionError("玩家行程未到达")

    # 外部 HTTP 运输层被硬性禁止；TestClient 在进程内走 ASGI，不受影响。
    with patch(
        "httpx.HTTPTransport.handle_request", side_effect=AssertionError("验收禁止外部模型请求")
    ):
        with TestClient(api_app) as client:
            heartbeat()
            with database.read() as c:
                planned = WonderService.view(c, wid)[0]
            rejected = client.post(
                f"/api/worlds/{wid}/wonders/{planned['id']}/traverse", json={"direction": "enter"}
            )
            checks["unfinished_access_rejected"] = rejected.status_code == 409
            before = post(
                client,
                "photos/preview",
                {"photographer_character_id": player.id, "direction": "north"},
            )
            checks["unfinished_not_in_photo"] = "云间联合城" not in before["prompt"]
            for _ in range(24):
                heartbeat()
                with database.read() as c:
                    city = WonderService.view(c, wid)[0]
                if city["status"] == "active":
                    break
            else:
                raise AssertionError("奇观没有在正常心跳中竣工")
            checks["joint_sponsors"] = set(city["sponsors"]) == {"维尔哈米", "德雷霍斯特"}
            scene = client.get(f"/api/worlds/{wid}/game-scene").json()
            checks["map_has_wonder"] = city["id"] in {w["id"] for w in scene["wonders"]}
            with database.read() as c:
                resident = WonderService.context(c, wid, first)
                checks["autonomous_resident"] = resident["environment"] is not None
                checks["distant_reporter_initially_unknown"] = (
                    WonderService.context(c, wid, journalist)["known_wonders"] == []
                )
            enter = post(client, f"wonders/{city['id']}/traverse", {"direction": "enter"})
            checks["player_enters_city"] = (
                enter["scene"]["player"]["space_layer"] == city["space_layer"]
            )
            checks["npc_layer_isolation"] = first in {
                p["id"] for p in enter["scene"]["npcs"]
            } and second not in {p["id"] for p in enter["scene"]["npcs"]}
            for direction in ("north", "east", "south", "west"):
                photos["inside_" + direction] = post(
                    client,
                    "photos/preview",
                    {"photographer_character_id": player.id, "direction": direction},
                )
            checks["inside_four_directions"] = all(
                "浮于空中" in v["prompt"] and "云间联合城" in v["prompt"] for k, v in photos.items()
            )
            post(client, "player/steer", {"dx": 1, "dy": 0, "sequence": 100})
            heartbeat(1 / 60)
            post(client, "player/steer/stop", {"sequence": 101})
            scene = client.get(f"/api/worlds/{wid}/game-scene").json()
            checks["wasd_stays_in_city"] = (
                scene["player"]["current_location_id"] == city["location_id"]
            )
            post(client, f"wonders/{city['id']}/traverse", {"direction": "exit"})
            for vantage, dx, dy, towards, away in (
                ("south", 0, -0.025, "north", "south"),
                ("east", 0.025, 0, "west", "east"),
                ("north", 0, 0.025, "south", "north"),
                ("west", -0.025, 0, "east", "west"),
            ):
                move(client, loc["longitude"] + dx, loc["latitude"] + dy)
                for direction in (towards, away):
                    photos[f"ground_{vantage}_{direction}"] = post(
                        client,
                        "photos/preview",
                        {"photographer_character_id": player.id, "direction": direction},
                    )
                checks["ground_" + vantage + "_directions"] = (
                    "云间联合城" in photos[f"ground_{vantage}_{towards}"]["prompt"]
                    and "云间联合城" not in photos[f"ground_{vantage}_{away}"]["prompt"]
                )
            move(client, loc["longitude"], loc["latitude"])
            for _ in range(30):
                heartbeat(0.5)
                with database.read() as c:
                    informed = WonderService.context(c, wid, journalist)
                if informed["known_wonders"]:
                    break
            with database.read() as c:
                reporter = WonderService.context(c, wid, journalist)
                unknown = WonderService.context(c, wid, hermit)
            checks["ground_reporter_has_sourced_news"] = bool(
                reporter["known_wonders"]
                and reporter["known_wonders"][0]["evidence"]["source_kind"] in {"heard", "rumor"}
            )
            checks["remote_npc_remains_unknown"] = unknown["known_wonders"] == []
            # 留下可玩的城内视角，哥哥打开独立演示时能直接看见居民与平台。
            post(client, f"wonders/{city['id']}/traverse", {"direction": "enter"})
    database.initialize()
    with database.read() as c:
        final = WonderService.view(c, wid)[0]
        checks["restart_persists_city"] = (
            final["location_id"] == city["location_id"] and final["status"] == "active"
        )
        events = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM world_events WHERE world_id=? ORDER BY occurred_at,created_at,id",
                (wid,),
            )
        ]
        checks["one_completion"] = (
            sum(e["event_type"] == "world.wonder_completed" for e in events) == 1
        )
        resident = WonderService.context(c, wid, first)
        checks["restart_persists_resident_awareness"] = resident["environment"] is not None
        from world_engine.game_scene import GameSceneService

        scene = GameSceneService.snapshot(c, wid)
    checks["zero_image_generation"] = image.calls == 0
    result = {
        "world_id": wid,
        "wonder": final,
        "checks": checks,
        "all_passed": all(checks.values()),
        "npc_policy": "rules; initial construction mandates and"
        " residence goals supplied; no LLM invent"
        "ion claim",
        "construction_hours": 6,
        "image_generation_requests": image.calls,
        "external_model_requests": 0,
        "resident": resident,
        "reporter": reporter,
        "uninformed": unknown,
        "scene": scene,
    }
    for name, data in (
        ("verification.json", result),
        ("timeline.json", timeline),
        ("events.json", events),
        ("photo-contexts.json", photos),
    ):
        (root / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 云间联合城自主建造验收",
        "",
        f"世界：{wid}",
        "",
        "哥哥，这次使用独立能力测试世界，维尔哈米与德雷霍斯特作为预设授权的政体参与方。建"
        "设动机、科技和材料不在验收范围。NPC 采用离线规则决策，不声称模型自行发明方案"
        "。测试工期预设6世界小时。",
        "",
        "初始投放建设机会之后，代表自行读取本人目击信息，按授权决定加入，通过既有登记服务开工。正常心跳推进施工；竣工创建可进入城市与升降通道，有入住目标的代表自行迁入。",
        "",
        "玩家通过真实 API 登城、行走、离城并从地面四侧取景。城内四个方向均使用悬空平台环境，"
        "地面朝向城市可见，背向不可见。拍摄只准备上下文，未生成图片。",
        "",
        "地面驿城记者在消息传播所需时间经过后获得有来源的消息；远方隐居者在验收终点仍未获消息。住在城中的代表保留亲历知识与当前悬空环境。",
        "",
        "| 验收项 | 结果 |",
        "| --- | --- |",
    ]
    lines += [f"| {name} | {'通过' if value else '未通过'} |" for name, value in checks.items()]
    lines += [
        "",
        "完整事件见 events.json，心跳与工程进度见 timeline.json，"
        "所有取景说明见 photo-contexts.json。",
        "",
        "运行独立演示服务：`python -m scripts.demo_wonders"
        " --serve --output <本目录>`。仅绑定127.0.0.1，不加"
        "载 .env 中的模型密钥。",
    ]
    (root / "验收过程.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(root),
                "world_id": wid,
                "all_passed": result["all_passed"],
                "checks": checks,
            },
            ensure_ascii=True,
        )
    )
    if not result["all_passed"]:
        raise AssertionError("独立演示未通过完整验收")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path("run/wonders") / utc_now().strftime("%Y%m%dT%H%M%S%f")
    )
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    root = args.output.resolve()
    if args.serve:
        import uvicorn

        uvicorn.run(
            create_app(settings_for(root)), host="127.0.0.1", port=args.port, access_log=False
        )
    else:
        run(root)


if __name__ == "__main__":
    main()
