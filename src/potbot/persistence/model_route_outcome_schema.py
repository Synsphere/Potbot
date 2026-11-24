"""Migration 52: explicit output sharing, independent review and regression fence.

Append-only observations copy terminal hosted job evidence; they do not settle
budgets, alter jobs or grant candidate execution. All functions are INVOKER.
"""

WORKSPACE_TABLES = (
    "model_route_outcomes",
    "model_route_outcome_shares",
    "model_route_outcome_reviews",
)
APPEND_ONLY_TABLES = WORKSPACE_TABLES
MIGRATION_STATEMENTS = (
    """CREATE TABLE model_route_outcomes (
      workspace_id uuid NOT NULL,decision_id uuid NOT NULL,policy_id uuid NOT NULL,
      principal_id uuid NOT NULL,job_id uuid NOT NULL,state text NOT NULL
        CHECK(state IN ('succeeded','failed','cancelled','indeterminate')),
      provider_receipts jsonb NOT NULL CHECK(jsonb_typeof(provider_receipts)='array' AND jsonb_array_length(provider_receipts)<=1),
      result jsonb,output_text text NOT NULL CHECK(octet_length(output_text)<=32768),
      dispatched_at timestamptz,finished_at timestamptz NOT NULL,
      output_contract text CHECK(output_contract='json_object_v1'),
      outcome_sha256 text NOT NULL CHECK(outcome_sha256 ~ '^[0-9a-f]{64}$'),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(workspace_id,decision_id),UNIQUE(workspace_id,job_id),
      FOREIGN KEY(workspace_id,decision_id) REFERENCES model_route_decisions(workspace_id,id),
      FOREIGN KEY(workspace_id,policy_id) REFERENCES model_route_policies(workspace_id,id)
    )""",
    """CREATE TABLE model_route_outcome_shares (
      workspace_id uuid NOT NULL,decision_id uuid NOT NULL,principal_id uuid NOT NULL,
      reviewer_id uuid NOT NULL CHECK(principal_id<>reviewer_id),
      outcome_sha256 text NOT NULL CHECK(outcome_sha256 ~ '^[0-9a-f]{64}$'),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(workspace_id,decision_id),
      FOREIGN KEY(workspace_id,decision_id) REFERENCES model_route_outcomes(workspace_id,decision_id)
    )""",
    """CREATE TABLE model_route_outcome_reviews (
      workspace_id uuid NOT NULL,id uuid NOT NULL,decision_id uuid NOT NULL,policy_id uuid NOT NULL,
      reviewer_id uuid NOT NULL,verdict text NOT NULL CHECK(verdict IN ('acceptable','regression','inconclusive')),
      outcome_sha256 text NOT NULL CHECK(outcome_sha256 ~ '^[0-9a-f]{64}$'),
      evidence_sha256 text NOT NULL CHECK(evidence_sha256 ~ '^[0-9a-f]{64}$'),
      evaluator_revision text NOT NULL CHECK(octet_length(evaluator_revision) BETWEEN 1 AND 128),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(workspace_id,id),UNIQUE(workspace_id,decision_id),
      FOREIGN KEY(workspace_id,decision_id) REFERENCES model_route_outcomes(workspace_id,decision_id),
      FOREIGN KEY(workspace_id,policy_id) REFERENCES model_route_policies(workspace_id,id)
    )""",
    """CREATE POLICY route_outcome_read ON model_route_outcomes AS RESTRICTIVE FOR SELECT
      USING(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid OR
        EXISTS(SELECT 1 FROM model_route_outcome_shares s WHERE s.workspace_id=model_route_outcomes.workspace_id
          AND s.decision_id=model_route_outcomes.decision_id AND s.reviewer_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid))""",
    """CREATE POLICY route_outcome_write ON model_route_outcomes AS RESTRICTIVE FOR INSERT
      WITH CHECK(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE POLICY route_share_read ON model_route_outcome_shares AS RESTRICTIVE FOR SELECT
      USING(NULLIF(current_setting('potbot.principal_id',true),'')::uuid IN (principal_id,reviewer_id))""",
    """CREATE POLICY route_share_write ON model_route_outcome_shares AS RESTRICTIVE FOR INSERT
      WITH CHECK(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE POLICY route_outcome_review_read ON model_route_outcome_reviews AS RESTRICTIVE FOR SELECT
      USING(EXISTS(SELECT 1 FROM model_route_policies p WHERE p.workspace_id=model_route_outcome_reviews.workspace_id AND p.id=policy_id))""",
    """CREATE POLICY route_outcome_review_write ON model_route_outcome_reviews AS RESTRICTIVE FOR INSERT
      WITH CHECK(reviewer_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE FUNCTION model_route_outcome_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE;j jobs%ROWTYPE;observed text;contract text;receipts jsonb;
    BEGIN
      SELECT * INTO d FROM model_route_decisions WHERE workspace_id=NEW.workspace_id AND id=NEW.decision_id;
      IF NOT FOUND OR d.principal_id<>NEW.principal_id OR d.policy_id<>NEW.policy_id OR d.job_id IS DISTINCT FROM NEW.job_id THEN
        RAISE EXCEPTION 'route outcome differs from original execution' USING ERRCODE='55000'; END IF;
      PERFORM model_route_lock(d.workspace_id,d.policy_id);
      SELECT * INTO j FROM jobs WHERE workspace_id=d.workspace_id AND id=d.job_id AND owner_principal_id=d.principal_id;
      IF NOT FOUND OR j.job_type<>'model.generate' OR j.state NOT IN ('succeeded','failed','cancelled','indeterminate')
        OR j.finished_at IS NULL OR (NEW.state,NEW.result,NEW.dispatched_at,NEW.finished_at)
          IS DISTINCT FROM (j.state,j.result,j.dispatched_at,j.finished_at) THEN
        RAISE EXCEPTION 'route outcome is not the original terminal hosted job' USING ERRCODE='55000'; END IF;
      SELECT COALESCE(string_agg(data->>'text','' ORDER BY sequence),'') INTO observed FROM job_events
        WHERE workspace_id=d.workspace_id AND job_id=d.job_id AND event_type='job.output';
      SELECT COALESCE(jsonb_agg(jsonb_build_object('id',h.id::text,'status',i.status,'finish_reason',i.result->>'finish_reason') ORDER BY h.ordinal),'[]'::jsonb) INTO receipts
        FROM hosted_requests h JOIN hosted_invocations i ON i.workspace_id=h.workspace_id AND i.id=h.id
        WHERE h.workspace_id=d.workspace_id AND h.job_id=d.job_id;
      IF NEW.provider_receipts IS DISTINCT FROM receipts THEN
        RAISE EXCEPTION 'route provider receipts differ' USING ERRCODE='55000'; END IF;
      SELECT recipe->>'output_contract' INTO contract FROM model_route_prompt_recipes WHERE workspace_id=d.workspace_id AND policy_id=d.policy_id;
      IF NEW.output_text IS DISTINCT FROM observed OR NEW.output_contract IS DISTINCT FROM contract THEN
        RAISE EXCEPTION 'route outcome output or recipe differs' USING ERRCODE='55000'; END IF;
      NEW.created_at:=clock_timestamp();RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_outcome_insert BEFORE INSERT ON model_route_outcomes FOR EACH ROW EXECUTE FUNCTION model_route_outcome_insert()",
    """CREATE FUNCTION model_route_outcome_share_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE o model_route_outcomes%ROWTYPE;p model_route_policies%ROWTYPE;
    BEGIN
      SELECT * INTO o FROM model_route_outcomes WHERE workspace_id=NEW.workspace_id AND decision_id=NEW.decision_id;
      IF NOT FOUND OR o.principal_id<>NEW.principal_id OR o.outcome_sha256<>NEW.outcome_sha256 THEN
        RAISE EXCEPTION 'route sharing requires the owned exact outcome' USING ERRCODE='55000'; END IF;
      PERFORM model_route_lock(o.workspace_id,o.policy_id);
      SELECT * INTO p FROM model_route_policies WHERE workspace_id=o.workspace_id AND id=o.policy_id;
      IF p.reviewer_id IS DISTINCT FROM NEW.reviewer_id THEN
        RAISE EXCEPTION 'route outcome reviewer differs' USING ERRCODE='55000'; END IF;
      NEW.created_at:=clock_timestamp();RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_outcome_share_insert BEFORE INSERT ON model_route_outcome_shares FOR EACH ROW EXECUTE FUNCTION model_route_outcome_share_insert()",
    """CREATE FUNCTION model_route_outcome_review_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE o model_route_outcomes%ROWTYPE;p model_route_policies%ROWTYPE;
    BEGIN
      PERFORM model_route_lock(NEW.workspace_id,NEW.policy_id);
      SELECT * INTO p FROM model_route_policies WHERE workspace_id=NEW.workspace_id AND id=NEW.policy_id;
      IF NOT FOUND OR p.reviewer_id<>NEW.reviewer_id THEN
        RAISE EXCEPTION 'independent route reviewer required' USING ERRCODE='55000'; END IF;
      SELECT * INTO o FROM model_route_outcomes WHERE workspace_id=NEW.workspace_id AND decision_id=NEW.decision_id;
      IF NOT FOUND OR o.policy_id<>NEW.policy_id OR o.outcome_sha256<>NEW.outcome_sha256
        OR NOT EXISTS(SELECT 1 FROM model_route_outcome_shares s WHERE s.workspace_id=o.workspace_id AND s.decision_id=o.decision_id AND s.reviewer_id=NEW.reviewer_id)
        OR (NEW.verdict='acceptable' AND (o.state<>'succeeded' OR o.dispatched_at IS NULL
          OR o.result->>'completed_models' IS DISTINCT FROM '1' OR jsonb_array_length(o.provider_receipts)<>1
          OR o.provider_receipts->0->>'status' IS DISTINCT FROM 'completed' OR o.provider_receipts->0->>'finish_reason' IS DISTINCT FROM 'stop')) THEN
        RAISE EXCEPTION 'shared confirmed outcome required for this review' USING ERRCODE='55000'; END IF;
      NEW.created_at:=clock_timestamp();RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_outcome_review_insert BEFORE INSERT ON model_route_outcome_reviews FOR EACH ROW EXECUTE FUNCTION model_route_outcome_review_insert()",
    # Outcome sharing must not expose the underlying private hosted request.
    """CREATE POLICY hosted_request_job_owner ON hosted_requests AS RESTRICTIVE FOR ALL
      USING(EXISTS(SELECT 1 FROM jobs j WHERE j.workspace_id=hosted_requests.workspace_id AND j.id=job_id))
      WITH CHECK(EXISTS(SELECT 1 FROM jobs j WHERE j.workspace_id=hosted_requests.workspace_id AND j.id=job_id))""",
    """CREATE POLICY hosted_invocation_job_owner ON hosted_invocations AS RESTRICTIVE FOR ALL
      USING(EXISTS(SELECT 1 FROM hosted_requests h WHERE h.workspace_id=hosted_invocations.workspace_id AND h.id=hosted_invocations.id))
      WITH CHECK(EXISTS(SELECT 1 FROM hosted_requests h WHERE h.workspace_id=hosted_invocations.workspace_id AND h.id=hosted_invocations.id))""",
    """CREATE POLICY hosted_event_job_owner ON hosted_invocation_events AS RESTRICTIVE FOR ALL
      USING(EXISTS(SELECT 1 FROM hosted_requests h WHERE h.workspace_id=hosted_invocation_events.workspace_id AND h.id=invocation_id))
      WITH CHECK(EXISTS(SELECT 1 FROM hosted_requests h WHERE h.workspace_id=hosted_invocation_events.workspace_id AND h.id=invocation_id))""",
    # Add an irreversible policy-local regression veto without editing migration50.
    """CREATE OR REPLACE FUNCTION model_route_current(w uuid,p uuid) RETURNS uuid LANGUAGE sql AS $$
      SELECT CASE WHEN accepted AND valid_until>clock_timestamp() AND NOT EXISTS(
        SELECT 1 FROM model_route_outcome_reviews o WHERE o.workspace_id=w AND o.policy_id=p AND o.verdict='regression') THEN id END
      FROM model_route_reviews WHERE workspace_id=w AND policy_id=p ORDER BY revision DESC LIMIT 1 $$""",
)
