"""哥哥，地区需求、生长、实际摄入和废物维护不会靠报告文字改写。"""

REGION_SCHEMA = """
CREATE TABLE IF NOT EXISTS collective_toxic_burdens (
 world_id TEXT NOT NULL REFERENCES worlds(id), recipient_kind TEXT NOT NULL, recipient_id TEXT NOT NULL,
 burden REAL NOT NULL, last_at TEXT NOT NULL, damage_total REAL NOT NULL, charged_damage INTEGER NOT NULL,
 PRIMARY KEY(recipient_kind,recipient_id)
);
CREATE TABLE IF NOT EXISTS collective_body_stress (
 actor_id TEXT PRIMARY KEY REFERENCES characters(id), starvation_hours REAL NOT NULL DEFAULT 0,
 damage_residual REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS collective_deaths (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), actor_id TEXT REFERENCES characters(id),
 region_id TEXT, count INTEGER NOT NULL CHECK(count>0), at TEXT NOT NULL, cause TEXT NOT NULL,
 UNIQUE(actor_id), UNIQUE(region_id)
);
CREATE TRIGGER IF NOT EXISTS collective_death_no_update BEFORE UPDATE ON collective_deaths
 BEGIN SELECT RAISE(ABORT,'death ledger is immutable'); END;
CREATE TRIGGER IF NOT EXISTS collective_death_no_delete BEFORE DELETE ON collective_deaths
 BEGIN SELECT RAISE(ABORT,'death ledger is immutable'); END;
CREATE TABLE IF NOT EXISTS collective_runoff_times (
 region_id TEXT PRIMARY KEY, last_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collective_regions (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), location_id TEXT NOT NULL REFERENCES locations(id),
 manager_id TEXT NOT NULL REFERENCES characters(id), population INTEGER NOT NULL CHECK(population>0),
 daily_nutrition REAL NOT NULL CHECK(daily_nutrition>0), baseline_soil_mass REAL NOT NULL,
 soil_id TEXT NOT NULL, growth_source_id TEXT NOT NULL, water_id TEXT NOT NULL,
 seed_baseline REAL NOT NULL DEFAULT 1, salt_limit REAL NOT NULL DEFAULT 0.005,
 particle_limit REAL NOT NULL DEFAULT 0.01, safe_days INTEGER NOT NULL DEFAULT 0,
 health REAL NOT NULL DEFAULT 100, deaths INTEGER NOT NULL DEFAULT 0,
 hunger REAL NOT NULL DEFAULT 0, started_at TEXT NOT NULL, last_consumed_at TEXT NOT NULL,
 UNIQUE(world_id,location_id)
);
CREATE TRIGGER IF NOT EXISTS collective_region_fixed_demand BEFORE UPDATE OF population,daily_nutrition,baseline_soil_mass,seed_baseline,salt_limit,particle_limit ON collective_regions
 BEGIN SELECT RAISE(ABORT,'region baseline demand is immutable'); END;
CREATE TABLE IF NOT EXISTS collective_region_permissions (
 region_id TEXT NOT NULL REFERENCES collective_regions(id), actor_id TEXT NOT NULL REFERENCES characters(id),
 operation TEXT NOT NULL, granted_by TEXT NOT NULL REFERENCES characters(id), reason TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(region_id,actor_id,operation)
);
CREATE TABLE IF NOT EXISTS collective_food_days (
 region_id TEXT NOT NULL REFERENCES collective_regions(id), consumed_at TEXT NOT NULL,
 required_nutrition REAL NOT NULL, delivered_nutrition REAL NOT NULL, safe_nutrition REAL NOT NULL,
 material_json TEXT NOT NULL, exposure REAL NOT NULL, shortage REAL NOT NULL,
 PRIMARY KEY(region_id,consumed_at)
);
CREATE TRIGGER IF NOT EXISTS collective_food_day_no_update BEFORE UPDATE ON collective_food_days
 BEGIN SELECT RAISE(ABORT,'food ledger is immutable'); END;
CREATE TRIGGER IF NOT EXISTS collective_food_day_no_delete BEFORE DELETE ON collective_food_days
 BEGIN SELECT RAISE(ABORT,'food ledger is immutable'); END;
CREATE TABLE IF NOT EXISTS collective_exposures (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), actor_id TEXT REFERENCES characters(id),
 region_id TEXT REFERENCES collective_regions(id), source_batch_id TEXT NOT NULL, dose REAL NOT NULL,
 ingested_at TEXT NOT NULL, due_at TEXT NOT NULL, settled_at TEXT, damage REAL
);
CREATE TABLE IF NOT EXISTS collective_nutrition_residuals (
 actor_id TEXT PRIMARY KEY REFERENCES characters(id), value REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS collective_domain_jobs (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), actor_id TEXT NOT NULL REFERENCES characters(id),
 activity_id TEXT NOT NULL REFERENCES character_life_activities(id), program_id TEXT REFERENCES collective_programs(id),
 op TEXT NOT NULL, spec_json TEXT NOT NULL, inputs_json TEXT NOT NULL DEFAULT '[]',
 started_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'working', result_json TEXT
);
CREATE TABLE IF NOT EXISTS collective_recurring_jobs (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), actor_id TEXT NOT NULL REFERENCES characters(id),
 spec_json TEXT NOT NULL, next_due_at TEXT NOT NULL, remaining INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'active', error TEXT
);
CREATE TABLE IF NOT EXISTS collective_crops (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), region_id TEXT NOT NULL REFERENCES collective_regions(id),
 actor_id TEXT NOT NULL REFERENCES characters(id), crop_item_id TEXT NOT NULL,
 planted_at TEXT NOT NULL, due_at TEXT NOT NULL, last_grown_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'growing', target_growth_mass REAL NOT NULL, grown_mass REAL NOT NULL DEFAULT 0,
 seed_retention REAL NOT NULL DEFAULT 0.05, harvested_at TEXT, food_id TEXT, seed_id TEXT,
 yield_nutrition REAL NOT NULL DEFAULT 0, output_seed_viability REAL
);
CREATE TABLE IF NOT EXISTS collective_food_decisions (
 actor_id TEXT NOT NULL REFERENCES characters(id), batch_id TEXT NOT NULL, policy TEXT NOT NULL,
 reason TEXT NOT NULL, decided_at TEXT NOT NULL, PRIMARY KEY(actor_id,batch_id)
);
CREATE TABLE IF NOT EXISTS collective_liners (
 item_type_id TEXT PRIMARY KEY REFERENCES item_types(id), capacity_per_unit REAL NOT NULL CHECK(capacity_per_unit>0),
 leak_fraction REAL NOT NULL CHECK(leak_fraction>=0 AND leak_fraction<=1),
 maintenance_hours REAL NOT NULL DEFAULT 1, maintenance_interval_hours REAL NOT NULL DEFAULT 168,
 observation_window_hours REAL NOT NULL DEFAULT 336
);
CREATE TABLE IF NOT EXISTS collective_waste_sites (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), region_id TEXT NOT NULL REFERENCES collective_regions(id),
 owner_id TEXT NOT NULL REFERENCES characters(id), capacity REAL NOT NULL, liner_type_id TEXT REFERENCES item_types(id),
 leak_fraction REAL NOT NULL, created_at TEXT NOT NULL, last_processed_at TEXT NOT NULL,
 last_maintained_at TEXT NOT NULL, observation_window_hours REAL NOT NULL, leaked_salt REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS collective_sink_recoveries (
 sink_id TEXT PRIMARY KEY REFERENCES material_sinks(id), output_id TEXT NOT NULL, recovered_at TEXT NOT NULL
);
"""


def install_region_schema(c):
    c.executescript(REGION_SCHEMA)
