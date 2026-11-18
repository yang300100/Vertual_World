"""哥哥，房主审价对 NPC 和玩家使用同一规则，而且不会直接交房。"""

import sqlite3
from datetime import UTC, datetime

import pytest

from world_engine.lodging_review import review


@pytest.fixture
def housing():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE locations(id TEXT, world_id TEXT, is_active INTEGER);
        CREATE TABLE life_rooms(id TEXT, world_id TEXT, owner_character_id TEXT, location_id TEXT);
        CREATE TABLE room_rental_rates(room_id TEXT, nightly_rate INTEGER);
        CREATE TABLE contract_fulfillments(world_id TEXT, kind TEXT, asset_id TEXT, status TEXT);
        CREATE TABLE relationships(world_id TEXT, source_character_id TEXT,
                                  target_character_id TEXT,
                                  affinity INTEGER, trust INTEGER);
        INSERT INTO locations VALUES ('inn','world',1);
        INSERT INTO life_rooms VALUES ('room','world','owner','inn');
        INSERT INTO room_rental_rates VALUES ('room',3);
        INSERT INTO relationships VALUES ('world','owner','renter',10,10);
        """
    )
    yield c
    c.close()


def decide(c, **terms):
    return review(
        c,
        "world",
        {"id": "renter"},
        {"id": "owner"},
        {"room_id": "room", "duration_days": 2, "payment": 6, **terms},
        datetime(2040, 4, 1, 8, tzinfo=UTC),
    )


def test_acceptance_is_read_only_and_has_no_player_assumption(housing):
    before = housing.total_changes
    result = decide(housing)
    assert result[0] == "npc_accepted" and result[1] is None
    assert "玩家" not in result[2]
    assert housing.total_changes == before


def test_insufficient_rent_counter_keeps_complete_terms(housing):
    status, counter, _ = decide(housing, payment=1, title="独立客房", details="实际登记租住")
    assert status == "npc_countered"
    assert counter == {
        "room_id": "room",
        "duration_days": 2,
        "payment": 6,
        "title": "独立客房",
        "details": "实际登记租住",
    }


@pytest.mark.parametrize(
    "score,status,multiplier",
    [(14, "npc_countered", 2), (15, "npc_accepted", 1), (-35, "npc_rejected", 1)],
)
def test_relationship_thresholds_preserve_original_housing_rule(housing, score, status, multiplier):
    housing.execute("UPDATE relationships SET affinity=?,trust=0", (score,))
    result = decide(housing)
    assert result[0] == status
    if status == "npc_countered":
        assert result[1]["payment"] == 6 * multiplier


def test_relationship_score_uses_both_directions(housing):
    housing.execute("UPDATE relationships SET affinity=10,trust=0")
    housing.execute("INSERT INTO relationships VALUES ('world','renter','owner',20,0)")
    assert decide(housing)[0] == "npc_accepted"


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration_days", True),
        ("duration_days", 0),
        ("duration_days", 3651),
        ("duration_days", 1.0),
        ("duration_days", "2"),
        ("payment", True),
        ("payment", False),
        ("payment", -1),
        ("payment", 1.0),
        ("payment", "6"),
        ("payment", 1_000_001),
    ],
)
def test_housing_amounts_and_days_are_strict_bounded_integers(housing, field, value):
    assert decide(housing, **{field: value})[0] == "npc_rejected"


def test_original_housing_duration_limit_is_not_narrowed_to_thirty(housing):
    assert decide(housing, duration_days=3650, payment=10950)[0] == "npc_accepted"


def test_omitted_days_quote_original_one_day(housing):
    result = review(
        housing,
        "world",
        {"id": "renter"},
        {"id": "owner"},
        {"room_id": "room", "payment": 1},
        datetime(2040, 4, 1, tzinfo=UTC),
    )
    assert result[0] == "npc_countered"
    assert result[1]["duration_days"] == 1 and result[1]["payment"] == 3


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE life_rooms SET owner_character_id='third'",
        "UPDATE life_rooms SET world_id='other'",
        "UPDATE locations SET is_active=0",
        "DELETE FROM room_rental_rates",
        "INSERT INTO contract_fulfillments VALUES ('world','lodging','room','active')",
    ],
)
def test_actual_owner_rental_offer_and_occupancy_are_required(housing, change):
    housing.execute(change)
    assert decide(housing)[0] == "npc_rejected"


def test_inexecutable_total_and_counter_are_rejected(housing):
    housing.execute("UPDATE room_rental_rates SET nightly_rate=100000")
    assert decide(housing, duration_days=11, payment=1_000_000)[0] == "npc_rejected"
    housing.execute("UPDATE relationships SET affinity=0,trust=0")
    assert decide(housing, duration_days=6, payment=600000)[0] == "npc_rejected"
