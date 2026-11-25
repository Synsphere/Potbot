"""Migration 59: live workspace overrides and protected node-service control."""

WORKSPACE_TABLES = (
    "workspace_configuration_revisions",
    "node_runtime_controls",
    "node_job_admissions",
    "node_control_events",
)
APPEND_ONLY_TABLES = (
    "workspace_configuration_revisions",
    "node_job_admissions",
    "node_control_events",
)

MIGRATION_STATEMENTS = (
    """CREATE TABLE workspace_configuration_revisions (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
        predecessor_revision bigint,
        principal_id uuid NOT NULL,
        request_id uuid NOT NULL,
        overrides jsonb NOT NULL CHECK(jsonb_typeof(overrides)='object'
            AND octet_length(overrides::text)<=32768
            AND (overrides - ARRAY['policy_versions','reasoning_models','coding_models',
                'allowed_tools','physical_inference_concurrency','max_packet_tokens',
                'verification_policy','embedding_profile']::text[])='{}'::jsonb),
        overrides_sha256 text NOT NULL CHECK(overrides_sha256 ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,revision),
        UNIQUE(workspace_id,request_id),
        CHECK(revision=COALESCE(predecessor_revision,0)+1),
        FOREIGN KEY(workspace_id,predecessor_revision)
            REFERENCES workspace_configuration_revisions(workspace_id,revision),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES remote_workspace_owners(workspace_id,principal_id)
    )""",
    """CREATE POLICY configuration_owner ON workspace_configuration_revisions
        AS RESTRICTIVE FOR INSERT WITH CHECK (
        principal_id=NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM remote_workspace_owners o
            WHERE o.workspace_id=workspace_configuration_revisions.workspace_id
            AND o.principal_id=workspace_configuration_revisions.principal_id))""",
    """CREATE POLICY live_review_principal ON command_results AS RESTRICTIVE
        FOR INSERT WITH CHECK (
            operation NOT IN ('save_live_configuration','node_service_control/v1')
            OR principal_id=NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid
        )""",
    """CREATE TABLE node_runtime_controls (
        workspace_id uuid NOT NULL,
        node_id uuid NOT NULL,
        principal_id uuid NOT NULL,
        registration_generation bigint NOT NULL CHECK(registration_generation>0),
        revision bigint NOT NULL CHECK(revision>0),
        desired text NOT NULL CHECK(desired IN ('active','drain','restart')),
        phase text NOT NULL CHECK(phase IN
            ('starting','ready','draining','drained','restarting','indeterminate','stopped')),
        incarnation uuid NOT NULL,
        worker_id text NOT NULL CHECK(worker_id ~ '^potbot-node-[0-9a-f]{32}-[0-9a-f]{32}$'),
        profile_sha256 text NOT NULL CHECK(profile_sha256 ~ '^[0-9a-f]{64}$'),
        heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        request_id uuid,
        PRIMARY KEY(workspace_id,node_id),
        UNIQUE(workspace_id,worker_id),
        FOREIGN KEY(workspace_id,node_id) REFERENCES remote_nodes(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES remote_workspace_owners(workspace_id,principal_id)
    )""",
    """CREATE TABLE node_job_admissions (
        workspace_id uuid NOT NULL,
        node_id uuid NOT NULL,
        incarnation uuid NOT NULL,
        job_id uuid NOT NULL,
        generation bigint NOT NULL CHECK(generation>0),
        worker_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,job_id,generation),
        FOREIGN KEY(workspace_id,node_id) REFERENCES remote_nodes(workspace_id,id),
        FOREIGN KEY(workspace_id,job_id) REFERENCES jobs(workspace_id,id)
    )""",
    """CREATE TABLE node_control_events (
        workspace_id uuid NOT NULL,
        node_id uuid NOT NULL,
        request_id uuid NOT NULL,
        stage text NOT NULL CHECK(stage IN ('accepted','applied','rejected','indeterminate')),
        reason text NOT NULL,
        incarnation uuid NOT NULL,
        revision bigint NOT NULL CHECK(revision>0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,request_id,stage),
        FOREIGN KEY(workspace_id,node_id) REFERENCES remote_nodes(workspace_id,id)
    )""",
    *tuple(
        f"CREATE POLICY native_control_writes ON {table} AS RESTRICTIVE "
        "FOR ALL USING (true) WITH CHECK (false)"
        for table in ("node_runtime_controls", "node_control_events")
    ),
    # WITH CHECK protects INSERT/UPDATE; DELETE needs its own false USING.
    # Keep SELECT/row-lock visibility for the restricted claim/review paths.
    *tuple(
        f"CREATE POLICY native_control_delete ON {table} AS RESTRICTIVE FOR DELETE USING (false)"
        for table in ("node_runtime_controls", "node_control_events")
    ),
    # The trigger uses the trusted trigger-table schema with pg_catalog as its
    # search path. Temporary tables cannot shadow the protected runtime owner.
    """CREATE FUNCTION node_claim_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        DECLARE runtime record; pending boolean;
        BEGIN
            IF NEW.lease_owner IS NULL OR NEW.lease_owner NOT LIKE 'potbot-node-%'
               OR NEW.state<>'running' THEN RETURN NEW; END IF;
            IF TG_OP='UPDATE' AND OLD.state IN ('running','cancelling')
               AND OLD.generation=NEW.generation AND OLD.lease_owner=NEW.lease_owner
               THEN RETURN NEW; END IF;
            EXECUTE format('SELECT * FROM %I.node_runtime_controls
                WHERE workspace_id=$1 AND worker_id=$2',TG_TABLE_SCHEMA)
                INTO runtime USING NEW.workspace_id,NEW.lease_owner;
            IF runtime.node_id IS NOT NULL THEN
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'node-control:'||NEW.workspace_id::text||':'||runtime.node_id::text,0));
            END IF;
            EXECUTE format('SELECT * FROM %I.node_runtime_controls
                WHERE workspace_id=$1 AND worker_id=$2 FOR SHARE',TG_TABLE_SCHEMA)
                INTO runtime USING NEW.workspace_id,NEW.lease_owner;
            IF runtime.node_id IS NULL OR runtime.phase<>'ready' OR runtime.desired<>'active'
               OR runtime.principal_id IS DISTINCT FROM NEW.owner_principal_id
               OR runtime.heartbeat_at < clock_timestamp()-interval '15 seconds'
            THEN RAISE EXCEPTION 'native node admission is fenced' USING ERRCODE='55000'; END IF;
            EXECUTE format('SELECT EXISTS(SELECT 1 FROM %I.command_results c
                WHERE c.workspace_id=$1 AND c.principal_id=$2
                AND c.operation=''node_service_control/v1''
                AND c.result->''review''->>''nodeId''=$3
                AND NOT EXISTS(SELECT 1 FROM %I.node_control_events e
                    WHERE e.workspace_id=c.workspace_id
                    AND e.request_id::text=c.idempotency_key))',TG_TABLE_SCHEMA,TG_TABLE_SCHEMA)
                INTO pending USING NEW.workspace_id,runtime.principal_id,runtime.node_id::text;
            IF pending THEN RAISE EXCEPTION 'native node review is pending'
                USING ERRCODE='55000'; END IF;
            EXECUTE format('INSERT INTO %I.node_job_admissions
                (workspace_id,node_id,incarnation,job_id,generation,worker_id)
                VALUES($1,$2,$3,$4,$5,$6)',TG_TABLE_SCHEMA)
                USING NEW.workspace_id,runtime.node_id,runtime.incarnation,
                    NEW.id,NEW.generation,NEW.lease_owner;
            RETURN NEW;
        END $$""",
    # AFTER ensures the job row and its generation are visible to the receipt's FK.
    "CREATE TRIGGER native_node_admission AFTER INSERT OR UPDATE ON jobs "
    "FOR EACH ROW EXECUTE FUNCTION node_claim_guard()",
    """CREATE FUNCTION node_admission_receipt_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        DECLARE matching boolean;
        BEGIN
            EXECUTE format('SELECT EXISTS(SELECT 1 FROM %I.jobs j
                JOIN %I.node_runtime_controls c ON c.workspace_id=j.workspace_id
                  AND c.worker_id=j.lease_owner AND c.principal_id=j.owner_principal_id
                WHERE j.workspace_id=$1 AND j.id=$2 AND j.generation=$3
                  AND c.node_id=$4 AND c.incarnation=$5 AND c.worker_id=$6
                  AND j.state=''running'' AND c.phase=''ready'' AND c.desired=''active'')',
                TG_TABLE_SCHEMA,TG_TABLE_SCHEMA) INTO matching USING NEW.workspace_id,
                NEW.job_id,NEW.generation,NEW.node_id,NEW.incarnation,NEW.worker_id;
            IF NOT matching THEN RAISE EXCEPTION 'node admission receipt lacks a matching lease'
                USING ERRCODE='55000'; END IF;
            RETURN NEW;
        END $$""",
    "CREATE TRIGGER native_node_receipt BEFORE INSERT ON node_job_admissions "
    "FOR EACH ROW EXECUTE FUNCTION node_admission_receipt_guard()",
    "CREATE INDEX node_admission_runtime ON node_job_admissions(workspace_id,node_id,incarnation)",
)
