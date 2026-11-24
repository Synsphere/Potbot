"""Migration 56: freeze operator guidance before each successor stage allocation."""

WORKSPACE_TABLES = ("routed_reasoning_guidance",)
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    """CREATE TABLE routed_reasoning_guidance (
        workspace_id uuid NOT NULL,
        run_id uuid NOT NULL,
        principal_id uuid NOT NULL,
        stage_index integer NOT NULL CHECK(stage_index IN (1,2)),
        constraints jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,run_id,stage_index),
        FOREIGN KEY(workspace_id,run_id) REFERENCES routed_reasoning_runs(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE POLICY routed_reasoning_guidance_owner
        ON routed_reasoning_guidance AS RESTRICTIVE FOR ALL
        USING (principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK (principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    # Allocation takes its policy lock before the run lock. Guidance takes
    # only the run lock and reads the decision without locking it.
    """CREATE FUNCTION routed_reasoning_guidance_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE r routed_reasoning_runs%ROWTYPE;
    BEGIN
        SELECT * INTO r FROM routed_reasoning_runs
          WHERE workspace_id=NEW.workspace_id AND id=NEW.run_id
            AND principal_id=NEW.principal_id FOR UPDATE;
        IF NOT FOUND OR r.state<>'active' OR r.stage_index NOT IN (1,2)
          OR r.stage_index IS DISTINCT FROM NEW.stage_index
          OR NOT routed_reasoning_task_current(r)
        THEN RAISE EXCEPTION 'operator guidance requires the current successor stage'
          USING ERRCODE='55000'; END IF;
        IF EXISTS (
            SELECT 1 FROM model_route_decisions d
              WHERE d.workspace_id=r.workspace_id AND d.principal_id=r.principal_id
                AND d.policy_id=(r.plan->'stages'->r.stage_index->>'policy_id')::uuid
                AND d.allocation_key='rrun-v1:'||r.id::text||':'||r.stage_index::text
        ) THEN RAISE EXCEPTION 'operator guidance cannot change an allocated stage'
          USING ERRCODE='55000'; END IF;
        IF jsonb_typeof(NEW.constraints) IS DISTINCT FROM 'array' THEN
          RAISE EXCEPTION 'operator guidance requires a bounded array'
            USING ERRCODE='55000';
        END IF;
        IF jsonb_array_length(NEW.constraints) NOT BETWEEN 1 AND 4
          OR EXISTS (
            SELECT 1 FROM jsonb_array_elements(NEW.constraints) AS item(value)
            WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'string'
              OR octet_length(convert_to(item.value #>> '{}','UTF8')) NOT BETWEEN 1 AND 256
              OR (item.value #>> '{}') ~ '[[:cntrl:]]|^[[:space:]]|[[:space:]]$'
          ) OR EXISTS (
            SELECT 1 FROM jsonb_array_elements(NEW.constraints) AS item(value)
            GROUP BY item.value HAVING count(*)>1
          )
        THEN RAISE EXCEPTION 'operator guidance requires distinct bounded strings'
          USING ERRCODE='55000'; END IF;
        NEW.created_at:=clock_timestamp();
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER routed_reasoning_guidance_insert
        BEFORE INSERT ON routed_reasoning_guidance
        FOR EACH ROW EXECUTE FUNCTION routed_reasoning_guidance_insert()""",
    """CREATE OR REPLACE FUNCTION routed_reasoning_compilation_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE; r routed_reasoning_runs%ROWTYPE;
        outputs jsonb; context jsonb; guidance jsonb;
    BEGIN
        SELECT * INTO d FROM model_route_decisions
          WHERE workspace_id=NEW.workspace_id AND principal_id=NEW.principal_id
            AND id=NEW.decision_id;
        IF NOT FOUND THEN RETURN NEW; END IF;
        r:=routed_reasoning_decision_current(d);
        IF r.id IS NULL THEN RETURN NEW; END IF;
        SELECT COALESCE(jsonb_agg(s.receipt->'output_sha256' ORDER BY s.ordinal),'[]'::jsonb)
          INTO outputs FROM jsonb_array_elements(r.results) WITH ORDINALITY AS s(receipt,ordinal);
        context:=NEW.facts->'context';
        IF NEW.facts - 'context' IS DISTINCT FROM r.plan - 'stages' - 'answer_check'
          OR jsonb_typeof(context) IS DISTINCT FROM 'object'
          OR (r.stage_index=0 AND context IS DISTINCT FROM '{}'::jsonb)
          OR (r.stage_index>0 AND context->'stage_outputs' IS DISTINCT FROM outputs)
          OR context->'answer_check' IS DISTINCT FROM
            (CASE WHEN r.stage_index=2 THEN r.plan->'answer_check' END)
        THEN RAISE EXCEPTION 'routed reasoning facts differ from its immutable plan'
          USING ERRCODE='55000'; END IF;
        SELECT g.constraints INTO guidance FROM routed_reasoning_guidance g
          WHERE g.workspace_id=r.workspace_id AND g.run_id=r.id
            AND g.principal_id=r.principal_id AND g.stage_index=r.stage_index;
        IF context->'operator_constraints' IS DISTINCT FROM guidance THEN
          RAISE EXCEPTION 'routed reasoning facts differ from retained operator guidance'
            USING ERRCODE='55000';
        END IF;
        RETURN NEW;
    END $$""",
)
