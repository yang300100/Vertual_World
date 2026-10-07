"""Yuki 保存预测、阶段程序、实测与实际劳动的来源。"""

COLLECTIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS collective_assay_results (
 order_id TEXT PRIMARY KEY REFERENCES collective_work_orders(id),
 observation_id TEXT NOT NULL REFERENCES collective_observations(id)
);
CREATE TRIGGER IF NOT EXISTS collective_assay_result_no_update BEFORE UPDATE ON collective_assay_results
 BEGIN SELECT RAISE(ABORT,'assay result is immutable'); END;
CREATE TRIGGER IF NOT EXISTS collective_assay_result_no_delete BEFORE DELETE ON collective_assay_results
 BEGIN SELECT RAISE(ABORT,'assay result is immutable'); END;
CREATE TABLE IF NOT EXISTS collective_task_sources (
 proposal_id TEXT NOT NULL REFERENCES collective_proposals(id), version INTEGER NOT NULL,
 task_key TEXT NOT NULL, program_id TEXT NOT NULL REFERENCES collective_programs(id),
 origin_version INTEGER NOT NULL, previous_task_json TEXT, task_json TEXT NOT NULL,
 evidence_ids_json TEXT NOT NULL, PRIMARY KEY(proposal_id,version,task_key)
);
CREATE TRIGGER IF NOT EXISTS collective_task_source_no_update BEFORE UPDATE ON collective_task_sources
 BEGIN SELECT RAISE(ABORT,'task source is immutable'); END;
CREATE TRIGGER IF NOT EXISTS collective_task_source_no_delete BEFORE DELETE ON collective_task_sources
 BEGIN SELECT RAISE(ABORT,'task source is immutable'); END;
