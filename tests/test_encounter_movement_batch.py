"""哥哥，同一心跳里的多人同步移动不会制造一次假分离或假重逢。"""

# ruff: noqa: F811
from test_companion_consent import accepted
from test_companion_group_exit import group
from test_lived_world import town  # noqa: F401

from world_engine.encounters import EncounterService
from world_engine.movement import MovementService
from world_engine.repository import from_iso
from world_engine.society import SocietyService


def test_real_companion_routes_preserve_continuous_meeting_through_midway_and_arrival(town):
    e = town
    accepted(e, 30)
    with e.db.write() as c:
        SocietyService.tick(c, e.wid, e.at)
        initial = EncounterService.current(c, e.wid, e.npc, e.player, e.at)
        MovementService().start(c, world_id=e.wid, character_id=e.player, world_time=e.at,
            destination_longitude=e.loc["longitude"] + 0.003, destination_latitude=e.loc["latitude"])
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    with e.db.read() as c:
        at = from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0])
        assert EncounterService.current(c, e.wid, e.npc, e.player, at)["episode_id"] == initial["episode_id"]
        due = from_iso(c.execute("SELECT estimated_arrival_world FROM character_movements WHERE character_id=? AND status='moving'", (e.player,)).fetchone()[0])
    e.clock.heartbeat(e.wid, elapsed_seconds=(due-at).total_seconds()+0.001)
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM encounter_episodes WHERE npc_id=?", (e.npc,)).fetchone()[0] == 1
        assert c.execute("SELECT ended_at FROM encounter_episodes WHERE id=?", (initial["episode_id"],)).fetchone()[0] is None


def test_three_people_keep_each_pair_while_other_stationary_encounters_end(town):
    e = town
    third, _, _ = group(e)
    with e.db.write() as c:
        SocietyService.tick(c, e.wid, e.at)
        episodes = {cid: EncounterService.current(c, e.wid, cid, e.player, e.at)["episode_id"] for cid in (e.npc, third)}
        MovementService().start(c, world_id=e.wid, character_id=e.player, world_time=e.at,
            destination_longitude=e.loc["longitude"] + 0.003, destination_latitude=e.loc["latitude"])
    e.clock.heartbeat(e.wid, elapsed_seconds=70)
    with e.db.read() as c:
        at = from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0])
        for cid, episode in episodes.items():
            current = EncounterService.current(c, e.wid, cid, e.player, at)
            assert current and current["episode_id"] == episode
            assert c.execute("SELECT COUNT(*) FROM encounter_episodes WHERE npc_id=?", (cid,)).fetchone()[0] == 1
