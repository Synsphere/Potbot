"""Migration 49: R7.2 atomic DAG admission over the *existing* checked job owner.

A graph is only an ordering/lock authority. The actual invocation, lease,
usage, accounting, result and uncertain-outcome authority is still ``jobs``.
An inserted checked execution is linked to its leaf IN THE SAME TRANSACTION;
no application-side read/launch race or second worker queue is introduced.
"""

WORKSPACE_TABLES = ("atomic_graph_runs", "atomic_graph_nodes", "atomic_graph_resource_holds")
APPEND_ONLY_TABLES: tuple[str, ...] = ()

MIGRATION_STATEMENTS = (
    """CREATE TABLE atomic_graph_runs (
        workspace_id uuid NOT NULL REFERENCES workspaces(id),
        id uuid NOT NULL,
        principal_id uuid NOT NULL,
        objective text NOT NULL CHECK(octet_length(objective) BETWEEN 1 AND 32768),
        structure jsonb NOT NULL CHECK(jsonb_typeof(structure)='object' AND octet_length(structure::text)<=262144),
        manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
        max_parallel integer NOT NULL CHECK(max_parallel BETWEEN 1 AND 64),
        state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','interrupted')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        interrupted_at timestamptz,
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        CHECK((state='interrupted')=(interrupted_at IS NOT NULL))
    )""",
    """CREATE TABLE atomic_graph_nodes (
        workspace_id uuid NOT NULL,
        graph_id uuid NOT NULL,
        principal_id uuid NOT NULL,
        node_id text NOT NULL CHECK(octet_length(node_id) BETWEEN 1 AND 64),
        sort_order integer NOT NULL CHECK(sort_order BETWEEN 0 AND 1023),
        operation_name text NOT NULL CHECK(octet_length(operation_name) BETWEEN 1 AND 128),
        cog_ref text NOT NULL CHECK(octet_length(cog_ref) BETWEEN 1 AND 512),
        binding_hash text NOT NULL CHECK(binding_hash ~ '^[0-9a-f]{64}$'),
        input_fingerprint text NOT NULL CHECK(input_fingerprint ~ '^[0-9a-f]{64}$'),
        operation_id text NOT NULL CHECK(operation_id ~ '^[0-9a-f]{64}$'),
        check_job_id uuid NOT NULL,
        request jsonb NOT NULL CHECK(jsonb_typeof(request)='object'
            AND octet_length(request::text) <= 32768),
        dependencies text[] NOT NULL DEFAULT '{}' CHECK(cardinality(dependencies)<=16),
        lock_keys text[] NOT NULL DEFAULT '{}' CHECK(cardinality(lock_keys)<=32),
        job_id uuid,
        PRIMARY KEY(workspace_id,graph_id,node_id),
        UNIQUE(workspace_id,principal_id,check_job_id),
        UNIQUE(workspace_id,principal_id,job_id),
        FOREIGN KEY(workspace_id,graph_id) REFERENCES atomic_graph_runs(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id,check_job_id)
            REFERENCES jobs(workspace_id,owner_principal_id,id),
        FOREIGN KEY(workspace_id,principal_id,job_id)
            REFERENCES jobs(workspace_id,owner_principal_id,id)
    )""",
    """CREATE TABLE atomic_graph_resource_holds (
        workspace_id uuid NOT NULL,
        principal_id uuid NOT NULL,
        job_id uuid NOT NULL,
        lock_key text NOT NULL CHECK(octet_length(lock_key) BETWEEN 1 AND 128),
        active boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        released_at timestamptz,
        PRIMARY KEY(workspace_id,job_id,lock_key),
        FOREIGN KEY(workspace_id,principal_id,job_id)
            REFERENCES jobs(workspace_id,owner_principal_id,id),
        CHECK(active=(released_at IS NULL))
    )""",
    # The unique index works even when row-level security hides another owner's
    # row. This is a workspace-level lock, not an owner-visible lock table.
    "CREATE UNIQUE INDEX atomic_active_resource_hold ON atomic_graph_resource_holds(workspace_id,lock_key) WHERE active",
    """CREATE POLICY atomic_resource_principal ON atomic_graph_resource_holds AS RESTRICTIVE FOR ALL
        USING(principal_id=NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK(principal_id=NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE FUNCTION atomic_resource_protect() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE observed_state text;
    BEGIN
        IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
           OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
           OR NEW.job_id IS DISTINCT FROM OLD.job_id
           OR NEW.lock_key IS DISTINCT FROM OLD.lock_key
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR (OLD.active=false AND NEW.active=true)
           OR (OLD.released_at IS NOT NULL AND NEW.released_at IS DISTINCT FROM OLD.released_at)
        THEN RAISE EXCEPTION 'atomic resource reservation is immutable' USING ERRCODE='55000';
        END IF;
        IF OLD.active AND NOT NEW.active THEN
            SELECT state INTO observed_state FROM jobs
            WHERE workspace_id=NEW.workspace_id AND id=NEW.job_id
              AND owner_principal_id=NEW.principal_id;
            IF observed_state NOT IN ('succeeded','failed','cancelled')
               OR observed_state IS NULL THEN
                RAISE EXCEPTION 'atomic resource release needs verified terminal work'
                   USING ERRCODE='55000';
            END IF;
        END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER atomic_resource_protect BEFORE UPDATE ON atomic_graph_resource_holds FOR EACH ROW EXECUTE FUNCTION atomic_resource_protect()",
    "CREATE INDEX atomic_graph_job_lookup ON atomic_graph_nodes(workspace_id,principal_id,job_id) WHERE job_id IS NOT NULL",
    "CREATE INDEX atomic_graph_owner_history ON atomic_graph_runs(workspace_id,principal_id,created_at,id)",
    # Workspace isolation from Database._apply_security is supplemented with
    # strict principal separation. The foreign keys must not grant cross-owner
    # job read/write privileges.
    """CREATE POLICY atomic_graph_principal ON atomic_graph_runs AS RESTRICTIVE FOR ALL
        USING(principal_id=NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK(principal_id=NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE POLICY atomic_node_principal ON atomic_graph_nodes AS RESTRICTIVE FOR ALL
        USING(principal_id=NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK(principal_id=NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE FUNCTION atomic_graph_protect() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
           OR NEW.id IS DISTINCT FROM OLD.id
           OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
           OR NEW.objective IS DISTINCT FROM OLD.objective
           OR NEW.structure IS DISTINCT FROM OLD.structure
           OR NEW.manifest_hash IS DISTINCT FROM OLD.manifest_hash
           OR NEW.max_parallel IS DISTINCT FROM OLD.max_parallel
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR (NEW.state IS DISTINCT FROM OLD.state
               AND NOT (OLD.state='active' AND NEW.state='interrupted'))
           OR (OLD.interrupted_at IS NOT NULL
               AND NEW.interrupted_at IS DISTINCT FROM OLD.interrupted_at)
        THEN RAISE EXCEPTION 'atomic graph identity is immutable' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER atomic_graph_protect BEFORE UPDATE ON atomic_graph_runs FOR EACH ROW EXECUTE FUNCTION atomic_graph_protect()",
    """CREATE FUNCTION atomic_node_protect() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE linked record;
    BEGIN
        IF (NEW.workspace_id,NEW.graph_id,NEW.principal_id,NEW.node_id,
            NEW.sort_order,NEW.operation_name,NEW.cog_ref,NEW.binding_hash,
            NEW.input_fingerprint,NEW.operation_id,NEW.check_job_id,
            NEW.request,NEW.dependencies,NEW.lock_keys)
           IS DISTINCT FROM
           (OLD.workspace_id,OLD.graph_id,OLD.principal_id,OLD.node_id,
            OLD.sort_order,OLD.operation_name,OLD.cog_ref,OLD.binding_hash,
            OLD.input_fingerprint,OLD.operation_id,OLD.check_job_id,
            OLD.request,OLD.dependencies,OLD.lock_keys)
           OR (OLD.job_id IS NOT NULL AND NEW.job_id IS DISTINCT FROM OLD.job_id)
        THEN RAISE EXCEPTION 'atomic leaf contract is immutable' USING ERRCODE='55000';
        END IF;
        IF NEW.job_id IS DISTINCT FROM OLD.job_id THEN
            SELECT id,operation_check_id INTO linked FROM jobs
            WHERE workspace_id=NEW.workspace_id
              AND owner_principal_id=NEW.principal_id AND id=NEW.job_id;
            IF NOT FOUND OR linked.operation_check_id IS DISTINCT FROM NEW.check_job_id THEN
                RAISE EXCEPTION 'atomic leaf job/check link mismatch' USING ERRCODE='55000';
            END IF;
        END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER atomic_node_protect BEFORE UPDATE ON atomic_graph_nodes FOR EACH ROW EXECUTE FUNCTION atomic_node_protect()",
    # The OperationWorkbench.launch -> JobService.submit transaction already
    # owns the signed check and exact native request. This trigger serializes
    # independent launches under one graph lock and attaches the new job before
    # the INSERT can commit and become visible to workers.
    """CREATE FUNCTION atomic_graph_admit_job() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE leaf atomic_graph_nodes%ROWTYPE;
    graph atomic_graph_runs%ROWTYPE;
    active_count integer;
    BEGIN
        IF NEW.operation_check_id IS NULL THEN RETURN NEW; END IF;
        SELECT * INTO leaf FROM atomic_graph_nodes n
            WHERE n.workspace_id=NEW.workspace_id
              AND n.principal_id=NEW.owner_principal_id
              AND n.check_job_id=NEW.operation_check_id;
        IF NOT FOUND THEN RETURN NEW; END IF;
        SELECT * INTO graph FROM atomic_graph_runs g
            WHERE g.workspace_id=leaf.workspace_id AND g.id=leaf.graph_id
              AND g.principal_id=leaf.principal_id FOR UPDATE;
        IF graph.state <> 'active' OR leaf.job_id IS NOT NULL THEN
            RAISE EXCEPTION 'atomic graph is stopped or leaf already scheduled' USING ERRCODE='55000';
        END IF;
        IF NEW.job_type <> 'cog.execute'
           OR NEW.episode_id::text <> leaf.request->>'episode_id'
           OR NEW.job_type <> leaf.request->>'job_type'
           OR NEW.input IS DISTINCT FROM leaf.request->'input'
           OR NEW.selected_models IS DISTINCT FROM leaf.request->'selected_models' THEN
            RAISE EXCEPTION 'atomic launch differs from checked leaf' USING ERRCODE='55000';
        END IF;
        IF EXISTS (
            SELECT 1 FROM unnest(leaf.dependencies) AS d(node_id)
            LEFT JOIN atomic_graph_nodes p ON p.workspace_id=leaf.workspace_id
                AND p.graph_id=leaf.graph_id AND p.node_id=d.node_id
            LEFT JOIN jobs j ON j.workspace_id=p.workspace_id AND j.id=p.job_id
            WHERE p.job_id IS NULL OR j.state IS DISTINCT FROM 'succeeded'
        ) THEN
            RAISE EXCEPTION 'atomic prerequisites are not verified' USING ERRCODE='55000';
        END IF;
        SELECT count(*) INTO active_count
          FROM atomic_graph_nodes n JOIN jobs j
            ON j.workspace_id=n.workspace_id AND j.id=n.job_id
          WHERE n.workspace_id=leaf.workspace_id AND n.graph_id=leaf.graph_id
            AND j.state IN ('queued','running','cancelling');
        IF active_count >= graph.max_parallel THEN
            RAISE EXCEPTION 'atomic graph parallelism bound reached' USING ERRCODE='55000';
        END IF;
        IF EXISTS (
          SELECT 1 FROM atomic_graph_nodes n JOIN jobs j
            ON j.workspace_id=n.workspace_id AND j.id=n.job_id
          WHERE n.workspace_id=leaf.workspace_id AND n.graph_id=leaf.graph_id
            AND j.state IN ('queued','running','cancelling')
            AND n.lock_keys && leaf.lock_keys
        ) THEN
            RAISE EXCEPTION 'atomic resource lock is held' USING ERRCODE='55000';
        END IF;
        -- These are globally unique per workspace, including other principals.
        -- Sorted acquisition prevents multi-lock insert deadlocks. A conflict
        -- rejects the entire job INSERT/check receipt in the same transaction.
        INSERT INTO atomic_graph_resource_holds(workspace_id,principal_id,job_id,lock_key)
        SELECT leaf.workspace_id,leaf.principal_id,NEW.id,k
          FROM unnest(leaf.lock_keys) AS k ORDER BY k;
        UPDATE atomic_graph_nodes SET job_id=NEW.id
            WHERE workspace_id=leaf.workspace_id AND graph_id=leaf.graph_id
              AND node_id=leaf.node_id AND job_id IS NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'atomic launch was already claimed' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER atomic_graph_job_admission AFTER INSERT ON jobs FOR EACH ROW EXECUTE FUNCTION atomic_graph_admit_job()",
    """CREATE FUNCTION atomic_resource_release() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
       -- Unknown effect status is deliberately NOT a release receipt.
       IF NEW.state IN ('succeeded','failed','cancelled')
          AND OLD.state IS DISTINCT FROM NEW.state THEN
          UPDATE atomic_graph_resource_holds
          SET active=false,released_at=clock_timestamp()
          WHERE workspace_id=NEW.workspace_id AND principal_id=NEW.owner_principal_id
            AND job_id=NEW.id AND active;
       END IF;
       RETURN NEW;
    END $$""",
    "CREATE TRIGGER atomic_resource_release AFTER UPDATE OF state ON jobs FOR EACH ROW EXECUTE FUNCTION atomic_resource_release()",
    # An interrupt commits before any later affected mark_dispatched. A worker
    # that was already dispatched retains its possible-effect/usage ledger.
    """CREATE FUNCTION atomic_graph_dispatch_fence() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE graph_state text;
    BEGIN
      IF OLD.dispatched_at IS NULL AND NEW.dispatched_at IS NOT NULL THEN
          SELECT g.state INTO graph_state
          FROM atomic_graph_nodes n JOIN atomic_graph_runs g
            ON g.workspace_id=n.workspace_id AND g.id=n.graph_id
          WHERE n.workspace_id=NEW.workspace_id
            AND n.principal_id=NEW.owner_principal_id AND n.job_id=NEW.id
          FOR SHARE OF g;
          IF FOUND AND graph_state <> 'active' THEN
              RAISE EXCEPTION 'interrupted atomic graph cannot dispatch' USING ERRCODE='55000';
          END IF;
      END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER atomic_graph_dispatch_fence BEFORE UPDATE OF dispatched_at ON jobs FOR EACH ROW EXECUTE FUNCTION atomic_graph_dispatch_fence()",
)
