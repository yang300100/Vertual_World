"""哥哥，重现两份产物被安排四次消耗性检测的真实失败形态。"""

from copy import deepcopy
from datetime import timedelta

import pytest

from world_engine.science_programs import ScienceService

pytest_plugins = ["science_fixtures"]


def program(assets, quantity):
    from test_science_programs import craft_program

    spec = craft_program(assets)
    spec["commands"][0]["quantity"] = quantity
    first = spec["commands"][1]
    spec["commands"] = [spec["commands"][0]] + [
        dict(first, result_key="reading" + str(i)) for i in range(4)
    ]
    return spec


def test_shortage_preview_locates_later_sample_without_executing_prefix(science_lab):
    db, _, sid, actors, assets, at = science_lab
    spec = program(assets["actors"][actors[0]], 2)
    unchanged = deepcopy(spec)
    with db.write() as c:
        before = list(c.iterdump())
        result = ScienceService.preview(c, sid, actors[0], spec, at)
        assert list(c.iterdump()) == before
    assert result["status"] == "rejected"
    assert result["errors"][0]["location"] == ["commands", 3, "input"]
    assert "检测完成会消耗取样" in result["errors"][0]["reason"]
    assert spec == unchanged


def test_execute_rejects_known_shortage_before_fuel_or_samples_are_committed(science_lab):
    db, _, sid, actors, assets, at = science_lab
    with db.write() as c:
        before = list(c.iterdump())
        with pytest.raises(ValueError, match="样品分配不足"):
            ScienceService.submit(c, sid, actors[0], program(assets["actors"][actors[0]], 2), at)
        assert list(c.iterdump()) == before


@pytest.mark.parametrize("indices", [("00",) * 4, ("0", "00", "000", "0"), ("٠٠",) * 4])
def test_equivalent_result_indices_share_the_same_sample_budget(science_lab, indices):
    db, _, sid, actors, assets, at = science_lab
    spec = program(assets["actors"][actors[0]], 2)
    for command, index in zip(spec["commands"][1:], indices, strict=True):
        command["input"] = "$heated:" + index
    with db.write() as c:
        before = list(c.iterdump())
        result = ScienceService.preview(c, sid, actors[0], spec, at)
        assert result["status"] == "rejected"
        assert result["errors"][0]["location"] == ["commands", 3, "input"]
        assert list(c.iterdump()) == before


def test_sufficient_samples_still_require_and_complete_actual_work(science_lab):
    from science_fixtures import advance

    from world_engine.science_knowledge import science_context

    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        spec = program(assets["actors"][actors[0]], 4)
        assert ScienceService.preview(c, sid, actors[0], spec, at)["status"] == "ready_to_start"
        pid = ScienceService.submit(c, sid, actors[0], spec, at)
        assert c.execute("SELECT COUNT(*) FROM collective_assay_results").fetchone()[0] == 0
        for hours in (1, 1.5, 2, 2.5, 3):
            advance(c, wid, at + timedelta(hours=hours))
        row = c.execute("SELECT status,error FROM science_programs WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "completed", row["error"]
        assert c.execute("SELECT COUNT(*) FROM collective_assay_results").fetchone()[0] == 4
        assert not c.execute("SELECT 1 FROM technologies").fetchone()
        policy = science_context(c, actors[0], at + timedelta(hours=3), max_chars=40000)[
            "measurement_policy"
        ]
        assert policy["science_assay_sample_quantity"] == 1
        assert policy["sample_consumed_on_completion"] is True
        assert policy["material_output_returned"] is False


def test_native_resource_change_is_not_treated_as_known_future_stock(science_lab):
    from world_engine.science_planning import check_sample_allocation
    from world_engine.science_types import ScienceProgram

    db, _, _, actors, assets, _ = science_lab
    spec = program(assets["actors"][actors[0]], 2)
    # 哥哥，原动作的取样与返还另有规则，不能沿用直接工艺的两份产物估计。
    spec["commands"].insert(1, {"op": "observe", "batch_id": "$heated:0"})
    with db.read() as c:
        check_sample_allocation(c, actors[0], ScienceProgram.model_validate(spec))


def test_foreign_stock_is_not_disclosed_by_quantity_checker(science_lab):
    from world_engine.science_planning import check_sample_allocation
    from world_engine.science_types import ScienceProgram

    db, _, _, actors, assets, _ = science_lab
    spec = program(assets["actors"][actors[0]], 100)
    spec["commands"][0]["input"] = assets["actors"][actors[1]]["materials"]["clay"]
    with db.read() as c:
        check_sample_allocation(c, actors[0], ScienceProgram.model_validate(spec))
