"""哥哥，人物档案应先限定参与者，再对结果数量设限。"""

from datetime import timedelta
from uuid import uuid4

from test_appointment_series import offer
from test_lived_world import town  # noqa: F401
from world_engine.repository import to_iso


def test_person_filter_precedes_limits_and_hides_other_attendance(town):
    e = town
    aid, sid = offer(e)
    with e.db.write() as c:
        outsider = c.execute("SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
                             (e.wid, e.player, e.npc)).fetchone()[0]
        c.execute("UPDATE social_appointments SET status='completed' WHERE id=?", (aid,))
        c.execute("UPDATE social_appointment_series SET status='completed' WHERE id=?", (sid,))
        c.execute("INSERT INTO appointment_attendance VALUES (?,?,?)", (aid, e.npc, to_iso(e.at)))
        c.execute("INSERT INTO appointment_attendance VALUES (?,?,?)", (aid, e.player, to_iso(e.at + timedelta(minutes=1))))
        date = dict(c.execute("SELECT * FROM social_appointments WHERE id=?", (aid,)).fetchone())
        cycle = dict(c.execute("SELECT * FROM social_appointment_series WHERE id=?", (sid,)).fetchone())
        # 哥哥，这些隔离数据只用于验证全局数量上限，不能写入正式世界。
        for index in range(25):
            row = {**date, "id": str(uuid4()), "proposer_id": outsider,
                   "starts_at": to_iso(e.at + timedelta(days=index + 2)), "contract_id": None,
                   "moment_id": None}
            columns = list(row)
            c.execute(f"INSERT INTO social_appointments ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                      tuple(row[name] for name in columns))
            row = {**cycle, "id": str(uuid4()), "proposer_id": outsider,
                   "created_at": to_iso(e.at + timedelta(days=index + 2))}
            columns = list(row)
            c.execute(f"INSERT INTO social_appointment_series ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                      tuple(row[name] for name in columns))
    base = f"/api/worlds/{e.wid}/appointments"
    assert aid not in {row["id"] for row in e.client.get(base).json()}
    assert sid not in {row["id"] for row in e.client.get(base + "/series").json()}
    dates = e.client.get(base, params={"character_id": e.npc})
    cycles = e.client.get(base + "/series", params={"character_id": e.npc})
    assert dates.status_code == cycles.status_code == 200
    assert [row["id"] for row in dates.json()] == [aid]
    assert [row["id"] for row in cycles.json()] == [sid]
    assert dates.json()[0]["own_arrived_at"] == to_iso(e.at + timedelta(minutes=1))
    assert "attendance" not in dates.json()[0]
    assert all(row["other_id"] == e.npc for row in dates.json() + cycles.json())
    for path in (base, base + "/series"):
        assert e.client.get(path, params={"character_id": e.player}).status_code == 404
        assert e.client.get(path, params={"character_id": str(uuid4())}).status_code == 404
