"""Migration42: scoped discovery observations and signed operator reviews.

These tables retain source evidence and decisions. Installed skills, tools,
release state, execution grants and scheduling keep their existing owners.
"""

WORKSPACE_TABLES = (
    "discovery_sources",
    "discovery_publishers",
    "discovery_snapshots",
    "discovery_checks",
    "discovery_events",
)
APPEND_ONLY_TABLES = ("discovery_snapshots", "discovery_events")
_PRINCIPAL = "NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid"

MIGRATION_STATEMENTS = (
    """CREATE TABLE discovery_sources (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        config jsonb NOT NULL CHECK(jsonb_typeof(config)='object'),
        revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740990),
        state text NOT NULL CHECK(state IN ('active','disabled','revoked')),
        snapshot_id uuid, query text NOT NULL DEFAULT '' CHECK(length(query)<=500),
        last_error_code text CHECK(last_error_code ~ '^[a-z][a-z0-9_]{0,95}$'),
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE discovery_publishers (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        source_id uuid NOT NULL, name text NOT NULL CHECK(length(name) BETWEEN 1 AND 256),
        evidence text NOT NULL CHECK(evidence IN ('registry_claim','source_owner_claim','operator_claim')),
        revision bigint NOT NULL CHECK(revision BETWEEN 0 AND 9007199254740990),
        state text NOT NULL CHECK(state IN ('unreviewed','approved','blocked','revoked')),
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,source_id,name),
        FOREIGN KEY(workspace_id,source_id) REFERENCES discovery_sources(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE discovery_snapshots (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        source_id uuid NOT NULL, config_sha256 text NOT NULL CHECK(config_sha256 ~ '^[a-f0-9]{64}$'),
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=3145728),
        sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        fetched_at timestamptz NOT NULL, expires_at timestamptz NOT NULL CHECK(expires_at>fetched_at),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,source_id) REFERENCES discovery_sources(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    "ALTER TABLE discovery_sources ADD CONSTRAINT discovery_current_snapshot "
    "FOREIGN KEY(workspace_id,snapshot_id) REFERENCES discovery_snapshots(workspace_id,id)",
    """CREATE TABLE discovery_checks (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        source_id uuid NOT NULL, publisher_id uuid NOT NULL, snapshot_id uuid NOT NULL,
        agent_id uuid NOT NULL,
        revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740990),
        state text NOT NULL CHECK(state IN ('awaiting_measurement','measuring','measured','failed','handed_off','cancelled')),
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=1572864),
        worker_token uuid,
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,source_id) REFERENCES discovery_sources(workspace_id,id),
        FOREIGN KEY(workspace_id,publisher_id) REFERENCES discovery_publishers(workspace_id,id),
        FOREIGN KEY(workspace_id,snapshot_id) REFERENCES discovery_snapshots(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id),
        CHECK((state='measuring')=(worker_token IS NOT NULL))
    )""",
    """CREATE TABLE discovery_events (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        subject_kind text NOT NULL CHECK(subject_kind IN ('source','publisher','check')),
        subject_id uuid NOT NULL, revision bigint NOT NULL CHECK(revision BETWEEN 0 AND 9007199254740990),
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
    "CREATE INDEX discovery_event_head ON discovery_events(workspace_id,subject_kind,subject_id,revision DESC)",
)
