"""Add bounded candidate windows over two independently reviewed checked routes."""

WORKSPACE_TABLES = ("model_route_canaries", "model_route_canary_allocations")
APPEND_ONLY_TABLES = ("model_route_canary_allocations",)

MIGRATION_STATEMENTS = (
    """CREATE TABLE model_route_canaries (
      workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
      baseline_id uuid NOT NULL, candidate_id uuid NOT NULL CHECK(candidate_id<>baseline_id),
      baseline_epoch bigint NOT NULL, candidate_epoch bigint NOT NULL,
      baseline_review_id uuid NOT NULL, candidate_review_id uuid NOT NULL,
      sample_bps integer NOT NULL CHECK(sample_bps BETWEEN 1 AND 1000),
      max_candidate_allocations integer NOT NULL CHECK(max_candidate_allocations BETWEEN 1 AND 100),
      valid_until timestamptz NOT NULL, created_at timestamptz NOT NULL,
      rolled_back_at timestamptz,
      PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,candidate_id),
      FOREIGN KEY(workspace_id,baseline_id) REFERENCES model_route_policies(workspace_id,id),
      FOREIGN KEY(workspace_id,candidate_id) REFERENCES model_route_policies(workspace_id,id),
      FOREIGN KEY(workspace_id,baseline_review_id) REFERENCES model_route_reviews(workspace_id,id),
      FOREIGN KEY(workspace_id,candidate_review_id) REFERENCES model_route_reviews(workspace_id,id)
    )""",
    """CREATE TABLE model_route_canary_allocations (
      workspace_id uuid NOT NULL, canary_id uuid NOT NULL, principal_id uuid NOT NULL,
      allocation_key text NOT NULL CHECK(octet_length(allocation_key) BETWEEN 1 AND 160),
      input_sha256 text NOT NULL CHECK(input_sha256 ~ '^[0-9a-f]{64}$'),
      policy_id uuid NOT NULL, decision_key text NOT NULL,
      PRIMARY KEY(workspace_id,canary_id,allocation_key),
      UNIQUE(workspace_id,policy_id,decision_key),
      FOREIGN KEY(workspace_id,canary_id) REFERENCES model_route_canaries(workspace_id,id),
      FOREIGN KEY(workspace_id,policy_id,decision_key)
        REFERENCES model_route_decisions(workspace_id,policy_id,allocation_key)
        DEFERRABLE INITIALLY DEFERRED
    )""",
    """CREATE POLICY route_canary_owner ON model_route_canaries AS RESTRICTIVE FOR ALL
      USING(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)
      WITH CHECK(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE POLICY route_canary_allocation_owner ON model_route_canary_allocations AS RESTRICTIVE FOR ALL
      USING(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)
      WITH CHECK(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE INDEX model_route_canary_allocation_count
      ON model_route_canary_allocations(workspace_id,canary_id,policy_id)""",
    """CREATE FUNCTION model_route_canary_current(c model_route_canaries)
      RETURNS boolean LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM model_route_lock(c.workspace_id,c.candidate_id);
      PERFORM model_route_lock(c.workspace_id,c.baseline_id);
      RETURN c.rolled_back_at IS NULL AND c.valid_until>clock_timestamp()
        AND EXISTS(SELECT 1 FROM model_route_policies p
          WHERE p.workspace_id=c.workspace_id AND p.id=c.baseline_id AND p.stage='shadow'
            AND p.epoch=c.baseline_epoch)
        AND EXISTS(SELECT 1 FROM model_route_policies p
          WHERE p.workspace_id=c.workspace_id AND p.id=c.candidate_id AND p.stage='shadow'
            AND p.epoch=c.candidate_epoch)
        AND model_route_current(c.workspace_id,c.baseline_id) IS NOT DISTINCT FROM c.baseline_review_id
        AND model_route_current(c.workspace_id,c.candidate_id) IS NOT DISTINCT FROM c.candidate_review_id;
    END $$""",
    """CREATE FUNCTION model_route_canary_protect() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE baseline model_route_policies%ROWTYPE; candidate model_route_policies%ROWTYPE;
    BEGIN
      PERFORM model_route_lock(NEW.workspace_id,NEW.candidate_id);
      IF TG_OP='UPDATE' THEN
        IF OLD.rolled_back_at IS NOT NULL OR NEW.rolled_back_at IS NULL OR
          (to_jsonb(NEW)-'rolled_back_at') IS DISTINCT FROM (to_jsonb(OLD)-'rolled_back_at') THEN
          RAISE EXCEPTION 'canary is immutable except for terminal rollback' USING ERRCODE='55000';
        END IF;
        NEW.rolled_back_at:=clock_timestamp();
        RETURN NEW;
      END IF;
      PERFORM model_route_lock(NEW.workspace_id,NEW.baseline_id);
      SELECT * INTO baseline FROM model_route_policies WHERE workspace_id=NEW.workspace_id AND id=NEW.baseline_id;
      SELECT * INTO candidate FROM model_route_policies WHERE workspace_id=NEW.workspace_id AND id=NEW.candidate_id;
      NEW.created_at:=clock_timestamp();
      IF baseline.id IS NULL OR candidate.id IS NULL OR
        baseline.principal_id<>NEW.principal_id OR candidate.principal_id<>NEW.principal_id OR
        baseline.reviewer_id<>candidate.reviewer_id OR
        baseline.manifest->'candidate' IS DISTINCT FROM candidate.manifest->'baseline' OR
        baseline.manifest->'baseline' IS NOT DISTINCT FROM candidate.manifest->'baseline' OR
        baseline.manifest->'role' IS DISTINCT FROM candidate.manifest->'role' OR
        baseline.manifest->'atomic_kind' IS DISTINCT FROM candidate.manifest->'atomic_kind' OR
        baseline.manifest->'evaluation_plan_sha256' IS DISTINCT FROM candidate.manifest->'evaluation_plan_sha256' OR
        NEW.rolled_back_at IS NOT NULL OR NEW.valid_until>NEW.created_at+interval '1 hour' OR
        NOT model_route_canary_current(NEW) OR
        EXISTS(SELECT 1 FROM model_route_canaries WHERE workspace_id=NEW.workspace_id
          AND (candidate_id=NEW.baseline_id OR baseline_id=NEW.candidate_id)) OR
        EXISTS(SELECT 1 FROM model_route_decisions WHERE workspace_id=NEW.workspace_id AND policy_id=NEW.candidate_id) OR
        2<>(SELECT count(*) FROM model_route_prompt_recipes WHERE workspace_id=NEW.workspace_id
          AND policy_id IN (NEW.baseline_id,NEW.candidate_id)) THEN
        RAISE EXCEPTION 'canary requires exact separately reviewed compiled routes' USING ERRCODE='55000';
      END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_canary_protect BEFORE INSERT OR UPDATE ON model_route_canaries FOR EACH ROW EXECUTE FUNCTION model_route_canary_protect()",
    """CREATE FUNCTION model_route_canary_choice(c model_route_canaries, allocation text)
      RETURNS uuid LANGUAGE plpgsql AS $$
    DECLARE sample bytea;
    BEGIN
      IF NOT model_route_canary_current(c) THEN RETURN c.baseline_id; END IF;
      sample:=sha256(convert_to(c.id::text||'/'||allocation,'UTF8'));
      IF ((get_byte(sample,0)*16777216::bigint+get_byte(sample,1)*65536+
          get_byte(sample,2)*256+get_byte(sample,3))%10000)<c.sample_bps
        AND (SELECT count(*) FROM model_route_canary_allocations
          WHERE workspace_id=c.workspace_id AND canary_id=c.id AND policy_id=c.candidate_id)
          <c.max_candidate_allocations THEN
        RETURN c.candidate_id;
      END IF;
      RETURN c.baseline_id;
    END $$""",
    """CREATE FUNCTION model_route_canary_allocation_protect() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE c model_route_canaries%ROWTYPE;
    BEGIN
      SELECT * INTO c FROM model_route_canaries WHERE workspace_id=NEW.workspace_id AND id=NEW.canary_id;
      IF NOT FOUND OR c.principal_id<>NEW.principal_id THEN
        RAISE EXCEPTION 'canary allocation unavailable' USING ERRCODE='55000';
      END IF;
      PERFORM model_route_lock(c.workspace_id,c.candidate_id);
      SELECT * INTO c FROM model_route_canaries WHERE workspace_id=NEW.workspace_id AND id=NEW.canary_id;
      IF NEW.policy_id<>model_route_canary_choice(c,NEW.allocation_key)
        OR NEW.decision_key IS DISTINCT FROM 'canary/'||c.id::text||'/'||
           encode(sha256(convert_to(NEW.allocation_key,'UTF8')),'hex')
        OR EXISTS(SELECT 1 FROM model_route_decisions WHERE workspace_id=NEW.workspace_id
          AND policy_id=NEW.policy_id AND allocation_key=NEW.decision_key) THEN
        RAISE EXCEPTION 'canary allocation differs from its bounded selection' USING ERRCODE='55000';
      END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_canary_allocation_protect BEFORE INSERT ON model_route_canary_allocations FOR EACH ROW EXECUTE FUNCTION model_route_canary_allocation_protect()",
    """CREATE OR REPLACE FUNCTION model_route_decision_current(d model_route_decisions)
      RETURNS void LANGUAGE plpgsql AS $$
    DECLARE p model_route_policies%ROWTYPE; c model_route_canaries%ROWTYPE;
    BEGIN
      PERFORM model_route_lock(d.workspace_id,d.policy_id);
      SELECT * INTO p FROM model_route_policies WHERE workspace_id=d.workspace_id AND id=d.policy_id;
      IF NOT FOUND OR p.principal_id<>d.principal_id OR p.stage<>'shadow' OR p.epoch<>d.epoch
        OR d.review_id IS NULL OR d.expires_at<=clock_timestamp() OR model_route_current(d.workspace_id,d.policy_id) IS DISTINCT FROM d.review_id THEN
        RAISE EXCEPTION 'model route decision is stale or unaccepted' USING ERRCODE='55000'; END IF;
      IF NOT EXISTS(SELECT 1 FROM episodes e WHERE e.workspace_id=d.workspace_id AND e.id=d.episode_id
        AND e.active_contract_id=d.contract_revision_id AND e.status='running') THEN
        RAISE EXCEPTION 'model route task revision is stale' USING ERRCODE='55000'; END IF;
      SELECT * INTO c FROM model_route_canaries WHERE workspace_id=d.workspace_id AND candidate_id=d.policy_id;
      IF FOUND AND (NOT model_route_canary_current(c) OR NOT EXISTS(
        SELECT 1 FROM model_route_canary_allocations a WHERE a.workspace_id=d.workspace_id AND a.canary_id=c.id
          AND a.policy_id=d.policy_id AND a.decision_key=d.allocation_key AND a.principal_id=d.principal_id)) THEN
        RAISE EXCEPTION 'candidate decision is outside the current canary' USING ERRCODE='55000'; END IF;
    END $$""",
)
