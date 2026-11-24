"""Migration 50: operator-managed native deployments and direct control grants.

Application roles can read their workspace records but cannot install or alter
them. Owner bindings, revisions and registrations use explicit admin operations,
as existing Cog grants do. No existing sandbox row or execution gate changes.
"""

WORKSPACE_TABLES = (
    "remote_workspace_owners",
    "remote_nodes",
    "native_workspace_deployments",
    "remote_provisioning_attempts",
    "workspace_control_grant_revisions",
)
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    """CREATE TABLE remote_workspace_owners (
        workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
        principal_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(workspace_id,principal_id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE remote_nodes (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        owner_principal_id uuid NOT NULL,
        generation bigint NOT NULL CHECK(generation > 0),
        registration jsonb NOT NULL CHECK(jsonb_typeof(registration)='object'),
        registration_sha256 text NOT NULL CHECK(registration_sha256 ~ '^[0-9a-f]{64}$'),
        state text NOT NULL DEFAULT 'registered' CHECK(state='registered'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,owner_principal_id)
            REFERENCES remote_workspace_owners(workspace_id,principal_id)
    )""",
    """CREATE TABLE native_workspace_deployments (
        workspace_id uuid NOT NULL,
        revision bigint NOT NULL CHECK(revision > 0),
        owner_principal_id uuid NOT NULL,
        node_workspace_id uuid NOT NULL,
        node_id uuid NOT NULL,
        execution_mode text NOT NULL CHECK(execution_mode='base_os'),
        root_path text NOT NULL CHECK(length(root_path) BETWEEN 2 AND 4096),
        execution_account text NOT NULL CHECK(execution_account ~ '^potbot-w-[0-9a-f]{16}$'),
        environment jsonb NOT NULL CHECK(jsonb_typeof(environment)='object'
            AND COALESCE(environment->>'execution_mode','')='base_os'
            AND COALESCE(environment->>'schema','')='workspace.execution-environment/1'),
        environment_sha256 text NOT NULL CHECK(environment_sha256 ~ '^[0-9a-f]{64}$'),
        deployment_sha256 text NOT NULL CHECK(deployment_sha256 ~ '^[0-9a-f]{64}$'),
        qualification text NOT NULL DEFAULT 'unqualified' CHECK(qualification='unqualified'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,revision),
        FOREIGN KEY(workspace_id,owner_principal_id)
            REFERENCES remote_workspace_owners(workspace_id,principal_id),
        FOREIGN KEY(node_workspace_id,node_id) REFERENCES remote_nodes(workspace_id,id)
    )""",
    """CREATE TABLE remote_provisioning_attempts (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        node_id uuid NOT NULL,
        idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 128),
        manifest jsonb NOT NULL CHECK(jsonb_typeof(manifest)='object'),
        manifest_sha256 text NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
        state text NOT NULL DEFAULT 'planned' CHECK(state='planned'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,idempotency_key),
        FOREIGN KEY(workspace_id,node_id) REFERENCES remote_nodes(workspace_id,id)
    )""",
    """CREATE TABLE workspace_control_grant_revisions (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        revision bigint NOT NULL CHECK(revision > 0),
        issuer_principal_id uuid NOT NULL,
        source_workspace_id uuid NOT NULL REFERENCES workspaces(id),
        principal_id uuid NOT NULL,
        permissions text[] NOT NULL CHECK(cardinality(permissions) BETWEEN 1 AND 7
            AND permissions <@ ARRAY['workspace.inspect','tool.execute','model.invoke',
                'apps.launch','apps.view','apps.input','apps.terminate']::text[]),
        expires_at timestamptz NOT NULL,
        revocation_generation bigint NOT NULL CHECK(revocation_generation > 0),
        revoked boolean NOT NULL,
        delegation boolean NOT NULL DEFAULT false CHECK(NOT delegation),
        grant_sha256 text NOT NULL CHECK(grant_sha256 ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id,revision),
        CHECK(workspace_id <> source_workspace_id),
        FOREIGN KEY(workspace_id,issuer_principal_id)
            REFERENCES remote_workspace_owners(workspace_id,principal_id),
        FOREIGN KEY(source_workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    "CREATE INDEX control_grants_source ON workspace_control_grant_revisions(source_workspace_id)",
    *tuple(
        f"CREATE POLICY remote_operator_writes ON {table} AS RESTRICTIVE "
        "FOR ALL USING (true) WITH CHECK (false)"
        for table in WORKSPACE_TABLES
    ),
)
