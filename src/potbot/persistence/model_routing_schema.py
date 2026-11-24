"""Migration 50: persistent, independently accepted SHADOW model routes.

Candidates are recorded, never executed by this increment. Canary/promotion
remain database-blocked until their independent outcome gates are implemented.
Existing checked jobs own execution, accounting and uncertain outcomes.
All functions are SECURITY INVOKER; reviewers receive no private job access.
"""

WORKSPACE_TABLES = ("model_route_policies", "model_route_reviews", "model_route_decisions")
APPEND_ONLY_TABLES = ("model_route_reviews",)

MIGRATION_STATEMENTS = (
    """CREATE TABLE model_route_policies (
      workspace_id uuid NOT NULL, id uuid NOT NULL, principal_id uuid NOT NULL,
      reviewer_id uuid NOT NULL CHECK(reviewer_id<>principal_id),
      manifest jsonb NOT NULL CHECK(jsonb_typeof(manifest)='object' AND octet_length(manifest::text)<=16384),
      manifest_sha256 text NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
      stage text NOT NULL DEFAULT 'draft' CHECK(stage IN ('draft','shadow','blocked','disabled')),
      epoch bigint NOT NULL DEFAULT 1 CHECK(epoch>=1),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(workspace_id,id),
      FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id),
      FOREIGN KEY(workspace_id,reviewer_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE model_route_reviews (
      workspace_id uuid NOT NULL, id uuid NOT NULL, policy_id uuid NOT NULL,
      reviewer_id uuid NOT NULL, revision integer NOT NULL CHECK(revision>=1),
      accepted boolean NOT NULL,
      evidence_sha256 text NOT NULL CHECK(evidence_sha256 ~ '^[0-9a-f]{64}$'),
      valid_until timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,policy_id,revision),
      FOREIGN KEY(workspace_id,policy_id) REFERENCES model_route_policies(workspace_id,id),
      FOREIGN KEY(workspace_id,reviewer_id) REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    """CREATE TABLE model_route_decisions (
      workspace_id uuid NOT NULL, id uuid NOT NULL, policy_id uuid NOT NULL,
      principal_id uuid NOT NULL, epoch bigint NOT NULL, review_id uuid NOT NULL,
      allocation_key text NOT NULL CHECK(octet_length(allocation_key) BETWEEN 1 AND 160),
      request jsonb NOT NULL CHECK(jsonb_typeof(request)='object' AND octet_length(request::text)<=32768),
      episode_id uuid NOT NULL, contract_revision_id uuid NOT NULL,
      expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      check_job_id uuid, job_id uuid,
      PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,policy_id,allocation_key),
      UNIQUE(workspace_id,check_job_id), UNIQUE(workspace_id,job_id),
      FOREIGN KEY(workspace_id,policy_id) REFERENCES model_route_policies(workspace_id,id),
      FOREIGN KEY(workspace_id,review_id) REFERENCES model_route_reviews(workspace_id,id),
      FOREIGN KEY(workspace_id,episode_id,contract_revision_id) REFERENCES contract_revisions(workspace_id,episode_id,id),
      FOREIGN KEY(workspace_id,principal_id,check_job_id) REFERENCES jobs(workspace_id,owner_principal_id,id),
      FOREIGN KEY(workspace_id,principal_id,job_id) REFERENCES jobs(workspace_id,owner_principal_id,id)
    )""",
    """CREATE POLICY route_policy_read ON model_route_policies AS RESTRICTIVE FOR SELECT
      USING(NULLIF(current_setting('potbot.principal_id',true),'')::uuid IN (principal_id,reviewer_id))""",
    """CREATE POLICY route_policy_write ON model_route_policies AS RESTRICTIVE FOR INSERT
      WITH CHECK(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE POLICY route_policy_update ON model_route_policies AS RESTRICTIVE FOR UPDATE
      USING(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)
      WITH CHECK(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE POLICY route_review_read ON model_route_reviews AS RESTRICTIVE FOR SELECT
      USING(EXISTS(SELECT 1 FROM model_route_policies p WHERE p.workspace_id=model_route_reviews.workspace_id AND p.id=policy_id))""",
    """CREATE POLICY route_review_write ON model_route_reviews AS RESTRICTIVE FOR INSERT
      WITH CHECK(reviewer_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid AND
        EXISTS(SELECT 1 FROM model_route_policies p WHERE p.workspace_id=model_route_reviews.workspace_id
          AND p.id=policy_id AND p.reviewer_id=model_route_reviews.reviewer_id))""",
    """CREATE POLICY route_decision_owner ON model_route_decisions AS RESTRICTIVE FOR ALL
      USING(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)
      WITH CHECK(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    # A transaction-scoped common lock orders review, activation, allocation,
    # admission and possible-dispatch markers without a privileged function.
    """CREATE FUNCTION model_route_lock(w uuid,p uuid) RETURNS void LANGUAGE sql AS $$
      SELECT pg_advisory_xact_lock(hashtextextended(w::text||'/model-route/'||p::text,0)) $$""",
    """CREATE FUNCTION model_route_current(w uuid,p uuid) RETURNS uuid LANGUAGE sql AS $$
      SELECT CASE WHEN accepted AND valid_until>clock_timestamp() THEN id END
      FROM model_route_reviews WHERE workspace_id=w AND policy_id=p ORDER BY revision DESC LIMIT 1 $$""",
    """CREATE FUNCTION model_route_review_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM model_route_lock(NEW.workspace_id,NEW.policy_id);
      NEW.created_at:=clock_timestamp();
      IF NEW.valid_until<=NEW.created_at OR NEW.valid_until>NEW.created_at+interval '30 days' THEN
        RAISE EXCEPTION 'model route review validity exceeds bounds' USING ERRCODE='55000'; END IF;
      SELECT coalesce(max(revision),0)+1 INTO NEW.revision FROM model_route_reviews
        WHERE workspace_id=NEW.workspace_id AND policy_id=NEW.policy_id;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_review_insert BEFORE INSERT ON model_route_reviews FOR EACH ROW EXECUTE FUNCTION model_route_review_insert()",
    """CREATE FUNCTION model_route_policy_protect() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM model_route_lock(NEW.workspace_id,NEW.id);
      IF TG_OP='INSERT' THEN
        IF NEW.stage<>'draft' OR NEW.epoch<>1 THEN
          RAISE EXCEPTION 'model routes begin in draft' USING ERRCODE='55000'; END IF;
        RETURN NEW;
      END IF;
      IF (NEW.workspace_id,NEW.id,NEW.principal_id,NEW.reviewer_id,NEW.manifest,NEW.manifest_sha256,NEW.created_at)
        IS DISTINCT FROM (OLD.workspace_id,OLD.id,OLD.principal_id,OLD.reviewer_id,OLD.manifest,OLD.manifest_sha256,OLD.created_at)
        OR NEW.epoch<>OLD.epoch+1 OR OLD.stage='disabled' OR NEW.stage=OLD.stage
        OR NOT (NEW.stage='disabled' OR (OLD.stage IN ('draft','blocked') AND NEW.stage='shadow')
          OR (OLD.stage='shadow' AND NEW.stage='blocked')) THEN
          RAISE EXCEPTION 'model route identity or transition changed' USING ERRCODE='55000'; END IF;
      IF NEW.stage='shadow' AND model_route_current(NEW.workspace_id,NEW.id) IS NULL THEN
        RAISE EXCEPTION 'independent current baseline acceptance required' USING ERRCODE='55000'; END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_policy_protect BEFORE INSERT OR UPDATE ON model_route_policies FOR EACH ROW EXECUTE FUNCTION model_route_policy_protect()",
    """CREATE FUNCTION model_route_matches(projection jsonb,actual jsonb,depth integer DEFAULT 0)
      RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
    DECLARE pair record;
    BEGIN
      IF depth>12 OR actual IS NULL OR projection IS NULL THEN RETURN false; END IF;
      IF jsonb_typeof(projection)='object' THEN
        IF jsonb_typeof(actual)<>'object' THEN RETURN false; END IF;
        FOR pair IN SELECT * FROM jsonb_each(projection) LOOP
          IF NOT model_route_matches(pair.value,actual->pair.key,depth+1) THEN RETURN false; END IF;
        END LOOP;
        RETURN true;
      END IF;
      RETURN projection=actual;
    END $$""",
    """CREATE FUNCTION model_route_decision_current(d model_route_decisions) RETURNS void LANGUAGE plpgsql AS $$
    DECLARE p model_route_policies%ROWTYPE;
    BEGIN
      PERFORM model_route_lock(d.workspace_id,d.policy_id);
      SELECT * INTO p FROM model_route_policies WHERE workspace_id=d.workspace_id AND id=d.policy_id;
      IF NOT FOUND OR p.principal_id<>d.principal_id OR p.stage<>'shadow' OR p.epoch<>d.epoch
        OR d.review_id IS NULL OR d.expires_at<=clock_timestamp() OR model_route_current(d.workspace_id,d.policy_id) IS DISTINCT FROM d.review_id THEN
        RAISE EXCEPTION 'model route decision is stale or unaccepted' USING ERRCODE='55000'; END IF;
      IF NOT EXISTS(SELECT 1 FROM episodes e WHERE e.workspace_id=d.workspace_id AND e.id=d.episode_id
        AND e.active_contract_id=d.contract_revision_id AND e.status='running') THEN
        RAISE EXCEPTION 'model route task revision is stale' USING ERRCODE='55000'; END IF;
    END $$""",
    """CREATE FUNCTION model_route_decision_protect() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE p model_route_policies%ROWTYPE;
    BEGIN
      IF TG_OP='INSERT' THEN
        PERFORM model_route_decision_current(NEW);
        SELECT * INTO p FROM model_route_policies WHERE workspace_id=NEW.workspace_id AND id=NEW.policy_id;
        IF NEW.expires_at>clock_timestamp()+interval '5 minutes' OR NEW.check_job_id IS NOT NULL OR NEW.job_id IS NOT NULL
          OR NEW.request->>'episode_id' IS DISTINCT FROM NEW.episode_id::text
          OR NOT model_route_matches(p.manifest->'baseline'->'executor',NEW.request) THEN
          RAISE EXCEPTION 'model route request differs from baseline' USING ERRCODE='55000'; END IF;
      ELSE
        IF (NEW.workspace_id,NEW.id,NEW.policy_id,NEW.principal_id,NEW.epoch,NEW.review_id,NEW.allocation_key,
          NEW.request,NEW.episode_id,NEW.contract_revision_id,NEW.expires_at,NEW.created_at)
          IS DISTINCT FROM (OLD.workspace_id,OLD.id,OLD.policy_id,OLD.principal_id,OLD.epoch,OLD.review_id,OLD.allocation_key,
          OLD.request,OLD.episode_id,OLD.contract_revision_id,OLD.expires_at,OLD.created_at)
          OR (OLD.check_job_id IS NOT NULL AND NEW.check_job_id IS DISTINCT FROM OLD.check_job_id)
          OR (OLD.job_id IS NOT NULL AND NEW.job_id IS DISTINCT FROM OLD.job_id) THEN
          RAISE EXCEPTION 'model route allocation is immutable' USING ERRCODE='55000'; END IF;
        IF NEW.check_job_id IS DISTINCT FROM OLD.check_job_id THEN
          IF NOT EXISTS(SELECT 1 FROM jobs j WHERE j.workspace_id=NEW.workspace_id AND j.id=NEW.check_job_id
            AND j.owner_principal_id=NEW.principal_id AND j.job_type='operation.check'
            AND j.input->>'routing_decision_id'=NEW.id::text AND j.input->'request'=NEW.request) THEN
            RAISE EXCEPTION 'model route check link differs' USING ERRCODE='55000'; END IF;
        END IF;
        IF NEW.job_id IS DISTINCT FROM OLD.job_id THEN
          IF NEW.check_job_id IS NULL OR NOT EXISTS(SELECT 1 FROM jobs j WHERE j.workspace_id=NEW.workspace_id
            AND j.id=NEW.job_id AND j.owner_principal_id=NEW.principal_id AND j.operation_check_id=NEW.check_job_id) THEN
            RAISE EXCEPTION 'model route execution link differs' USING ERRCODE='55000'; END IF;
        END IF;
      END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_decision_protect BEFORE INSERT OR UPDATE ON model_route_decisions FOR EACH ROW EXECUTE FUNCTION model_route_decision_protect()",
    """CREATE FUNCTION model_route_job_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE; p model_route_policies%ROWTYPE;
    BEGIN
      IF NEW.job_type='operation.check' AND NEW.input ? 'routing_decision_id' THEN
        SELECT * INTO d FROM model_route_decisions WHERE workspace_id=NEW.workspace_id
          AND principal_id=NEW.owner_principal_id AND id=(NEW.input->>'routing_decision_id')::uuid;
        IF NOT FOUND THEN RAISE EXCEPTION 'unknown model route decision' USING ERRCODE='55000'; END IF;
        PERFORM model_route_decision_current(d);
        SELECT * INTO p FROM model_route_policies WHERE workspace_id=d.workspace_id AND id=d.policy_id;
        IF d.check_job_id IS NOT NULL OR d.request IS DISTINCT FROM NEW.input->'request'
          OR NEW.input->>'operation_id' IS DISTINCT FROM p.manifest->'baseline'->>'operation_id'
          OR NEW.episode_id<>d.episode_id OR NEW.contract_revision_id<>d.contract_revision_id THEN
          RAISE EXCEPTION 'model route check was changed or reused' USING ERRCODE='55000'; END IF;
        UPDATE model_route_decisions SET check_job_id=NEW.id WHERE workspace_id=d.workspace_id AND id=d.id;
      ELSIF NEW.operation_check_id IS NOT NULL THEN
        SELECT * INTO d FROM model_route_decisions WHERE workspace_id=NEW.workspace_id
          AND principal_id=NEW.owner_principal_id AND check_job_id=NEW.operation_check_id;
        IF FOUND THEN
          PERFORM model_route_decision_current(d);
          IF d.job_id IS NOT NULL OR NEW.episode_id<>d.episode_id OR NEW.contract_revision_id<>d.contract_revision_id
            OR NEW.job_type IS DISTINCT FROM d.request->>'job_type' OR NEW.input IS DISTINCT FROM d.request->'input'
            OR NEW.selected_models IS DISTINCT FROM d.request->'selected_models' THEN
            RAISE EXCEPTION 'model route execution was changed or reused' USING ERRCODE='55000'; END IF;
          UPDATE model_route_decisions SET job_id=NEW.id WHERE workspace_id=d.workspace_id AND id=d.id;
        END IF;
      END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_job_insert AFTER INSERT ON jobs FOR EACH ROW EXECUTE FUNCTION model_route_job_insert()",
    """CREATE FUNCTION model_route_dispatch_fence() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE;
    BEGIN
      IF OLD.dispatched_at IS NULL AND NEW.dispatched_at IS NOT NULL THEN
        SELECT * INTO d FROM model_route_decisions WHERE workspace_id=NEW.workspace_id
          AND principal_id=NEW.owner_principal_id AND job_id=NEW.id;
        IF FOUND THEN PERFORM model_route_decision_current(d); END IF;
      END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_dispatch_fence BEFORE UPDATE OF dispatched_at ON jobs FOR EACH ROW EXECUTE FUNCTION model_route_dispatch_fence()",
)
