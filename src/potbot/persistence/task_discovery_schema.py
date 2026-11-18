"""Migration 45: private task-bound shortlists over existing capability owners."""

WORKSPACE_TABLES = (
    "task_discovery_searches",
    "task_discovery_results",
    "task_discovery_selections",
    "task_discovery_events",
)
APPEND_ONLY_TABLES = WORKSPACE_TABLES[1:]
_PRINCIPAL = "NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid"

MIGRATION_STATEMENTS = (
    # The existing generic receipt owner is workspace-scoped. These new
    # commands carry private queries and native claim identities; narrow only
    # their namespace without changing historical task-command visibility.
    f"CREATE POLICY command_results_task_discovery_owner ON command_results AS RESTRICTIVE FOR ALL "
    f"USING(operation NOT LIKE 'task.discovery.%' OR principal_id={_PRINCIPAL}) "
    f"WITH CHECK(operation NOT LIKE 'task.discovery.%' OR principal_id={_PRINCIPAL})",
    """CREATE TABLE task_discovery_searches (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        episode_id uuid NOT NULL,
        revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740990),
        status text NOT NULL CHECK(status IN ('requested','running','completed','selected','failed','cancelled','indeterminate')),
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=2097152),
        worker_token uuid,
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,episode_id) REFERENCES episodes(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id),
        CHECK((status='running')=(worker_token IS NOT NULL))
    )""",
    """CREATE TABLE task_discovery_results (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        search_id uuid NOT NULL,
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=1048576),
        sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,search_id),
        FOREIGN KEY(workspace_id,search_id) REFERENCES task_discovery_searches(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE task_discovery_selections (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        search_id uuid NOT NULL, result_id uuid NOT NULL,
        candidate_id text NOT NULL CHECK(candidate_id ~ '^[a-f0-9]{64}$'),
        payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=65536),
        sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
        mac text NOT NULL CHECK(mac ~ '^[a-f0-9]{64}$'),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,search_id),
        FOREIGN KEY(workspace_id,search_id) REFERENCES task_discovery_searches(workspace_id,id),
        FOREIGN KEY(workspace_id,result_id) REFERENCES task_discovery_results(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE task_discovery_events (
        workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
        search_id uuid NOT NULL, revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740990),
        action text NOT NULL CHECK(action ~ '^[a-z][a-z0-9_]{0,63}$'),
        binding_sha256 text NOT NULL CHECK(binding_sha256 ~ '^[a-f0-9]{64}$'),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,search_id,revision),
        FOREIGN KEY(workspace_id,search_id) REFERENCES task_discovery_searches(workspace_id,id),
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
    "CREATE INDEX task_discovery_task_search ON task_discovery_searches(workspace_id,principal_id,episode_id,id)",
    "CREATE INDEX task_discovery_event_head ON task_discovery_events(workspace_id,principal_id,search_id,revision DESC)",
)
