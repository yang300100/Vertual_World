"""哥哥，复用必须重新劳动，依赖只能来自真实调用或真实产物。"""

import importlib
import importlib.util
import json
from datetime import timedelta

import pytest

pytest_plugins = ["science_fixtures"]


def methods():
    assert importlib.util.find_spec("world_engine.technology_methods"), "尚无实际方法复用"
    return importlib.import_module("world_engine.technology_methods")


@pytest.fixture
def certified(science_lab):
    from test_science_certification import declaration, repeat, technologies

    db, wid, sid, actors, assets, at = science_lab
    a = assets["actors"][actors[0]]
    with db.write() as c:
        body = declaration(a)
        body["method"]["steps"][0]["quantity"] = 2
        cid = technologies().declare(c, sid, actors[0], body, at)
        repeat(c, wid, sid, actors[0], cid, a, at)
        repeat(c, wid, sid, actors[0], cid, a, at, start=2)
        result = technologies().certify(c, cid, actors[0], at + timedelta(hours=4))
        assert result["status"] == "verified", result
    return db, wid, sid, actors, assets, at + timedelta(hours=4), result["technology_id"]


def test_unknown_method_is_not_executable(science_lab):
    m = methods()
    db, _, _, actors, _, at = science_lab
    with db.read() as c, pytest.raises(ValueError, match="方法|科技"):
        m.instantiate_method(c, actors[0], "未知科技", 1, {}, {}, at)


@pytest.mark.parametrize("case", ["unknown_actor", "wrong_version", "absent", "no_fuel", "remote"])
def test_known_methods_still_need_actual_typed_resources(certified, case):
    m = methods()
    from test_science_certification import bindings

    db, _, _, actors, assets, at, tid = certified
    with db.write() as c:
        a = assets["actors"][actors[0]]
        bound = bindings(a)
        actor, version = actors[0], 1
        if case == "unknown_actor":
            actor = actors[1]
        elif case == "wrong_version":
            version = 99
        elif case == "absent":
            bound["sample"] = "不存在的样品"
        elif case == "no_fuel":
            bound["fuel"] = bound["sample"]
        else:
            c.execute(
                "UPDATE item_instances SET container_type='collective_transit' WHERE id=?",
                (bound["sample"],),
            )
        with pytest.raises(ValueError):
            m.instantiate_method(c, actor, tid, version, bound, {}, at)


def test_reuse_has_real_costs_and_does_not_create_edges_from_mention(certified):
    m = methods()
    from test_science_certification import bindings, settle, technologies

    from world_engine.materials import MaterialService
    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at, tid = certified
    with db.write() as c:
        a = assets["actors"][actors[0]]
        before = c.execute(
            "SELECT quantity FROM item_instances WHERE id=?", (a["energy"],)
        ).fetchone()[0]
        eid = m.execute_method(c, sid, actors[0], tid, 1, bindings(a), {}, at)
        ScienceService.tick(c, wid, at)
        assert (
            c.execute("SELECT status FROM technology_executions WHERE id=?", (eid,)).fetchone()[0]
            != "completed"
        )
        settle(c, wid, at + timedelta(hours=1))
        settle(c, wid, at + timedelta(hours=1.5))
        row = c.execute("SELECT * FROM technology_executions WHERE id=?", (eid,)).fetchone()
        assert row["status"] == "completed", row["error"]
        outputs = json.loads(row["output_ids_json"])
        assert any(MaterialService.state(c, iid) for iid in outputs)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (a["energy"],)).fetchone()[
                0
            ]
            < before
        )
        assert technologies().graph(c, wid)["edges"] == []


def test_actual_nested_method_creates_dependency_after_real_validation(certified):
    methods()
    from test_science_certification import declaration, technologies

    db, wid, sid, actors, assets, at, parent = certified
    with db.write() as c:
        a = assets["actors"][actors[0]]
        spec = declaration(a, name="离线子方法")
        spec["method"]["steps"] = [
            {
                "op": "use_method",
                "technology_id": parent,
                "version": 1,
                "bindings": {key: key for key in spec["method"]["inputs"]},
                "result_key": "parent",
            },
            {
                "op": "compact",
                "input": "$parent:0",
                "hours": 1,
                "settings": {"pressure": 100},
                "apparatus": "kiln",
                "energy": "fuel",
                "result_key": "dense",
            },
            {"op": "assay", "input": "$dense:0", "metric": "strength", "instrument": "gauge"},
        ]
        cid = technologies().declare(c, sid, actors[0], spec, at)
        for start in (0, 3.5):
            from test_science_certification import settle, trial

            from world_engine.science_programs import ScienceService

            trial(c, sid, actors[0], cid, a, at + timedelta(hours=start))
            ScienceService.tick(c, wid, at + timedelta(hours=start))
            for offset in (1, 1.5, 2.5, 3, 3.5):
                settle(c, wid, at + timedelta(hours=start + offset))
        result = technologies().certify(c, cid, actors[0], at + timedelta(hours=7))
        assert result["status"] == "verified", result
        graph = technologies().graph(c, wid)
        assert any(
            e["parent_id"] == parent and e["child_id"] == result["technology_id"]
            for e in graph["edges"]
        )


