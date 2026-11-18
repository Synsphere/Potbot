"""Migration43: signed observations of existing installed MCP owners.

These records contain catalogue evidence and its admission. They never replace
the existing installed tool descriptors, task grants, gateway or scheduler.
"""

WORKSPACE_TABLES = (
    "mcp_discovery_bindings",
    "mcp_discovery_attempts",
    "mcp_discovery_snapshots",
    "mcp_discovery_events",
)
APPEND_ONLY_TABLES = ("mcp_discovery_snapshots", "mcp_discovery_events")
_PRINCIPAL = "NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid"

MIGRATION_STATEMENTS = (
    """CREATE TABLE mcp_discovery_bindings (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        config jsonb NOT NULL CHECK(jsonb_typeof(config)='object' AND octet_length(config::text)<=65536),
        revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740990),
        state text NOT NULL CHECK(state IN ('enabled','disabled','revoked')),
        snapshot_id uuid, active_attempt_id uuid, last_attempt_id uuid,
        last_error_code text CHECK(last_error_code ~ '^[a-z][a-z0-9_]{0,95}$'),
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE mcp_discovery_attempts (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        binding_id uuid NOT NULL, agent_id uuid NOT NULL,
        revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740990),
        state text NOT NULL CHECK(state IN ('requested','running','succeeded','failed','cancelled','indeterminate')),
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=131072),
        worker_token uuid,
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,binding_id) REFERENCES mcp_discovery_bindings(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id),
        CHECK((state='running')=(worker_token IS NOT NULL))
    )""",
    """CREATE TABLE mcp_discovery_snapshots (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        binding_id uuid NOT NULL, attempt_id uuid NOT NULL,
        config_sha256 text NOT NULL CHECK(config_sha256 ~ '^[a-f0-9]{64}$'),
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=3145728),
        observation jsonb NOT NULL CHECK(jsonb_typeof(observation)='object' AND octet_length(observation::text)<=65536),
        sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        fetched_at timestamptz NOT NULL, expires_at timestamptz NOT NULL CHECK(expires_at>=fetched_at),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,binding_id) REFERENCES mcp_discovery_bindings(workspace_id,id),
        FOREIGN KEY(workspace_id,attempt_id) REFERENCES mcp_discovery_attempts(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    "ALTER TABLE mcp_discovery_bindings ADD CONSTRAINT mcp_discovery_current_snapshot "
    "FOREIGN KEY(workspace_id,snapshot_id) REFERENCES mcp_discovery_snapshots(workspace_id,id)",
    "ALTER TABLE mcp_discovery_bindings ADD CONSTRAINT mcp_discovery_active_attempt "
    "FOREIGN KEY(workspace_id,active_attempt_id) REFERENCES mcp_discovery_attempts(workspace_id,id)",
    "ALTER TABLE mcp_discovery_bindings ADD CONSTRAINT mcp_discovery_last_attempt "
    "FOREIGN KEY(workspace_id,last_attempt_id) REFERENCES mcp_discovery_attempts(workspace_id,id)",
    """CREATE TABLE mcp_discovery_events (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        subject_kind text NOT NULL CHECK(subject_kind IN ('binding','attempt')),
        subject_id uuid NOT NULL, revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740990),
        action text NOT NULL CHECK(action ~ '^[a-z][a-z0-9_]{0,95}$'),
        binding_sha256 text NOT NULL CHECK(binding_sha256 ~ '^[a-f0-9]{64}$'),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,subject_kind,subject_id,revision),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    *(
        f"CREATE POLICY {table}_owner ON {table} AS RESTRICTIVE FOR ALL "
        f"USING(principal_id={_PRINCIPAL}) WITH CHECK(principal_id={_PRINCIPAL})"
        for table in WORKSPACE_TABLES
    ),
    *(
        f"CREATE INDEX {table}_scope ON {table}(workspace_id,principal_id,id)"
        for table in WORKSPACE_TABLES
    ),
    "CREATE INDEX mcp_discovery_event_head ON mcp_discovery_events(workspace_id,subject_kind,subject_id,revision DESC)",
    "CREATE INDEX mcp_discovery_attempt_binding ON mcp_discovery_attempts(workspace_id,principal_id,binding_id,id)",
)
