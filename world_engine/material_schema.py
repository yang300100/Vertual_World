"""哥哥，批次状态与物质去向独立保存；普通世界不用新增灾难规则。"""

MATERIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS material_clock_steps (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), item_ids_json TEXT NOT NULL,
 before_age_json TEXT NOT NULL, after_age_json TEXT NOT NULL, world_time TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS material_clock_no_update BEFORE UPDATE ON material_clock_steps
 BEGIN SELECT RAISE(ABORT,'material clock ledger is immutable'); END;
CREATE TRIGGER IF NOT EXISTS material_clock_no_delete BEFORE DELETE ON material_clock_steps
 BEGIN SELECT RAISE(ABORT,'material clock ledger is immutable'); END;
CREATE TABLE IF NOT EXISTS material_modes (
 world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
 policy_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS material_batches (
 item_id TEXT PRIMARY KEY REFERENCES item_instances(id) ON DELETE RESTRICT,
 world_id TEXT NOT NULL REFERENCES worlds(id), quantity_ref INTEGER NOT NULL CHECK(quantity_ref>0),
 state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_sinks (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
 destination TEXT NOT NULL CHECK(length(destination)>0), state_json TEXT NOT NULL,
 source_item_id TEXT NOT NULL, world_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_ledger (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), operation TEXT NOT NULL,
 before_json TEXT NOT NULL, after_json TEXT NOT NULL, item_ids_json TEXT NOT NULL,
 destination TEXT, world_time TEXT NOT NULL, source_event_id TEXT REFERENCES world_events(id)
);
CREATE INDEX IF NOT EXISTS material_ledger_world ON material_ledger(world_id);
CREATE TABLE IF NOT EXISTS material_mutation_permits (item_id TEXT PRIMARY KEY);
CREATE TRIGGER IF NOT EXISTS protect_tracked_material_quantity
 BEFORE UPDATE OF quantity ON item_instances
 WHEN EXISTS(SELECT 1 FROM material_batches WHERE item_id=OLD.id)
 AND NOT EXISTS(SELECT 1 FROM material_mutation_permits WHERE item_id=OLD.id)
 BEGIN SELECT RAISE(ABORT,'material quantity change requires material transaction'); END;
CREATE TRIGGER IF NOT EXISTS protect_tracked_material_delete
 BEFORE DELETE ON item_instances
 WHEN EXISTS(SELECT 1 FROM material_batches WHERE item_id=OLD.id)
 AND NOT EXISTS(SELECT 1 FROM material_mutation_permits WHERE item_id=OLD.id)
 BEGIN SELECT RAISE(ABORT,'material deletion requires material transaction'); END;
CREATE TRIGGER IF NOT EXISTS protect_material_state_update
 BEFORE UPDATE ON material_batches
 WHEN NOT EXISTS(SELECT 1 FROM material_mutation_permits WHERE item_id=OLD.item_id)
 BEGIN SELECT RAISE(ABORT,'material state update requires material transaction'); END;
CREATE TRIGGER IF NOT EXISTS protect_material_state_delete
 BEFORE DELETE ON material_batches
 WHEN NOT EXISTS(SELECT 1 FROM material_mutation_permits WHERE item_id=OLD.item_id)
 BEGIN SELECT RAISE(ABORT,'material state deletion requires material transaction'); END;
CREATE TRIGGER IF NOT EXISTS material_ledger_immutable_update
 BEFORE UPDATE ON material_ledger
 BEGIN SELECT RAISE(ABORT,'material ledger is immutable'); END;
CREATE TRIGGER IF NOT EXISTS material_ledger_immutable_delete
 BEFORE DELETE ON material_ledger
 BEGIN SELECT RAISE(ABORT,'material ledger is immutable'); END;
"""


def install_material_schema(connection):
    connection.executescript(MATERIAL_SCHEMA)