def test_cycle_detection_and_limits_are_real():
    m = methods()
    assert m.would_cycle([("a", "b"), ("b", "c")], "c", "a")
    assert not m.would_cycle([("a", "b"), ("b", "c")], "a", "d")


def test_invalidation_requires_actual_counterevidence(certified):
    methods()
    from test_science_certification import technologies

    db, _, _, actors, _, at, tid = certified
    with db.write() as c, pytest.raises(ValueError, match="反证|证据"):
        technologies().invalidate(c, tid, actors[0], ("不存在的反证",), "文字否定", at)


@pytest.mark.parametrize("depth,rejected", [(4, False), (5, True)])
def test_compiler_counts_the_root_in_four_level_limit(monkeypatch, depth, rejected):
    m = methods()
    from world_engine.science_types import MethodSpec

    leaf = {"inputs": {"sample": "material"}, "steps": [{"op": "store", "input": "sample"}]}

    def record(_, actor, tid, version, at):
        number = int(tid)
        body = (
            leaf
            if number == depth - 1
            else {
                "inputs": leaf["inputs"],
                "steps": [
                    {
                        "op": "use_method",
                        "technology_id": str(number + 1),
                        "version": 1,
                        "bindings": {"sample": "sample"},
                    }
                ],
            }
        )
        return {"method_json": json.dumps(body), "conditions_json": "{}"}

    monkeypatch.setattr(m, "verified_version", record)
    root = MethodSpec.model_validate(json.loads(record(None, None, "0", 1, None)["method_json"]))
    if rejected:
        with pytest.raises(ValueError, match="深度|层"):
            m.compile_method(None, None, root, {"sample": "test"}, {}, None)
    else:
        assert m.compile_method(None, None, root, {"sample": "test"}, {}, None).method.steps


@pytest.mark.parametrize("calls,rejected", [(4, False), (5, True)])
def test_compiler_bounds_the_actual_expanded_step_count(monkeypatch, calls, rejected):
    m = methods()
    from world_engine.science_types import MethodSpec

    leaf = {
        "inputs": {"sample": "material"},
        "steps": [{"op": "store", "input": "sample"} for _ in range(24)],
    }
    monkeypatch.setattr(
        m,
        "verified_version",
        lambda *args: {"method_json": json.dumps(leaf), "conditions_json": "{}"},
    )
    root = MethodSpec.model_validate(
        {
            "inputs": leaf["inputs"],
            "steps": [
                {
                    "op": "use_method",
                    "technology_id": "leaf",
                    "version": 1,
                    "bindings": {"sample": "sample"},
                }
                for _ in range(calls)
            ],
        }
    )
    if rejected:
        with pytest.raises(ValueError, match="96"):
            m.compile_method(None, None, root, {"sample": "test"}, {}, None)
    else:
        assert (
            len(m.compile_method(None, None, root, {"sample": "test"}, {}, None).method.steps) == 96
        )


def test_material_reuse_creates_dependency_from_actual_consumption(certified):
    m = methods()
    from test_science_certification import bindings, declaration, settle, technologies

    from world_engine.materials import MaterialService
    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at, parent = certified
    with db.write() as c:
        a = assets["actors"][actors[0]]
        spec = declaration(a, name="实物来源的离线子方法")
        spec["method"]["steps"][0] = {
            "op": "compact",
            "input": "sample",
            "hours": 1,
            "settings": {"pressure": 100},
            "apparatus": "kiln",
            "energy": "fuel",
            "result_key": "heated",
        }
        cid = technologies().declare(c, sid, actors[0], spec, at)
        for start in (0, 3.5):
            eid = m.execute_method(
                c, sid, actors[0], parent, 1, bindings(a), {}, at + timedelta(hours=start)
            )
            ScienceService.tick(c, wid, at + timedelta(hours=start))
            for offset in (1, 1.5):
                settle(c, wid, at + timedelta(hours=start + offset))
            outputs = json.loads(
                c.execute(
                    "SELECT output_ids_json FROM technology_executions WHERE id=?", (eid,)
                ).fetchone()[0]
            )
            sample = next(iid for iid in outputs if MaterialService.state(c, iid))
            bound = bindings(a) | {"sample": sample}
            technologies().validate(
                c,
                sid,
                actors[0],
                {
                    "candidate_id": cid,
                    "revision": 1,
                    "bindings": bound,
                    "control_bindings": bindings(a, control=True),
                },
                at + timedelta(hours=start + 1.5),
            )
            ScienceService.tick(c, wid, at + timedelta(hours=start + 1.5))
            for offset in (2.5, 3, 3.5):
                settle(c, wid, at + timedelta(hours=start + offset))
        result = technologies().certify(c, cid, actors[0], at + timedelta(hours=7))
        assert result["status"] == "verified", result
        assert any(
            e["parent_id"] == parent and e["child_id"] == result["technology_id"]
            for e in technologies().graph(c, wid)["edges"]
        )


