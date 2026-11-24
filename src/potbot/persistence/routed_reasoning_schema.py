"""Migration 54: three durable stages scheduled through the existing route jobs."""

WORKSPACE_TABLES = ("routed_reasoning_runs",)
APPEND_ONLY_TABLES: tuple[str, ...] = ()

MIGRATION_STATEMENTS = (
    """CREATE TABLE routed_reasoning_runs (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        principal_id uuid NOT NULL,
        episode_id uuid NOT NULL,
        contract_revision_id uuid NOT NULL,
        plan jsonb NOT NULL CHECK (jsonb_typeof(plan)='object' AND octet_length(plan::text)<=131072),
        plan_sha256 text NOT NULL CHECK (plan_sha256 ~ '^[a-f0-9]{64}$'),
        stage_index integer NOT NULL DEFAULT 0 CHECK (stage_index BETWEEN 0 AND 3),
        results jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
            jsonb_typeof(results)='array' AND jsonb_array_length(results)=stage_index
        ),
        state text NOT NULL DEFAULT 'active' CHECK (
            state IN ('active','completed','blocked','interrupted')
            AND (state='completed')=(stage_index=3)
        ),
        error_code text CHECK (error_code ~ '^[a-z][a-z0-9_]{0,63}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace_id,id),
        FOREIGN KEY (workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id),
        FOREIGN KEY (workspace_id,episode_id,contract_revision_id)
            REFERENCES contract_revisions(workspace_id,episode_id,id),
        CHECK (state NOT IN ('active','completed') OR error_code IS NULL),
        CHECK (state<>'blocked' OR error_code IS NOT NULL)
    )""",
    """CREATE POLICY routed_reasoning_owner ON routed_reasoning_runs AS RESTRICTIVE FOR ALL
        USING (principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK (principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE INDEX routed_reasoning_owner_history
        ON routed_reasoning_runs(workspace_id,principal_id,created_at,id)""",
    """CREATE FUNCTION routed_reasoning_task_current(r routed_reasoning_runs)
        RETURNS boolean LANGUAGE sql AS $$
        SELECT EXISTS (SELECT 1 FROM episodes e
          WHERE e.workspace_id=r.workspace_id AND e.id=r.episode_id AND e.status='running'
            AND e.active_contract_id=r.contract_revision_id)
    $$""",
    """CREATE FUNCTION routed_reasoning_payload(request jsonb)
        RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
        SELECT CASE request->>'job_type'
          WHEN 'model.generate' THEN request #- '{input,prompt}' #- '{input,system}'
          WHEN 'cog.execute' THEN request #- '{input,value,prompt}' #- '{input,value,system}'
        END
    $$""",
    # The route transaction already holds its policy lock before it enters
    # this guard. Interrupt and result advancement lock only the run and read
    # job/decision records without locks.
    """CREATE FUNCTION routed_reasoning_decision_current(d model_route_decisions)
        RETURNS routed_reasoning_runs LANGUAGE plpgsql AS $$
    DECLARE r routed_reasoning_runs%ROWTYPE; stage jsonb; phase integer;
    BEGIN
        IF d.allocation_key NOT LIKE 'rrun-v1:%' THEN RETURN NULL; END IF;
        IF d.allocation_key !~ '^rrun-v1:[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}:[0-2]$'
        THEN RAISE EXCEPTION 'routed reasoning stage key is invalid' USING ERRCODE='55000'; END IF;
        phase:=split_part(d.allocation_key,':',3)::integer;
        SELECT * INTO r FROM routed_reasoning_runs
          WHERE workspace_id=d.workspace_id AND id=split_part(d.allocation_key,':',2)::uuid
            AND principal_id=d.principal_id FOR UPDATE;
        IF NOT FOUND OR r.state<>'active' OR r.stage_index<>phase
          OR d.episode_id IS DISTINCT FROM r.episode_id
          OR d.contract_revision_id IS DISTINCT FROM r.contract_revision_id
          OR NOT routed_reasoning_task_current(r)
        THEN RAISE EXCEPTION 'routed reasoning stage is no longer current' USING ERRCODE='55000'; END IF;
        stage:=r.plan->'stages'->phase;
        IF stage->>'policy_id' IS DISTINCT FROM d.policy_id::text
          OR stage->'request' IS DISTINCT FROM routed_reasoning_payload(d.request)
        THEN RAISE EXCEPTION 'routed reasoning decision differs from plan' USING ERRCODE='55000'; END IF;
        RETURN r;
    END $$""",
    """CREATE FUNCTION routed_reasoning_run_write() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE planned_stage jsonb; ordered_role text; receipt jsonb; phase integer;
    BEGIN
        IF TG_OP='INSERT' THEN
            IF NEW.state<>'active' OR NEW.stage_index<>0 OR NEW.results<>'[]'::jsonb
              OR NOT routed_reasoning_task_current(NEW)
              OR jsonb_typeof(NEW.plan->'stages') IS DISTINCT FROM 'array'
              OR jsonb_typeof(NEW.plan->'task') IS DISTINCT FROM 'string'
              OR jsonb_typeof(NEW.plan->'evidence') IS DISTINCT FROM 'array'
              OR jsonb_typeof(NEW.plan->'constraints') IS DISTINCT FROM 'array'
            THEN RAISE EXCEPTION 'invalid initial routed reasoning run' USING ERRCODE='55000'; END IF;
            IF jsonb_array_length(NEW.plan->'stages')<>3
            THEN RAISE EXCEPTION 'routed reasoning must have three stages' USING ERRCODE='55000'; END IF;
            FOR phase IN 0..2 LOOP
                planned_stage:=NEW.plan->'stages'->phase;
                ordered_role:=(ARRAY['hypothesis_generator','critic','synthesizer'])[phase+1];
                IF jsonb_typeof(planned_stage) IS DISTINCT FROM 'object'
                  OR NOT planned_stage ?& ARRAY['role','policy_id','request']
                  OR planned_stage - ARRAY['role','policy_id','request'] <> '{}'::jsonb
                  OR planned_stage->>'role' IS DISTINCT FROM ordered_role
                  OR jsonb_typeof(planned_stage->'request') IS DISTINCT FROM 'object'
                  OR planned_stage->'request' IS DISTINCT FROM routed_reasoning_payload(planned_stage->'request')
                  OR planned_stage->'request'->>'episode_id' IS DISTINCT FROM NEW.episode_id::text
                  OR NOT EXISTS (
                    SELECT 1 FROM model_route_policies p JOIN model_route_prompt_recipes r
                      ON r.workspace_id=p.workspace_id AND r.policy_id=p.id
                    WHERE p.workspace_id=NEW.workspace_id AND p.principal_id=NEW.principal_id
                      AND p.id::text=planned_stage->>'policy_id' AND p.manifest->>'role'='reasoning'
                      AND r.recipe->>'role'=ordered_role
                  )
                THEN RAISE EXCEPTION 'routed reasoning stage differs from owned recipe'
                  USING ERRCODE='55000'; END IF;
            END LOOP;
            NEW.created_at:=clock_timestamp();
        ELSE
            IF OLD.state<>'active' OR
              (NEW.workspace_id,NEW.id,NEW.principal_id,NEW.episode_id,NEW.contract_revision_id,
               NEW.plan,NEW.plan_sha256,NEW.created_at) IS DISTINCT FROM
              (OLD.workspace_id,OLD.id,OLD.principal_id,OLD.episode_id,OLD.contract_revision_id,
               OLD.plan,OLD.plan_sha256,OLD.created_at)
            THEN RAISE EXCEPTION 'routed reasoning run identity or terminal state is immutable'
              USING ERRCODE='55000'; END IF;
            IF NEW.stage_index=OLD.stage_index+1 THEN
                receipt:=NEW.results->OLD.stage_index;
                IF NEW.state NOT IN ('active','completed')
                  OR NOT routed_reasoning_task_current(NEW)
                  OR NEW.results - OLD.stage_index IS DISTINCT FROM OLD.results
                  OR jsonb_typeof(receipt) IS DISTINCT FROM 'object'
                  OR NOT receipt ?& ARRAY['decision_id','output_sha256']
                  OR receipt - ARRAY['decision_id','output_sha256'] <> '{}'::jsonb
                  OR jsonb_typeof(receipt->'output_sha256') IS DISTINCT FROM 'string'
                  OR receipt->>'output_sha256' !~ '^[a-f0-9]{64}$'
                  OR NOT EXISTS (
                    SELECT 1 FROM model_route_decisions d JOIN jobs j
                      ON j.workspace_id=d.workspace_id AND j.id=d.job_id
                        AND j.owner_principal_id=d.principal_id
                    WHERE d.workspace_id=OLD.workspace_id AND d.principal_id=OLD.principal_id
                      AND d.id::text=receipt->>'decision_id'
                      AND d.allocation_key='rrun-v1:'||OLD.id::text||':'||OLD.stage_index::text
                      AND d.policy_id::text=OLD.plan->'stages'->OLD.stage_index->>'policy_id'
                      AND d.episode_id=OLD.episode_id AND d.contract_revision_id=OLD.contract_revision_id
                      AND j.operation_check_id=d.check_job_id AND j.state='succeeded'
                      AND j.dispatched_at IS NOT NULL AND j.finished_at IS NOT NULL
                  )
                THEN RAISE EXCEPTION 'routed reasoning advancement requires its confirmed stage job'
                  USING ERRCODE='55000'; END IF;
            ELSIF NEW.stage_index IS DISTINCT FROM OLD.stage_index
              OR NEW.results IS DISTINCT FROM OLD.results
              OR NEW.state NOT IN ('blocked','interrupted')
            THEN RAISE EXCEPTION 'routed reasoning stages advance once in order'
              USING ERRCODE='55000'; END IF;
        END IF;
        NEW.updated_at:=clock_timestamp();
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER routed_reasoning_run_write BEFORE INSERT OR UPDATE ON routed_reasoning_runs
        FOR EACH ROW EXECUTE FUNCTION routed_reasoning_run_write()""",
    """CREATE FUNCTION routed_reasoning_decision_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        PERFORM routed_reasoning_decision_current(NEW);
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER routed_reasoning_decision_insert BEFORE INSERT ON model_route_decisions
        FOR EACH ROW EXECUTE FUNCTION routed_reasoning_decision_insert()""",
    # Preserve the plan's literal facts independent of the controller. The
    # controller separately verifies the derived branch context and artifact.
    """CREATE FUNCTION routed_reasoning_compilation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE; r routed_reasoning_runs%ROWTYPE; outputs jsonb;
    BEGIN
        SELECT * INTO d FROM model_route_decisions
          WHERE workspace_id=NEW.workspace_id AND principal_id=NEW.principal_id
            AND id=NEW.decision_id;
        IF NOT FOUND THEN RETURN NEW; END IF;
        r:=routed_reasoning_decision_current(d);
        IF r.id IS NULL THEN RETURN NEW; END IF;
        SELECT COALESCE(jsonb_agg(s.receipt->'output_sha256' ORDER BY s.ordinal),'[]'::jsonb)
          INTO outputs FROM jsonb_array_elements(r.results) WITH ORDINALITY AS s(receipt,ordinal);
        IF NEW.facts - 'context' IS DISTINCT FROM r.plan - 'stages'
          OR jsonb_typeof(NEW.facts->'context') IS DISTINCT FROM 'object'
          OR (r.stage_index=0 AND NEW.facts->'context' IS DISTINCT FROM '{}'::jsonb)
          OR (r.stage_index>0 AND NEW.facts->'context'->'stage_outputs' IS DISTINCT FROM outputs)
        THEN RAISE EXCEPTION 'routed reasoning facts differ from its immutable plan'
          USING ERRCODE='55000'; END IF;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER routed_reasoning_compilation_insert BEFORE INSERT ON model_route_compiled_prompts
        FOR EACH ROW EXECUTE FUNCTION routed_reasoning_compilation_guard()""",
    """CREATE FUNCTION routed_reasoning_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE;
    BEGIN
        IF TG_OP='UPDATE' THEN
            IF OLD.dispatched_at IS NOT NULL OR NEW.dispatched_at IS NULL THEN RETURN NEW; END IF;
            SELECT * INTO d FROM model_route_decisions
              WHERE workspace_id=NEW.workspace_id AND principal_id=NEW.owner_principal_id
                AND job_id=NEW.id;
        ELSIF NEW.job_type='operation.check' AND NEW.input ? 'routing_decision_id' THEN
            SELECT * INTO d FROM model_route_decisions
              WHERE workspace_id=NEW.workspace_id AND principal_id=NEW.owner_principal_id
                AND id=(NEW.input->>'routing_decision_id')::uuid;
        ELSIF NEW.operation_check_id IS NOT NULL THEN
            SELECT * INTO d FROM model_route_decisions
              WHERE workspace_id=NEW.workspace_id AND principal_id=NEW.owner_principal_id
                AND check_job_id=NEW.operation_check_id;
        ELSE RETURN NEW;
        END IF;
        IF FOUND THEN PERFORM routed_reasoning_decision_current(d); END IF;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER routed_reasoning_job_insert AFTER INSERT ON jobs
        FOR EACH ROW EXECUTE FUNCTION routed_reasoning_job_guard()""",
    """CREATE TRIGGER routed_reasoning_job_dispatch BEFORE UPDATE OF dispatched_at ON jobs
        FOR EACH ROW EXECUTE FUNCTION routed_reasoning_job_guard()""",
    # Hosted transport has a separate final authorization transaction. Keep the
    # run fence at that boundary as well as the earlier job marker.
    """CREATE FUNCTION routed_reasoning_hosted_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE;
    BEGIN
        IF NEW.status='dispatched' AND OLD.status IS DISTINCT FROM NEW.status THEN
            SELECT route.* INTO d FROM hosted_requests h JOIN model_route_decisions route
              ON route.workspace_id=h.workspace_id AND route.job_id=h.job_id
              WHERE h.workspace_id=NEW.workspace_id AND h.id=NEW.id;
            IF FOUND THEN PERFORM routed_reasoning_decision_current(d); END IF;
        END IF;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER routed_reasoning_hosted_dispatch BEFORE UPDATE OF status ON hosted_invocations
        FOR EACH ROW EXECUTE FUNCTION routed_reasoning_hosted_dispatch()""",
)
