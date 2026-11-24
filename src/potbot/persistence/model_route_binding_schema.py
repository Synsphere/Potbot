"""Migration 51: immutable prompt facts and checked worker admission identities.

Adds constraints to opt-in compiled routes, without changing migration50 or
allowing candidate execution. Normal jobs, leases and budget owners remain.
"""

WORKSPACE_TABLES = ("model_route_prompt_recipes", "model_route_compiled_prompts")
APPEND_ONLY_TABLES = WORKSPACE_TABLES
MIGRATION_STATEMENTS = (
    """CREATE TABLE model_route_prompt_recipes (
      workspace_id uuid NOT NULL,policy_id uuid NOT NULL,principal_id uuid NOT NULL,
      recipe jsonb NOT NULL CHECK(jsonb_typeof(recipe)='object' AND octet_length(recipe::text)<=4096),
      recipe_sha256 text NOT NULL CHECK(recipe_sha256 ~ '^[0-9a-f]{64}$'),
      PRIMARY KEY(workspace_id,policy_id),
      FOREIGN KEY(workspace_id,policy_id) REFERENCES model_route_policies(workspace_id,id)
    )""",
    """CREATE TABLE model_route_compiled_prompts (
      workspace_id uuid NOT NULL,decision_id uuid NOT NULL,principal_id uuid NOT NULL,
      facts jsonb NOT NULL CHECK(jsonb_typeof(facts)='object' AND octet_length(facts::text)<=32768),
      prompt_sha256 text NOT NULL CHECK(prompt_sha256 ~ '^[0-9a-f]{64}$'),
      recipe_sha256 text NOT NULL CHECK(recipe_sha256 ~ '^[0-9a-f]{64}$'),
      binding_sha256 text NOT NULL CHECK(binding_sha256 ~ '^[0-9a-f]{64}$'),
      PRIMARY KEY(workspace_id,decision_id),
      FOREIGN KEY(workspace_id,decision_id) REFERENCES model_route_decisions(workspace_id,id)
    )""",
    """CREATE POLICY route_recipe_read ON model_route_prompt_recipes AS RESTRICTIVE FOR SELECT
      USING(EXISTS(SELECT 1 FROM model_route_policies p WHERE p.workspace_id=model_route_prompt_recipes.workspace_id AND p.id=policy_id))""",
    """CREATE POLICY route_recipe_write ON model_route_prompt_recipes AS RESTRICTIVE FOR INSERT
      WITH CHECK(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid AND
        EXISTS(SELECT 1 FROM model_route_policies p WHERE p.workspace_id=model_route_prompt_recipes.workspace_id AND p.id=policy_id AND p.principal_id=model_route_prompt_recipes.principal_id))""",
    """CREATE POLICY route_compilation_owner ON model_route_compiled_prompts AS RESTRICTIVE FOR ALL
      USING(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)
      WITH CHECK(principal_id=NULLIF(current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE FUNCTION model_route_recipe_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE p model_route_policies%ROWTYPE;
    BEGIN
      PERFORM model_route_lock(NEW.workspace_id,NEW.policy_id);
      SELECT * INTO p FROM model_route_policies WHERE workspace_id=NEW.workspace_id AND id=NEW.policy_id;
      IF NOT FOUND OR p.principal_id<>NEW.principal_id OR p.stage<>'draft' OR p.epoch<>1
        OR p.manifest->>'prompt_contract_sha256' IS DISTINCT FROM NEW.recipe_sha256
        OR EXISTS(SELECT 1 FROM model_route_reviews WHERE workspace_id=NEW.workspace_id AND policy_id=NEW.policy_id)
        OR EXISTS(SELECT 1 FROM model_route_decisions WHERE workspace_id=NEW.workspace_id AND policy_id=NEW.policy_id) THEN
        RAISE EXCEPTION 'route recipe must be frozen before independent review' USING ERRCODE='55000'; END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_recipe_insert BEFORE INSERT ON model_route_prompt_recipes FOR EACH ROW EXECUTE FUNCTION model_route_recipe_insert()",
    """CREATE FUNCTION model_route_compilation_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE; recipe_hash text;
    BEGIN
      SELECT * INTO d FROM model_route_decisions WHERE workspace_id=NEW.workspace_id AND id=NEW.decision_id;
      IF NOT FOUND OR d.principal_id<>NEW.principal_id THEN
        RAISE EXCEPTION 'compiled route owner differs' USING ERRCODE='55000'; END IF;
      PERFORM model_route_decision_current(d);
      SELECT recipe_sha256 INTO recipe_hash FROM model_route_prompt_recipes WHERE workspace_id=d.workspace_id AND policy_id=d.policy_id;
      IF d.check_job_id IS NOT NULL OR recipe_hash IS DISTINCT FROM NEW.recipe_sha256 THEN
        RAISE EXCEPTION 'compiled route recipe changed or already checked' USING ERRCODE='55000'; END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_compilation_insert BEFORE INSERT ON model_route_compiled_prompts FOR EACH ROW EXECUTE FUNCTION model_route_compilation_insert()",
    """CREATE FUNCTION model_route_bound_job() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE; p model_route_policies%ROWTYPE;
    compiled model_route_compiled_prompts%ROWTYPE; observed jsonb;
    BEGIN
      IF TG_OP='UPDATE' AND NOT (OLD.dispatched_at IS NULL AND NEW.dispatched_at IS NOT NULL) THEN RETURN NEW; END IF;
      IF NEW.job_type='operation.check' AND NEW.input ? 'routing_decision_id' THEN
        SELECT * INTO d FROM model_route_decisions WHERE workspace_id=NEW.workspace_id
          AND principal_id=NEW.owner_principal_id AND id=(NEW.input->>'routing_decision_id')::uuid;
      ELSIF NEW.operation_check_id IS NOT NULL THEN
        SELECT * INTO d FROM model_route_decisions WHERE workspace_id=NEW.workspace_id
          AND principal_id=NEW.owner_principal_id AND check_job_id=NEW.operation_check_id;
      ELSE RETURN NEW; END IF;
      IF NOT FOUND THEN RETURN NEW; END IF;
      PERFORM model_route_lock(d.workspace_id,d.policy_id);
      IF NOT EXISTS(SELECT 1 FROM model_route_prompt_recipes WHERE workspace_id=d.workspace_id AND policy_id=d.policy_id) THEN RETURN NEW; END IF;
      SELECT * INTO compiled FROM model_route_compiled_prompts WHERE workspace_id=d.workspace_id AND decision_id=d.id;
      IF NOT FOUND THEN RAISE EXCEPTION 'compiled route provenance is required' USING ERRCODE='55000'; END IF;
      IF NEW.operation_check_id IS NOT NULL THEN
        SELECT result->'operation_check'->'bindings' INTO observed FROM jobs WHERE workspace_id=NEW.workspace_id AND id=NEW.operation_check_id;
        SELECT * INTO p FROM model_route_policies WHERE workspace_id=d.workspace_id AND id=d.policy_id;
        IF observed->>'modelPromptSha256' IS DISTINCT FROM compiled.prompt_sha256
          OR observed->>'modelRoutingSha256' IS DISTINCT FROM compiled.binding_sha256
          OR observed->>'modelRuntimeSha256' IS DISTINCT FROM p.manifest->'baseline'->>'runtime_identity_sha256' THEN
          RAISE EXCEPTION 'checked route admission bindings differ' USING ERRCODE='55000'; END IF;
      END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER model_route_bound_job AFTER INSERT ON jobs FOR EACH ROW EXECUTE FUNCTION model_route_bound_job()",
    "CREATE TRIGGER model_route_bound_dispatch BEFORE UPDATE OF dispatched_at ON jobs FOR EACH ROW EXECUTE FUNCTION model_route_bound_job()",
)