def test_actual_counterevidence_invalidates_reliable_use_and_keeps_history(certified):
    m = methods()
    from test_science_certification import bindings, settle

    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at, tid = certified
    with db.write() as c:
        a = assets["actors"][actors[0]]
        pid = ScienceService.submit(
            c,
            sid,
            actors[0],
            {
                "reason": "测试受损材料是否仍能采用已知方法",
                "commands": [
                    {
                        "op": "craft",
                        "operation": "heat",
                        "input": a["materials"]["clay"],
                        "quantity": 2,
                        "settings": {"temperature": 2000, "power": 5},
                        "hours": 5,
                        "apparatus": a["apparatus"],
                        "energy": a["energy"],
                        "result_key": "damaged",
                    },
                    {
                        "op": "craft",
                        "operation": "cool",
                        "input": "$damaged:0",
                        "quantity": 2,
                        "settings": {"temperature": 20, "ventilation": 5},
                        "hours": 2,
                        "apparatus": a["apparatus"],
                        "result_key": "cooled",
                    },
                ],
            },
            at,
        )
        settle(c, wid, at + timedelta(hours=5))
        settle(c, wid, at + timedelta(hours=7))
        p = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
        assert p["status"] == "completed", p["error"]
        sample = json.loads(p["bindings_json"])["cooled"][0]
        eid = m.execute_method(
            c, sid, actors[0], tid, 1, bindings(a) | {"sample": sample}, {}, at + timedelta(hours=7)
        )
        ScienceService.tick(c, wid, at + timedelta(hours=7))
        for offset in (8, 8.5):
            settle(c, wid, at + timedelta(hours=offset))
        execution = c.execute("SELECT * FROM technology_executions WHERE id=?", (eid,)).fetchone()
        assert execution["status"] == "completed", execution["error"]
        evidence = tuple(json.loads(execution["observation_ids_json"]))
        invalidation = ScienceService.submit(
            c,
            sid,
            actors[0],
            {
                "reason": "本人取得反证后撤销原可靠结论",
                "commands": [
                    {
                        "op": "invalidate_method",
                        "technology_id": tid,
                        "version": 1,
                        "evidence_ids": list(evidence),
                        "reason": "既有方法在同载体受损材料上出现真实失败",
                    }
                ],
            },
            at + timedelta(hours=8.5),
        )
        assert (
            c.execute("SELECT status FROM science_programs WHERE id=?", (invalidation,)).fetchone()[
                0
            ]
            == "completed"
        )
        assert (
            c.execute("SELECT status FROM technologies WHERE id=?", (tid,)).fetchone()[0]
            == "invalidated"
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM technology_versions WHERE technology_id=?", (tid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM technology_evidence WHERE technology_id=?", (tid,)
            ).fetchone()[0]
            >= 4
        )
        with pytest.raises(ValueError, match="失效|复核"):
            m.instantiate_method(c, actors[0], tid, 1, bindings(a), {}, at + timedelta(hours=8.5))