CREATE TABLE IF NOT EXISTS collective_component_sources (
 proposal_id TEXT NOT NULL REFERENCES collective_proposals(id), version INTEGER NOT NULL,
 program_id TEXT NOT NULL REFERENCES collective_programs(id), command_index INTEGER NOT NULL,
 PRIMARY KEY(proposal_id,version)
);
CREATE TABLE IF NOT EXISTS collective_process_traces (
 ledger_id TEXT PRIMARY KEY REFERENCES material_ledger(id),
 source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
 operation TEXT NOT NULL, settings_json TEXT NOT NULL, hours REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS collective_trace_no_update BEFORE UPDATE ON collective_process_traces
 BEGIN SELECT RAISE(ABORT,'process trace is immutable'); END;
CREATE TRIGGER IF NOT EXISTS collective_trace_no_delete BEFORE DELETE ON collective_process_traces
 BEGIN SELECT RAISE(ABORT,'process trace is immutable'); END;
CREATE TRIGGER IF NOT EXISTS collective_component_no_update BEFORE UPDATE ON collective_component_sources
 BEGIN SELECT RAISE(ABORT,'component source is immutable'); END;
CREATE TRIGGER IF NOT EXISTS collective_component_no_delete BEFORE DELETE ON collective_component_sources
 BEGIN SELECT RAISE(ABORT,'component source is immutable'); END;
CREATE TABLE IF NOT EXISTS collective_people (
 character_id TEXT PRIMARY KEY REFERENCES characters(id), world_id TEXT NOT NULL REFERENCES worlds(id),
 lab_location_id TEXT NOT NULL REFERENCES locations(id), enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS collective_carry_limits (
 actor_id TEXT PRIMARY KEY REFERENCES characters(id), max_load REAL NOT NULL CHECK(max_load>0)
);
CREATE TABLE IF NOT EXISTS collective_route_limits (
 sender_id TEXT NOT NULL REFERENCES characters(id), recipient_id TEXT NOT NULL REFERENCES characters(id),
 max_load REAL NOT NULL CHECK(max_load>0), PRIMARY KEY(sender_id,recipient_id)
);
CREATE TABLE IF NOT EXISTS collective_model_calls (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), actor_id TEXT NOT NULL REFERENCES characters(id),
 fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL,
 context_json TEXT NOT NULL, system_prompt TEXT NOT NULL, response_json TEXT, diagnostics_json TEXT NOT NULL DEFAULT '{}',
 input_tokens INTEGER, output_tokens INTEGER, reserved_tokens INTEGER NOT NULL, created_at TEXT NOT NULL,
 error TEXT
);
CREATE TABLE IF NOT EXISTS collective_runner_stops (
 world_id TEXT PRIMARY KEY REFERENCES worlds(id), reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collective_instruments (
 item_id TEXT PRIMARY KEY REFERENCES item_instances(id), world_id TEXT NOT NULL REFERENCES worlds(id),
 method TEXT NOT NULL, skill_name TEXT NOT NULL, min_proficiency INTEGER NOT NULL,
 duration_hours REAL NOT NULL CHECK(duration_hours>0), detection_limit REAL NOT NULL CHECK(detection_limit>=0),
 precision REAL NOT NULL CHECK(precision>0), supplies_json TEXT NOT NULL DEFAULT '[]',
 incubation_hours REAL NOT NULL DEFAULT 0 CHECK(incubation_hours>=0)
 ,sample_capacity INTEGER NOT NULL DEFAULT 5 CHECK(sample_capacity>0)
);
CREATE TABLE IF NOT EXISTS collective_programs (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), character_id TEXT NOT NULL REFERENCES characters(id),
 spec_json TEXT NOT NULL, pc INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
 created_at TEXT NOT NULL, call_id TEXT, error TEXT, bindings_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS collective_apparatus (
 item_id TEXT PRIMARY KEY REFERENCES item_instances(id), world_id TEXT NOT NULL REFERENCES worlds(id),
 operations_json TEXT NOT NULL, capacity REAL NOT NULL CHECK(capacity>0),
 throughput REAL NOT NULL CHECK(throughput>0), max_power REAL NOT NULL DEFAULT 1,
 skill_name TEXT NOT NULL DEFAULT '工艺', min_proficiency INTEGER NOT NULL DEFAULT 40,
 supplies_json TEXT NOT NULL DEFAULT '[]', stations_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS collective_hypotheses (
 id TEXT PRIMARY KEY, program_id TEXT NOT NULL REFERENCES collective_programs(id),
 hypothesis_index INTEGER NOT NULL, spec_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(program_id,hypothesis_index)
);
CREATE TABLE IF NOT EXISTS collective_observations (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), character_id TEXT NOT NULL REFERENCES characters(id),
 kind TEXT NOT NULL, summary TEXT NOT NULL, content_json TEXT NOT NULL, world_time TEXT NOT NULL,
 source_event_id TEXT REFERENCES world_events(id), source_observation_id TEXT REFERENCES collective_observations(id)
);
CREATE TABLE IF NOT EXISTS collective_jobs (
 id TEXT PRIMARY KEY, program_id TEXT NOT NULL REFERENCES collective_programs(id),
 command_index INTEGER NOT NULL, activity_id TEXT NOT NULL REFERENCES character_life_activities(id),
 spec_json TEXT NOT NULL, sample_id TEXT REFERENCES item_instances(id),
 instrument_id TEXT REFERENCES item_instances(id), started_at TEXT NOT NULL, due_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'working', result_json TEXT, observation_id TEXT REFERENCES collective_observations(id),
 UNIQUE(program_id,command_index)
);
CREATE TRIGGER IF NOT EXISTS collective_hypothesis_no_update BEFORE UPDATE ON collective_hypotheses
 BEGIN SELECT RAISE(ABORT,'predictions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS collective_hypothesis_no_delete BEFORE DELETE ON collective_hypotheses
 BEGIN SELECT RAISE(ABORT,'predictions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS collective_observation_no_update BEFORE UPDATE ON collective_observations
 BEGIN SELECT RAISE(ABORT,'observations are immutable'); END;
CREATE TABLE IF NOT EXISTS collective_contacts (
 sender_id TEXT NOT NULL REFERENCES characters(id), recipient_id TEXT NOT NULL REFERENCES characters(id),
 transit_hours REAL NOT NULL CHECK(transit_hours>=0), PRIMARY KEY(sender_id,recipient_id)
);
CREATE TABLE IF NOT EXISTS collective_messages (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), sender_id TEXT NOT NULL REFERENCES characters(id),
 recipient_id TEXT NOT NULL REFERENCES characters(id), content_json TEXT NOT NULL,
 sent_at TEXT NOT NULL, due_at TEXT NOT NULL, delivered_at TEXT,
 destination_id TEXT NOT NULL REFERENCES locations(id), cargo_id TEXT REFERENCES item_instances(id)
);
CREATE TABLE IF NOT EXISTS collective_proposals (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id), author_id TEXT NOT NULL REFERENCES characters(id),
 current_version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'proposed', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collective_proposal_versions (
 proposal_id TEXT NOT NULL REFERENCES collective_proposals(id), version INTEGER NOT NULL,
 author_id TEXT NOT NULL REFERENCES characters(id), reason TEXT NOT NULL, spec_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(proposal_id,version)
);
CREATE TABLE IF NOT EXISTS collective_acceptances (
 proposal_id TEXT NOT NULL REFERENCES collective_proposals(id), version INTEGER NOT NULL,
 task_key TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES characters(id), reason TEXT NOT NULL,
 accepted_at TEXT NOT NULL, inherited_from_version INTEGER,
 PRIMARY KEY(proposal_id,version,task_key,actor_id)
);
CREATE TABLE IF NOT EXISTS collective_resource_permissions (
 proposal_id TEXT NOT NULL REFERENCES collective_proposals(id), version INTEGER NOT NULL,
 item_id TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES characters(id),
 allowed_actor_id TEXT NOT NULL REFERENCES characters(id), accepted_at TEXT NOT NULL,
 PRIMARY KEY(proposal_id,version,item_id,allowed_actor_id)
);
CREATE TABLE IF NOT EXISTS collective_rejections (
 proposal_id TEXT NOT NULL REFERENCES collective_proposals(id), version INTEGER NOT NULL,
 task_key TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES characters(id), reason TEXT NOT NULL,
 rejected_at TEXT NOT NULL, PRIMARY KEY(proposal_id,version,task_key,actor_id)
);
CREATE TABLE IF NOT EXISTS collective_work_orders (
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES collective_proposals(id), version INTEGER NOT NULL,
 task_key TEXT NOT NULL, worker_id TEXT NOT NULL REFERENCES characters(id),
 activity_id TEXT REFERENCES character_life_activities(id), spec_json TEXT NOT NULL,
 started_at TEXT, due_at TEXT, status TEXT NOT NULL DEFAULT 'queued', inputs_json TEXT NOT NULL DEFAULT '[]',
 outputs_json TEXT NOT NULL DEFAULT '[]', result_json TEXT,
 UNIQUE(proposal_id,version,task_key)
);
CREATE TRIGGER IF NOT EXISTS collective_version_immutable_update BEFORE UPDATE ON collective_proposal_versions
 BEGIN SELECT RAISE(ABORT,'proposal version is immutable'); END;
CREATE TABLE IF NOT EXISTS collective_work_people (
 order_id TEXT NOT NULL REFERENCES collective_work_orders(id), actor_id TEXT NOT NULL REFERENCES characters(id),
 activity_id TEXT NOT NULL REFERENCES character_life_activities(id), PRIMARY KEY(order_id,actor_id)
);
CREATE TRIGGER IF NOT EXISTS collective_version_immutable_delete BEFORE DELETE ON collective_proposal_versions
 BEGIN SELECT RAISE(ABORT,'proposal version is immutable'); END;
"""


def install_collective_schema(connection):
    connection.executescript(COLLECTIVE_SCHEMA)
    # 哥哥，早期本地存档也可以继续读，绝不替换它已有的程序。
    columns = {r[1] for r in connection.execute("PRAGMA table_info(collective_programs)")}
    if "bindings_json" not in columns:
        connection.execute(
            "ALTER TABLE collective_programs ADD COLUMN bindings_json TEXT NOT NULL DEFAULT '{}'"
        )
