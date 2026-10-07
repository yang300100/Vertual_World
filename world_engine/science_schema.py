"""哥哥，科研会话和真实科技的来源长期留存，迁移不启动研究。"""

from uuid import uuid4

from world_engine.repository import to_iso

SCIENCE_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS science_characters_world ON characters(id,world_id);
CREATE UNIQUE INDEX IF NOT EXISTS science_observations_world ON
collective_observations(id,world_id);
CREATE TABLE IF NOT EXISTS science_sessions (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
 status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN
('prepared','running','stopping','stopped')),
 max_calls INTEGER NOT NULL CHECK(max_calls>0), token_limit INTEGER NOT NULL CHECK(token_limit>0),
 max_output_tokens INTEGER NOT NULL CHECK(max_output_tokens>0),
 created_at TEXT NOT NULL, stopped_at TEXT, stop_reason TEXT,
 freeze_json TEXT NOT NULL DEFAULT '{}', seed INTEGER NOT NULL DEFAULT 0,
 cursor_json TEXT NOT NULL DEFAULT '{}', UNIQUE(id,world_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS science_one_live_session
 ON science_sessions(world_id) WHERE status IN ('prepared','running','stopping');
CREATE TRIGGER IF NOT EXISTS science_budget_immutable
 BEFORE UPDATE OF world_id,max_calls,token_limit,max_output_tokens,created_at ON science_sessions
 BEGIN SELECT RAISE(ABORT,'science session identity and budget are immutable'); END;
CREATE TABLE IF NOT EXISTS science_model_calls (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, world_id TEXT NOT NULL,
 actor_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'reserved' CHECK(status IN
('reserved','succeeded','rejected','failed','unknown')),
 reason TEXT NOT NULL, context_json TEXT NOT NULL, system_prompt TEXT NOT NULL,
 response_json TEXT, diagnostics_json TEXT NOT NULL DEFAULT '{}',
 input_tokens INTEGER CHECK(input_tokens>=0), output_tokens INTEGER CHECK(output_tokens>=0),
 reasoning_tokens INTEGER CHECK(reasoning_tokens>=0), accounted_tokens INTEGER
CHECK(accounted_tokens>=0),
 reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens>0), created_at TEXT NOT NULL,
 finished_at TEXT, error TEXT, UNIQUE(id,world_id),
 FOREIGN KEY(session_id,world_id) REFERENCES science_sessions(id,world_id),
 FOREIGN KEY(actor_id,world_id) REFERENCES characters(id,world_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS science_call_once ON
science_model_calls(session_id,actor_id,fingerprint);
CREATE TABLE IF NOT EXISTS technology_candidates (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, world_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 name TEXT NOT NULL, problem TEXT NOT NULL, spec_json TEXT NOT NULL, revision INTEGER NOT NULL
DEFAULT 1 CHECK(revision>0),
 status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN
('proposed','needs_evidence','verified','unsupported','duplicate')),
 gaps_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 source_program_id TEXT, UNIQUE(id,world_id),
 FOREIGN KEY(session_id,world_id) REFERENCES science_sessions(id,world_id),
 FOREIGN KEY(actor_id,world_id) REFERENCES characters(id,world_id)
);
CREATE TABLE IF NOT EXISTS technologies (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, world_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
 inventor_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'verified'
 CHECK(status IN ('verified','invalidated')), current_version INTEGER NOT NULL DEFAULT 1
CHECK(current_version>0),
 created_at TEXT NOT NULL, invalidated_at TEXT, UNIQUE(id,world_id),
 FOREIGN KEY(session_id,world_id) REFERENCES science_sessions(id,world_id),
 FOREIGN KEY(candidate_id,world_id) REFERENCES technology_candidates(id,world_id),
 FOREIGN KEY(inventor_id,world_id) REFERENCES characters(id,world_id)
);
CREATE TABLE IF NOT EXISTS technology_versions (
 technology_id TEXT NOT NULL, world_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
 method_json TEXT NOT NULL, claim_json TEXT NOT NULL, method_fingerprint TEXT NOT NULL,
 claim_fingerprint TEXT NOT NULL, conditions_json TEXT NOT NULL, source_candidate_id TEXT NOT NULL,
 source_revision INTEGER NOT NULL CHECK(source_revision>0), created_at TEXT NOT NULL,
 PRIMARY KEY(technology_id,version), UNIQUE(technology_id,version,world_id),
 FOREIGN KEY(technology_id,world_id) REFERENCES technologies(id,world_id),
 FOREIGN KEY(source_candidate_id,world_id) REFERENCES technology_candidates(id,world_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS technology_method_identity
 ON technology_versions(world_id,method_fingerprint,claim_fingerprint);
CREATE TABLE IF NOT EXISTS technology_executions (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, world_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 technology_id TEXT, version INTEGER, candidate_id TEXT, candidate_revision INTEGER,
 method_json TEXT NOT NULL, bindings_json TEXT NOT NULL, parameters_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN
('pending','running','completed','rejected','unsupported','interrupted')),
 source_program_id TEXT, work_ids_json TEXT NOT NULL DEFAULT '[]', output_ids_json TEXT NOT
NULL DEFAULT '[]',
 observation_ids_json TEXT NOT NULL DEFAULT '[]', ledger_ids_json TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, error TEXT,
 UNIQUE(id,world_id),
 CHECK((technology_id IS NULL AND version IS NULL) OR (technology_id IS NOT NULL AND version IS
NOT NULL)),
 CHECK((candidate_id IS NULL AND candidate_revision IS NULL) OR (candidate_id IS NOT NULL AND
candidate_revision>0)),
 FOREIGN KEY(session_id,world_id) REFERENCES science_sessions(id,world_id),
 FOREIGN KEY(actor_id,world_id) REFERENCES characters(id,world_id),
 FOREIGN KEY(technology_id,version,world_id) REFERENCES
technology_versions(technology_id,version,world_id),
 FOREIGN KEY(candidate_id,world_id) REFERENCES technology_candidates(id,world_id)
);
CREATE TABLE IF NOT EXISTS technology_dependencies (
 world_id TEXT NOT NULL, parent_id TEXT NOT NULL, parent_version INTEGER NOT NULL,
 child_id TEXT NOT NULL, child_version INTEGER NOT NULL, source_execution_id TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(parent_id,parent_version,child_id,child_version),
 CHECK(parent_id!=child_id),
 FOREIGN KEY(parent_id,parent_version,world_id) REFERENCES
technology_versions(technology_id,version,world_id),
 FOREIGN KEY(child_id,child_version,world_id) REFERENCES
technology_versions(technology_id,version,world_id),
 FOREIGN KEY(source_execution_id,world_id) REFERENCES technology_executions(id,world_id)
);
CREATE TABLE IF NOT EXISTS technology_evidence (
 world_id TEXT NOT NULL, technology_id TEXT NOT NULL, version INTEGER NOT NULL,
 observation_id TEXT NOT NULL, execution_id TEXT NOT NULL, role TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(technology_id,version,observation_id),
 FOREIGN KEY(technology_id,version,world_id) REFERENCES
technology_versions(technology_id,version,world_id),
 FOREIGN KEY(observation_id,world_id) REFERENCES collective_observations(id,world_id),
 FOREIGN KEY(execution_id,world_id) REFERENCES technology_executions(id,world_id)
);
CREATE TABLE IF NOT EXISTS technology_knowledge (
 world_id TEXT NOT NULL, actor_id TEXT NOT NULL, technology_id TEXT NOT NULL, version INTEGER
NOT NULL,
 source_actor_id TEXT, source_message_id TEXT, source_event_id TEXT REFERENCES world_events(id),
 evidence_ids_json TEXT NOT NULL DEFAULT '[]', learned_at TEXT NOT NULL,
 propagation_depth INTEGER NOT NULL DEFAULT 0 CHECK(propagation_depth>=0),
 PRIMARY KEY(actor_id,technology_id,version),
 FOREIGN KEY(actor_id,world_id) REFERENCES characters(id,world_id),
 FOREIGN KEY(source_actor_id,world_id) REFERENCES characters(id,world_id),
 FOREIGN KEY(technology_id,version,world_id) REFERENCES
technology_versions(technology_id,version,world_id)
);
CREATE TABLE IF NOT EXISTS science_claim_trials (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL, candidate_id TEXT NOT NULL, revision INTEGER NOT
NULL CHECK(revision>0),
 execution_id TEXT NOT NULL UNIQUE, control_execution_id TEXT NOT NULL,
 claim_json TEXT NOT NULL, registered_at TEXT NOT NULL, CHECK(execution_id!=control_execution_id),
 FOREIGN KEY(candidate_id,world_id) REFERENCES technology_candidates(id,world_id),
 FOREIGN KEY(execution_id,world_id) REFERENCES technology_executions(id,world_id),
 FOREIGN KEY(control_execution_id,world_id) REFERENCES technology_executions(id,world_id)
);
CREATE TABLE IF NOT EXISTS science_execution_sources (
 world_id TEXT NOT NULL, execution_id TEXT NOT NULL, source_execution_id TEXT NOT NULL,
 item_id TEXT NOT NULL DEFAULT '', ledger_id TEXT REFERENCES material_ledger(id), created_at
TEXT NOT NULL,
 PRIMARY KEY(execution_id,source_execution_id,item_id), CHECK(execution_id!=source_execution_id),
 FOREIGN KEY(execution_id,world_id) REFERENCES technology_executions(id,world_id),
 FOREIGN KEY(source_execution_id,world_id) REFERENCES technology_executions(id,world_id)
);
CREATE TABLE IF NOT EXISTS science_workshops (
 world_id TEXT PRIMARY KEY REFERENCES worlds(id), seed INTEGER NOT NULL,
 assets_json TEXT NOT NULL, rules_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS science_energy_stocks (
 item_id TEXT PRIMARY KEY REFERENCES item_instances(id) ON DELETE CASCADE,
 world_id TEXT NOT NULL REFERENCES worlds(id),
 energy_per_unit REAL NOT NULL CHECK(energy_per_unit>0),
 unit TEXT NOT NULL DEFAULT 'kJ'
);
CREATE TABLE IF NOT EXISTS science_reference_samples (
 item_id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
 family TEXT NOT NULL, origin_ledger_id TEXT NOT NULL REFERENCES material_ledger(id),
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS science_programs (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, world_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 spec_json TEXT NOT NULL, pc INTEGER NOT NULL DEFAULT 0, bindings_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'active'
 CHECK(status IN ('active','completed','rejected','unsupported')),
 created_at TEXT NOT NULL, completed_at TEXT, error TEXT, call_id TEXT, UNIQUE(id,world_id),
 FOREIGN KEY(session_id,world_id) REFERENCES science_sessions(id,world_id),
 FOREIGN KEY(actor_id,world_id) REFERENCES characters(id,world_id),
 FOREIGN KEY(call_id,world_id) REFERENCES science_model_calls(id,world_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS science_actor_program
 ON science_programs(actor_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS science_commands (
 program_id TEXT NOT NULL REFERENCES science_programs(id), command_index INTEGER NOT NULL,
 spec_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','running','completed','rejected','unsupported')),
 source_kind TEXT, source_id TEXT, result_json TEXT NOT NULL DEFAULT 'null',
 started_at TEXT, completed_at TEXT, error TEXT, PRIMARY KEY(program_id,command_index)
);
CREATE TABLE IF NOT EXISTS science_execution_work (
 execution_id TEXT NOT NULL REFERENCES technology_executions(id),
 order_id TEXT NOT NULL REFERENCES collective_work_orders(id),
 step_index INTEGER NOT NULL, PRIMARY KEY(execution_id,step_index), UNIQUE(order_id)
);
CREATE TABLE IF NOT EXISTS science_candidate_revisions (
 candidate_id TEXT NOT NULL, world_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 actor_id TEXT NOT NULL, spec_json TEXT NOT NULL, source_program_id TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(candidate_id,revision),
 FOREIGN KEY(candidate_id,world_id) REFERENCES technology_candidates(id,world_id),
 FOREIGN KEY(actor_id,world_id) REFERENCES characters(id,world_id)
);
CREATE TABLE IF NOT EXISTS science_method_spans (
 execution_id TEXT NOT NULL, world_id TEXT NOT NULL, technology_id TEXT NOT NULL,
 version INTEGER NOT NULL, start_step INTEGER NOT NULL, end_step INTEGER NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(execution_id,technology_id,version,start_step),
 FOREIGN KEY(execution_id,world_id) REFERENCES technology_executions(id,world_id),
 FOREIGN KEY(technology_id,version,world_id)
 REFERENCES technology_versions(technology_id,version,world_id)
);
CREATE TABLE IF NOT EXISTS science_version_states (
 technology_id TEXT NOT NULL, world_id TEXT NOT NULL, version INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('verified','needs_review','invalidated')),
 reason TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(technology_id,version),
 FOREIGN KEY(technology_id,version,world_id)
 REFERENCES technology_versions(technology_id,version,world_id)
);
CREATE TABLE IF NOT EXISTS science_chronicle_records (
 technology_id TEXT NOT NULL, world_id TEXT NOT NULL, version INTEGER NOT NULL,
 kind TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES world_events(id),
 process_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(technology_id,version,kind), UNIQUE(event_id),
 FOREIGN KEY(technology_id,version,world_id)
 REFERENCES technology_versions(technology_id,version,world_id)
);
CREATE TABLE IF NOT EXISTS science_invalidations (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL, technology_id TEXT NOT NULL, version INTEGER NOT NULL,
 actor_id TEXT NOT NULL, evidence_ids_json TEXT NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL,
 FOREIGN KEY(technology_id,version,world_id)
 REFERENCES technology_versions(technology_id,version,world_id),
 FOREIGN KEY(actor_id,world_id) REFERENCES characters(id,world_id)
);
CREATE TABLE IF NOT EXISTS science_runtime_seals (
 session_id TEXT NOT NULL REFERENCES science_sessions(id), sequence INTEGER NOT NULL,
 state_hash TEXT NOT NULL, previous_hash TEXT NOT NULL, chain_hash TEXT NOT NULL,
 world_time TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(session_id,sequence)
);
CREATE TRIGGER IF NOT EXISTS science_freeze_immutable
 BEFORE UPDATE OF freeze_json ON science_sessions WHEN OLD.freeze_json!='{}'
 BEGIN SELECT RAISE(ABORT,'science freeze is immutable'); END;
"""


def install_science_schema(c):
    c.executescript(SCIENCE_SCHEMA)
    # 哥哥，来源记录不可倒填；人物修订应写新版本，不改旧证据。
    for table in (
        "technology_versions",
        "technology_dependencies",
        "technology_evidence",
        "technology_knowledge",
        "science_claim_trials",
        "science_execution_sources",
        "science_candidate_revisions",
        "science_method_spans",
        "science_chronicle_records",
        "science_invalidations",
        "science_runtime_seals",
    ):
        for operation in ("UPDATE", "DELETE"):
            c.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                f"BEFORE {operation} ON {table} BEGIN "
                "SELECT RAISE(ABORT,'science provenance is immutable'); END;"
            )


def create_session(c, world_id, at, *, max_calls=12, token_limit=200000, max_output_tokens=8192):
    for value in (max_calls, token_limit, max_output_tokens):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("科研额度须为正整数")
    if max_output_tokens > token_limit:
        raise ValueError("单次输出预留不能超过总额度")
    if c.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone() is None:
        raise ValueError("科研世界不存在")
    if c.execute("SELECT 1 FROM science_sessions WHERE world_id=?", (world_id,)).fetchone():
        raise ValueError("世界已有科研会话，须恢复原会话而非清空账目")
    sid = str(uuid4())
    c.execute(
        "INSERT INTO science_sessions"
        "(id,world_id,max_calls,token_limit,max_output_tokens,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, world_id, max_calls, token_limit, max_output_tokens, to_iso(at)),
    )
    return sid