def test_two_real_parent_products_keep_sources_through_mix_and_delivery(certified):
    from test_science_certification import bindings, declaration, repeat, settle, technologies

    from world_engine.materials import MaterialService as M
    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at, parent = certified
    m = methods()
    with db.write() as c:
        a = assets["actors"][actors[0]]
        spec = declaration(a, name="离线第二种工艺", temperature=1250, delta=0.1)
        spec["method"]["steps"][0]["quantity"] = 2
        second = technologies().declare(c, sid, actors[0], spec, at)
        for start in (0, 2):
            repeat(c, wid, sid, actors[0], second, a, at, start=start)
        result = technologies().certify(c, second, actors[0], at + timedelta(hours=4))
        assert result["status"] == "verified", result
        parents = (parent, result["technology_id"])
        at += timedelta(hours=4)
        child = declaration(a, name="实物混合来源工艺", delta=0.1)
        child["method"]["steps"][0] = {
            "op": "compact",
            "input": "sample",
            "hours": 1,
            "settings": {"pressure": 100},
            "apparatus": "kiln",
            "energy": "fuel",
            "result_key": "heated",
        }
        cid = technologies().declare(c, sid, actors[0], child, at)
        for start in (0, 5):
            products = []
            for index, tid in enumerate(parents):
                moment = at + timedelta(hours=start + index * 1.5)
                eid = m.execute_method(c, sid, actors[0], tid, 1, bindings(a), {}, moment)
                ScienceService.tick(c, wid, moment)
                for offset in (1, 1.5):
                    settle(c, wid, moment + timedelta(hours=offset))
                outputs = json.loads(
                    c.execute(
                        "SELECT output_ids_json FROM technology_executions WHERE id=?", (eid,)
                    ).fetchone()[0]
                )
                products.append(next(iid for iid in outputs if M.state(c, iid)))

            # 真实交付再交还，不擦掉物质账中的来源。
            def item(iid):
                return c.execute("SELECT * FROM item_instances WHERE id=?", (iid,)).fetchone()

            M.move(c, item(products[0]), 1, "ground", a["site_id"], actors[1])
            M.move(c, item(products[0]), 1, "ground", a["site_id"], actors[0])
            M.merge(c, item(products[1]), item(products[0]), 1)
            technologies().validate(
                c,
                sid,
                actors[0],
                {
                    "candidate_id": cid,
                    "revision": 1,
                    "bindings": bindings(a) | {"sample": products[0]},
                    "control_bindings": bindings(a, control=True),
                },
                at + timedelta(hours=start + 3),
            )
            ScienceService.tick(c, wid, at + timedelta(hours=start + 3))
            for offset in (4, 4.5, 5):
                settle(c, wid, at + timedelta(hours=start + offset))
        result = technologies().certify(c, cid, actors[0], at + timedelta(hours=10))
        assert result["status"] == "verified", result
        edges = technologies().graph(c, wid)["edges"]
        assert {e["parent_id"] for e in edges if e["child_id"] == result["technology_id"]} == set(
            parents
        )
        child_id = result["technology_id"]
        moment = at + timedelta(hours=10)
        pid = ScienceService.submit(
            c,
            sid,
            actors[0],
            {
                "reason": "检验受损实物上的父方法可靠性",
                "commands": [
                    {
                        "op": "craft",
                        "operation": "heat",
                        "input": a["materials"]["clay"],
                        "quantity": 2,
                        "settings": {"temperature": 2000, "power": 5},
                        "hours": 5,
                        "apparatus": a["apparatus"],
                        "energy": a["energy"],
                        "result_key": "damaged",
                    },
                    {
                        "op": "craft",
                        "operation": "cool",
                        "input": "$damaged:0",
                        "quantity": 2,
                        "settings": {"temperature": 20, "ventilation": 5},
                        "hours": 2,
                        "apparatus": a["apparatus"],
                        "result_key": "cooled",
                    },
                ],
            },
            moment,
        )
        settle(c, wid, moment + timedelta(hours=5))
        settle(c, wid, moment + timedelta(hours=7))
        source = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
        assert source["status"] == "completed", source["error"]
        damaged = json.loads(source["bindings_json"])["cooled"][0]
        eid = m.execute_method(
            c,
            sid,
            actors[0],
            parent,
            1,
            bindings(a) | {"sample": damaged},
            {},
            moment + timedelta(hours=7),
        )
        ScienceService.tick(c, wid, moment + timedelta(hours=7))
        for offset in (8, 8.5):
            settle(c, wid, moment + timedelta(hours=offset))
        execution = c.execute("SELECT * FROM technology_executions WHERE id=?", (eid,)).fetchone()
        evidence = tuple(json.loads(execution["observation_ids_json"]))
        technologies().invalidate(
            c, parent, actors[0], evidence, "真实父方法反证", moment + timedelta(hours=8.5)
        )
        states = {n["id"]: n["version_status"] for n in technologies().graph(c, wid)["nodes"]}
        assert states[parent] == "invalidated" and states[child_id] == "needs_review"
        assert states[parents[1]] == "verified"
        assert len(technologies().graph(c, wid)["edges"]) == 2
        with pytest.raises(ValueError, match="失效|复核"):
            m.instantiate_method(
                c, actors[0], child_id, 1, bindings(a), {}, moment + timedelta(hours=8.5)
            )
