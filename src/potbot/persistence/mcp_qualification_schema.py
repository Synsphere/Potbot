"""Migration 44: signed qualification cases over existing MCP execution owners."""

WORKSPACE_TABLES = (
    "mcp_qualification_suites",
    "mcp_qualification_attempts",
    "mcp_qualification_reports",
    "mcp_qualification_events",
)
APPEND_ONLY_TABLES = ("mcp_qualification_reports", "mcp_qualification_events")
_PRINCIPAL = "NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid"

MIGRATION_STATEMENTS = (
    """CREATE TABLE mcp_qualification_suites (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        binding_id uuid NOT NULL,
        config jsonb NOT NULL CHECK(jsonb_typeof(config)='object' AND octet_length(config::text)<=65536),
        revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740990),
        state text NOT NULL CHECK(state IN ('enabled','disabled','revoked')),
        active_attempt_id uuid, last_attempt_id uuid, last_report_id uuid,
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,binding_id) REFERENCES mcp_discovery_bindings(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE mcp_qualification_attempts (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        suite_id uuid NOT NULL,
        revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740990),
        state text NOT NULL CHECK(state IN ('requested','running','recovering','succeeded','failed','cancelled','indeterminate')),
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=262144),
        worker_token uuid,
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,suite_id) REFERENCES mcp_qualification_suites(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id),
        CHECK((state IN ('running','recovering'))=(worker_token IS NOT NULL))
    )""",
    """CREATE TABLE mcp_qualification_reports (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        suite_id uuid NOT NULL, attempt_id uuid NOT NULL,
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=262144),
        sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,attempt_id),
        FOREIGN KEY(workspace_id,suite_id) REFERENCES mcp_qualification_suites(workspace_id,id),
        FOREIGN KEY(workspace_id,attempt_id) REFERENCES mcp_qualification_attempts(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    "ALTER TABLE mcp_qualification_suites ADD CONSTRAINT mcp_qualification_active_attempt "
    "FOREIGN KEY(workspace_id,active_attempt_id) REFERENCES mcp_qualification_attempts(workspace_id,id)",
    "ALTER TABLE mcp_qualification_suites ADD CONSTRAINT mcp_qualification_last_attempt "
    "FOREIGN KEY(workspace_id,last_attempt_id) REFERENCES mcp_qualification_attempts(workspace_id,id)",
    "ALTER TABLE mcp_qualification_suites ADD CONSTRAINT mcp_qualification_last_report "
    "FOREIGN KEY(workspace_id,last_report_id) REFERENCES mcp_qualification_reports(workspace_id,id)",
    """CREATE TABLE mcp_qualification_events (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        subject_kind text NOT NULL CHECK(subject_kind IN ('suite','attempt')),
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
    "CREATE INDEX mcp_qualification_event_head ON mcp_qualification_events(workspace_id,subject_kind,subject_id,revision DESC)",
    "CREATE INDEX mcp_qualification_attempt_suite ON mcp_qualification_attempts(workspace_id,principal_id,suite_id,id)",
    "CREATE INDEX mcp_qualification_suite_binding ON mcp_qualification_suites(workspace_id,principal_id,binding_id,id)",
)
