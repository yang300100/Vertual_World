"""哥哥，操作索引来自真实可见器具，不提供隐藏响应或替人物选择工艺。"""

from world_engine.materials import encoded
from world_engine.science_knowledge import science_context

pytest_plugins = ["science_fixtures"]


def test_actual_tool_operations_and_measurements_are_visible_without_fake_readiness(science_lab):
    db, _, _, actors, assets, at = science_lab
    aid = actors[0]
    with db.read() as c:
        context = science_context(c, aid, at, max_chars=40000)
    index = context["tool_operation_index"]
    assert index["apparatus"]["heat"] == [assets["actors"][aid]["apparatus"]]
    assert "screen" not in index["apparatus"]
    assert index["instruments"]["strength"] == [assets["actors"][aid]["instruments"]["strength"]]
    text = encoded(index)
    assert assets["actors"][actors[1]]["apparatus"] not in text
    assert "ready_to_start" not in text and "strength_scale" not in text


def test_remote_or_broken_apparatus_is_not_listed_as_current_tool(science_lab):
    db, wid, _, actors, assets, at = science_lab
    apparatus = assets["actors"][actors[0]]["apparatus"]
    with db.write() as c:
        c.execute("UPDATE item_instances SET condition=0 WHERE id=?", (apparatus,))
        assert "heat" not in science_context(c, actors[0], at, max_chars=40000)[
            "tool_operation_index"
        ]["apparatus"]
        from uuid import uuid4

        far = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
            "VALUES (?,?,'远处工坊','city',1,0)",
            (far, wid),
        )
        c.execute(
            "UPDATE item_instances SET condition=100,container_id=? WHERE id=?",
            (far, apparatus),
        )
        assert "heat" not in science_context(c, actors[0], at, max_chars=40000)[
            "tool_operation_index"
        ]["apparatus"]


def test_tool_index_does_not_preselect_a_recipe_or_change_state(science_lab):
    from world_engine.science_freeze import state_digest

    db, _, _, actors, _, at = science_lab
    with db.read() as c:
        before = state_digest(c)
        context = science_context(c, actors[0], at, max_chars=40000)
        assert state_digest(c) == before
    text = encoded(context["tool_operation_index"])
    assert "parameters" not in text and "settings" not in text
    assert "heat" in text and "form" in text and "compact" in text
