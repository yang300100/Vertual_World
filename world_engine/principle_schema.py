"""哥哥，原理研发与旧实物认证分开记账，不把理论依据改写为实验结果。"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS principle_sessions (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
 status TEXT NOT NULL DEFAULT 'prepared'
 CHECK(status IN ('prepared','running','completed','stopped')),
 target_count INTEGER NOT NULL CHECK(target_count>0), max_calls INTEGER NOT NULL CHECK(max_calls>0),
 token_limit INTEGER NOT NULL CHECK(token_limit>0), created_at TEXT NOT NULL,
 stop_reason TEXT, manifest_json TEXT NOT NULL DEFAULT '{}', cursor INTEGER NOT NULL DEFAULT 0,
 UNIQUE(id,world_id)
);
CREATE TRIGGER IF NOT EXISTS principle_budget_immutable
 BEFORE UPDATE OF world_id,target_count,max_calls,token_limit,created_at ON principle_sessions
 BEGIN SELECT RAISE(ABORT,'principle budget is immutable'); END;
CREATE TABLE IF NOT EXISTS principle_calls (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES principle_sessions(id),
 actor_id TEXT REFERENCES characters(id),
 purpose TEXT NOT NULL CHECK(purpose IN ('propose','review')),
 status TEXT NOT NULL CHECK(status IN ('reserved','succeeded','failed','unknown')),
 input_json TEXT NOT NULL, system_prompt TEXT NOT NULL, response_json TEXT,
 reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens>0), accounted_tokens INTEGER,
 input_tokens INTEGER, output_tokens INTEGER, error TEXT, created_at TEXT NOT NULL,
 finished_at TEXT, owner_pid INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS principle_meals (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), actor_id TEXT NOT NULL,
 batch_id TEXT NOT NULL, quantity_before INTEGER NOT NULL, quantity_after INTEGER NOT NULL,
 satiety_before INTEGER NOT NULL, satiety_after INTEGER NOT NULL, occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS principle_material_catalogue (
 world_id TEXT NOT NULL REFERENCES worlds(id),kind TEXT NOT NULL,item_type_id TEXT NOT NULL,
 PRIMARY KEY(world_id,kind), FOREIGN KEY(item_type_id) REFERENCES item_types(id)
);
CREATE TABLE IF NOT EXISTS principle_prototypes (
 technology_id TEXT PRIMARY KEY REFERENCES principle_technologies(id),
 actor_id TEXT NOT NULL REFERENCES characters(id),
 activity_id TEXT NOT NULL REFERENCES character_life_activities(id),
 item_id TEXT NOT NULL,inputs_json TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS principle_material_uses (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL,actor_id TEXT NOT NULL,technology_id TEXT NOT NULL,
 purpose TEXT NOT NULL,inputs_json TEXT NOT NULL,occurred_at TEXT NOT NULL,
 FOREIGN KEY(technology_id) REFERENCES principle_technologies(id)
);
CREATE TABLE IF NOT EXISTS principle_application_attempts (
 id TEXT PRIMARY KEY,technology_id TEXT NOT NULL REFERENCES principle_technologies(id),
 result_json TEXT NOT NULL,occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS principle_proposals (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES principle_sessions(id),
 world_id TEXT NOT NULL REFERENCES worlds(id), inventor_id TEXT NOT NULL REFERENCES characters(id),
 source_call_id TEXT NOT NULL REFERENCES principle_calls(id), spec_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'proposed'
 CHECK(status IN ('proposed','researched','rejected','duplicate')),
 review_call_id TEXT REFERENCES principle_calls(id), review_json TEXT,
 created_at TEXT NOT NULL, UNIQUE(id,world_id)
);
CREATE TABLE IF NOT EXISTS principle_technologies (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
 session_id TEXT NOT NULL REFERENCES principle_sessions(id), proposal_id TEXT NOT NULL UNIQUE,
 inventor_id TEXT NOT NULL REFERENCES characters(id), name TEXT NOT NULL,
 problem TEXT NOT NULL, principle TEXT NOT NULL, requirements_json TEXT NOT NULL,
 limitations TEXT NOT NULL, application TEXT NOT NULL, effect_json TEXT NOT NULL,
 identity_hash TEXT NOT NULL, basis TEXT NOT NULL DEFAULT 'principle_review',
 created_at TEXT NOT NULL, event_id TEXT REFERENCES world_events(id),
 UNIQUE(world_id,identity_hash), UNIQUE(id,world_id),
 FOREIGN KEY(proposal_id,world_id) REFERENCES principle_proposals(id,world_id)
);
CREATE TABLE IF NOT EXISTS principle_projects (
 proposal_id TEXT PRIMARY KEY REFERENCES principle_proposals(id),
 world_id TEXT NOT NULL REFERENCES worlds(id),
 review_call_id TEXT NOT NULL REFERENCES principle_calls(id),
 required_minutes INTEGER NOT NULL CHECK(required_minutes>0 AND required_minutes<=18000),
 earned_minutes INTEGER NOT NULL DEFAULT 0
 CHECK(earned_minutes>=0 AND earned_minutes<=required_minutes),
 status TEXT NOT NULL CHECK(status IN ('researching','paused','blocked','completed')),
 reason TEXT NOT NULL DEFAULT '',policy_json TEXT NOT NULL,started_at TEXT NOT NULL,
 completed_at TEXT,technology_id TEXT UNIQUE REFERENCES principle_technologies(id)
);
CREATE TRIGGER IF NOT EXISTS principle_cycle_immutable
 BEFORE UPDATE OF world_id,review_call_id,required_minutes,policy_json,started_at
 ON principle_projects
 BEGIN SELECT RAISE(ABORT,'research cycle is immutable'); END;
CREATE TABLE IF NOT EXISTS principle_research_work (
 activity_id TEXT PRIMARY KEY REFERENCES character_life_activities(id),
 proposal_id TEXT NOT NULL REFERENCES principle_projects(proposal_id),
 planned_minutes INTEGER NOT NULL CHECK(planned_minutes>0 AND planned_minutes<=60),
 credited_minutes INTEGER NOT NULL DEFAULT 0
 CHECK(credited_minutes>=0 AND credited_minutes<=planned_minutes),
 settled_at TEXT
);
CREATE TABLE IF NOT EXISTS principle_dependencies (
 world_id TEXT NOT NULL, parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
 PRIMARY KEY(parent_id,child_id), CHECK(parent_id!=child_id),
 FOREIGN KEY(parent_id,world_id) REFERENCES principle_technologies(id,world_id),
 FOREIGN KEY(child_id,world_id) REFERENCES principle_technologies(id,world_id)
);
CREATE TABLE IF NOT EXISTS principle_inventions (
 id TEXT PRIMARY KEY,technology_id TEXT NOT NULL,world_id TEXT NOT NULL,
 design_index INTEGER NOT NULL,spec_json TEXT NOT NULL,review_json TEXT NOT NULL,
 effect_json TEXT,status TEXT NOT NULL CHECK(status IN ('approved','rejected')),
 UNIQUE(technology_id,design_index),UNIQUE(id,world_id),
 FOREIGN KEY(technology_id,world_id) REFERENCES principle_technologies(id,world_id)
);
CREATE TABLE IF NOT EXISTS principle_invention_prototypes (
 invention_id TEXT PRIMARY KEY REFERENCES principle_inventions(id),actor_id TEXT NOT NULL,
 activity_id TEXT NOT NULL REFERENCES character_life_activities(id),item_id TEXT NOT NULL,
 inputs_json TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS principle_product_properties (
 item_id TEXT PRIMARY KEY REFERENCES item_instances(id),strength REAL NOT NULL DEFAULT 1,
 precision REAL NOT NULL DEFAULT 1,wear_accumulated REAL NOT NULL DEFAULT 0,
 invention_id TEXT NOT NULL REFERENCES principle_inventions(id),
 CHECK(strength>=1 AND strength<=1.4),CHECK(precision>=1 AND precision<=1.4)
);
CREATE TABLE IF NOT EXISTS principle_reuses (
 parent_id TEXT NOT NULL,child_id TEXT NOT NULL,invention_id TEXT,
 actor_id TEXT NOT NULL,source_id TEXT NOT NULL,occurred_at TEXT NOT NULL,
 PRIMARY KEY(parent_id,child_id,source_id),
 FOREIGN KEY(parent_id,child_id) REFERENCES principle_dependencies(parent_id,child_id)
);
CREATE TABLE IF NOT EXISTS magic_patterns (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id),name TEXT NOT NULL,
 operation TEXT NOT NULL CHECK(operation IN ('cast','transfer')),coupling_key TEXT NOT NULL,
 base_cost REAL NOT NULL CHECK(base_cost>0),useful_output REAL NOT NULL CHECK(useful_output>0),
 base_efficiency REAL NOT NULL CHECK(base_efficiency>0 AND base_efficiency<1),
 max_efficiency REAL NOT NULL CHECK(max_efficiency>=base_efficiency AND max_efficiency<1),
 source_event_id TEXT NOT NULL REFERENCES world_events(id),UNIQUE(id,world_id)
);
CREATE TABLE IF NOT EXISTS magic_known_patterns (
 world_id TEXT NOT NULL,actor_id TEXT NOT NULL REFERENCES characters(id),pattern_id TEXT NOT NULL,
 learned_at TEXT NOT NULL,source_event_id TEXT NOT NULL REFERENCES world_events(id),
 PRIMARY KEY(actor_id,pattern_id),
 FOREIGN KEY(pattern_id,world_id) REFERENCES magic_patterns(id,world_id)
);
CREATE TABLE IF NOT EXISTS magic_attunements (
 actor_id TEXT PRIMARY KEY REFERENCES characters(id),world_id TEXT NOT NULL REFERENCES worlds(id),
 guided_at TEXT NOT NULL,source_event_id TEXT NOT NULL REFERENCES world_events(id)
);
CREATE TABLE IF NOT EXISTS magic_energy_devices (
 item_id TEXT PRIMARY KEY REFERENCES item_instances(id),
 world_id TEXT NOT NULL REFERENCES worlds(id),
 coupling_key TEXT NOT NULL,capacity REAL NOT NULL CHECK(capacity>0),
 stored REAL NOT NULL CHECK(stored>=0 AND stored<=capacity),
 output_per_minute REAL NOT NULL CHECK(output_per_minute>0),
 source_event_id TEXT NOT NULL REFERENCES world_events(id)
);
CREATE TABLE IF NOT EXISTS magic_applications (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL,actor_id TEXT NOT NULL,technology_id TEXT NOT NULL,
 invention_id TEXT NOT NULL,pattern_id TEXT NOT NULL,source_item_id TEXT NOT NULL,
 receiver_item_id TEXT,activity_id TEXT NOT NULL REFERENCES character_life_activities(id),
 source_before REAL NOT NULL,source_after REAL NOT NULL,receiver_before REAL,receiver_after REAL,
 output REAL NOT NULL,loss REAL NOT NULL,occurred_at TEXT NOT NULL,
 auxiliary_use_id TEXT UNIQUE REFERENCES principle_material_uses(id),
 UNIQUE(invention_id,activity_id),
 FOREIGN KEY(invention_id,world_id) REFERENCES principle_inventions(id,world_id),
 FOREIGN KEY(pattern_id,world_id) REFERENCES magic_patterns(id,world_id)
);
CREATE TABLE IF NOT EXISTS principle_knowledge (
 world_id TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES characters(id),
 technology_id TEXT NOT NULL, learned_at TEXT NOT NULL, source_actor_id TEXT,
 PRIMARY KEY(actor_id,technology_id),
 FOREIGN KEY(technology_id,world_id) REFERENCES principle_technologies(id,world_id)
);
CREATE TABLE IF NOT EXISTS principle_relations (
 world_id TEXT NOT NULL, parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('requires','improves','complements')),
 explanation TEXT NOT NULL,review_call_id TEXT NOT NULL REFERENCES principle_calls(id),
 PRIMARY KEY(parent_id,child_id,kind), CHECK(parent_id!=child_id),
 FOREIGN KEY(parent_id,world_id) REFERENCES principle_technologies(id,world_id),
 FOREIGN KEY(child_id,world_id) REFERENCES principle_technologies(id,world_id)
);
CREATE TABLE IF NOT EXISTS principle_adoptions (
 world_id TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES characters(id),
 technology_id TEXT NOT NULL, adopted_at TEXT NOT NULL,
 PRIMARY KEY(actor_id,technology_id),
 FOREIGN KEY(technology_id,world_id) REFERENCES principle_technologies(id,world_id)
);
CREATE TABLE IF NOT EXISTS principle_impacts (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), actor_id TEXT NOT NULL,
 technology_id TEXT NOT NULL, kind TEXT NOT NULL,
 source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
 before_json TEXT NOT NULL, after_json TEXT NOT NULL, occurred_at TEXT NOT NULL,
 event_id TEXT REFERENCES world_events(id),
 FOREIGN KEY(technology_id,world_id) REFERENCES principle_technologies(id,world_id),
 UNIQUE(technology_id,source_kind,source_id)
);
CREATE INDEX IF NOT EXISTS principle_calls_session ON principle_calls(session_id,status);
CREATE INDEX IF NOT EXISTS principle_actor_knowledge ON principle_knowledge(actor_id,learned_at);
CREATE INDEX IF NOT EXISTS principle_world_impact ON principle_impacts(world_id,occurred_at);
"""


def install_principle_schema(c):
    c.executescript(SCHEMA)
    columns = {r[1] for r in c.execute("PRAGMA table_info(principle_calls)")}
    for column in ("model_name", "diagnostics_json"):
        if column not in columns:
            c.execute(f"ALTER TABLE principle_calls ADD COLUMN {column} TEXT")
    for table in ("principle_impacts", "principle_application_attempts"):
        columns = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if "invention_id" not in columns:
            c.execute(
                f"ALTER TABLE {table} ADD COLUMN invention_id TEXT "
                "REFERENCES principle_inventions(id)"
            )
    columns = {r[1] for r in c.execute("PRAGMA table_info(magic_applications)")}
    if "auxiliary_use_id" not in columns:
        c.execute(
            "ALTER TABLE magic_applications ADD COLUMN auxiliary_use_id TEXT "
            "REFERENCES principle_material_uses(id)"
        )
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS magic_auxiliary_once "
        "ON magic_applications(auxiliary_use_id) WHERE auxiliary_use_id IS NOT NULL"
    )
