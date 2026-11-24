"""Migration 53: observe the original native model job without invoking it."""

WORKSPACE_TABLES: tuple[str, ...] = ()
APPEND_ONLY_TABLES: tuple[str, ...] = ()
MIGRATION_STATEMENTS = (
    "ALTER TABLE model_route_outcomes ADD COLUMN native_evidence jsonb",
    """CREATE FUNCTION model_route_native_evidence(j jobs, evidence jsonb, observed text)
       RETURNS jsonb LANGUAGE plpgsql AS $$
    DECLARE a artifacts%ROWTYPE; req jsonb; dep jsonb; raw text; envelope jsonb; v jsonb;
      usage jsonb; request_hash text; receipt jsonb;
    BEGIN
      IF j.job_type<>'cog.execute' OR j.input->>'type_ref' IS DISTINCT FROM 'syn://type/potbot-local-model-request-v1'
        OR jsonb_typeof(evidence) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'native outcome needs its original model job' USING ERRCODE='55000'; END IF;
      IF j.state<>'succeeded' THEN
        IF evidence<>'{}'::jsonb OR observed<>'' THEN
          RAISE EXCEPTION 'unconfirmed native outcome cannot disclose result data' USING ERRCODE='55000'; END IF;
        RETURN evidence;
      END IF;
      raw:=evidence->>'artifact_body';
      IF j.dispatched_at IS NULL OR j.result->>'operation_id' IS DISTINCT FROM j.id::text
        OR octet_length(raw) NOT BETWEEN 1 AND 16384 OR NOT (raw IS JSON OBJECT WITH UNIQUE KEYS) THEN
        RAISE EXCEPTION 'completed native outcome needs its typed artifact' USING ERRCODE='55000'; END IF;
      SELECT * INTO a FROM artifacts WHERE workspace_id=j.workspace_id AND id::text=j.result->>'artifact_id';
      IF NOT FOUND OR a.media_type<>'application/json' OR octet_length(raw)<>a.byte_length
        OR encode(sha256(convert_to(raw,'UTF8')),'hex')<>a.digest THEN
        RAISE EXCEPTION 'native artifact bytes differ from original publication' USING ERRCODE='55000'; END IF;
      envelope:=raw::jsonb;
      v:=envelope->'value';
      req:=j.input->'value';
      SELECT content INTO dep FROM model_deployments WHERE workspace_id=j.workspace_id AND content_hash=req->>'deployment_sha256';
      IF NOT FOUND OR dep->>'canonical_model' IS DISTINCT FROM req->>'model'
        OR dep->>'output_adapter' IS DISTINCT FROM 'potbot.native-model-result/1'
        OR NOT COALESCE(dep->'response_schemas' ? 'syn://type/potbot-local-model-result-v1',false) THEN
        RAISE EXCEPTION 'native deployment differs from original publication' USING ERRCODE='55000'; END IF;
      -- Native request v1 has exactly these five scalar keys in canonical order.
      request_hash:=encode(sha256(convert_to(
        '{"deployment_sha256":'||to_json(req->>'deployment_sha256')::text||
        ',"max_output_tokens":'||(req->>'max_output_tokens')||
        ',"model":'||to_json(req->>'model')::text||',"prompt":'||to_json(req->>'prompt')::text||
        ',"system":'||to_json(req->>'system')::text||'}','UTF8')),'hex');
      IF envelope-'value' IS DISTINCT FROM '{"type_ref":"syn://type/potbot-local-model-result-v1"}'::jsonb
        OR jsonb_typeof(v) IS DISTINCT FROM 'object'
        OR NOT (v ?& ARRAY['schema','invocation_id','request_sha256','deployment_sha256','model',
                            'provenance','text','finish_reason','usage'])
        OR v-ARRAY['schema','invocation_id','request_sha256','deployment_sha256','model','provenance',
                   'text','finish_reason','usage']<>'{}'::jsonb
        OR v->>'schema' IS DISTINCT FROM 'potbot.native-model-result/1'
        OR v->>'invocation_id' IS DISTINCT FROM j.id::text
        OR request_hash IS NULL OR v->>'request_sha256' IS DISTINCT FROM request_hash
        OR v->>'deployment_sha256' IS DISTINCT FROM req->>'deployment_sha256'
        OR v->>'model' IS DISTINCT FROM req->>'model'
        OR v->'provenance' IS DISTINCT FROM jsonb_build_object(
          'weights_sha256',dep->'weights_digest','backend',dep->'backend',
          'backend_version',dep->'backend_version','tokenizer_revision',dep->'tokenizer_revision',
          'chat_template_sha256',dep->'chat_template_digest')
        OR jsonb_typeof(v->'text') IS DISTINCT FROM 'string' OR v->>'text' IS DISTINCT FROM observed
        OR octet_length(observed) NOT BETWEEN 1 AND 8192 OR observed !~ '[^[:space:]]'
        OR NOT COALESCE(v->>'finish_reason' IN ('stop','length','cancelled','failed'),false) THEN
        RAISE EXCEPTION 'native result differs from the original checked request' USING ERRCODE='55000'; END IF;
      usage:=v->'usage';
      IF jsonb_typeof(usage) IS DISTINCT FROM 'object'
        OR NOT (usage ?& ARRAY['input_tokens','output_tokens','total_tokens'])
        OR usage-ARRAY['input_tokens','output_tokens','total_tokens']<>'{}'::jsonb
        OR EXISTS(SELECT 1 FROM jsonb_each(usage)
                  WHERE jsonb_typeof(value)<>'number' OR value::text !~ '^[1-9][0-9]{0,9}$') THEN
        RAISE EXCEPTION 'native result usage is malformed' USING ERRCODE='55000'; END IF;
      IF (usage->>'input_tokens')::bigint+(usage->>'output_tokens')::bigint<>(usage->>'total_tokens')::bigint
        OR (usage->>'output_tokens')::bigint>(req->>'max_output_tokens')::bigint
        OR (usage->>'total_tokens')::bigint>(dep->>'usable_context_tokens')::bigint THEN
        RAISE EXCEPTION 'native result usage exceeds its checked bounds' USING ERRCODE='55000'; END IF;
      receipt:=(v-ARRAY['schema','invocation_id','text'])||jsonb_build_object(
        'artifact_id', a.id, 'artifact_sha256', a.digest, 'usage_origin', 'worker_reported');
      IF evidence-'artifact_body' IS DISTINCT FROM receipt THEN
        RAISE EXCEPTION 'native outcome receipt differs from the typed result' USING ERRCODE='55000'; END IF;
      RETURN receipt;
    END $$""",
    """CREATE OR REPLACE FUNCTION model_route_outcome_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE;j jobs%ROWTYPE;observed text;contract text;receipts jsonb;
    BEGIN
      SELECT * INTO d FROM model_route_decisions WHERE workspace_id=NEW.workspace_id AND id=NEW.decision_id;
      IF NOT FOUND OR d.principal_id<>NEW.principal_id OR d.policy_id<>NEW.policy_id OR d.job_id IS DISTINCT FROM NEW.job_id THEN
        RAISE EXCEPTION 'route outcome differs from original execution' USING ERRCODE='55000'; END IF;
      PERFORM model_route_lock(d.workspace_id,d.policy_id);
      SELECT * INTO j FROM jobs WHERE workspace_id=d.workspace_id AND id=d.job_id AND owner_principal_id=d.principal_id;
      IF NOT FOUND OR j.state NOT IN ('succeeded','failed','cancelled','indeterminate')
        OR j.finished_at IS NULL OR (NEW.state,NEW.result,NEW.dispatched_at,NEW.finished_at)
          IS DISTINCT FROM (j.state,j.result,j.dispatched_at,j.finished_at) THEN
        RAISE EXCEPTION 'route outcome is not the original terminal job' USING ERRCODE='55000'; END IF;
      SELECT recipe->>'output_contract' INTO contract FROM model_route_prompt_recipes WHERE workspace_id=d.workspace_id AND policy_id=d.policy_id;
      IF NEW.output_contract IS DISTINCT FROM contract THEN
        RAISE EXCEPTION 'route outcome recipe differs' USING ERRCODE='55000'; END IF;
      IF j.job_type='model.generate' THEN
        SELECT COALESCE(string_agg(data->>'text','' ORDER BY sequence),'') INTO observed FROM job_events
          WHERE workspace_id=d.workspace_id AND job_id=d.job_id AND event_type='job.output';
        SELECT COALESCE(jsonb_agg(jsonb_build_object('id',h.id::text,'status',i.status,'finish_reason',i.result->>'finish_reason') ORDER BY h.ordinal),'[]'::jsonb) INTO receipts
          FROM hosted_requests h JOIN hosted_invocations i ON i.workspace_id=h.workspace_id AND i.id=h.id
          WHERE h.workspace_id=d.workspace_id AND h.job_id=d.job_id;
        IF NEW.native_evidence IS NOT NULL OR NEW.provider_receipts IS DISTINCT FROM receipts
          OR NEW.output_text IS DISTINCT FROM observed THEN
          RAISE EXCEPTION 'hosted outcome output or receipts differ' USING ERRCODE='55000'; END IF;
      ELSE
        IF NEW.provider_receipts IS DISTINCT FROM '[]'::jsonb THEN
          RAISE EXCEPTION 'native outcome has no hosted provider receipts' USING ERRCODE='55000'; END IF;
        NEW.native_evidence:=model_route_native_evidence(j,NEW.native_evidence,NEW.output_text);
      END IF;
      NEW.created_at:=clock_timestamp();RETURN NEW;
    END $$""",
    """CREATE OR REPLACE FUNCTION model_route_outcome_review_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE o model_route_outcomes%ROWTYPE;p model_route_policies%ROWTYPE;
    BEGIN
      PERFORM model_route_lock(NEW.workspace_id,NEW.policy_id);
      SELECT * INTO p FROM model_route_policies WHERE workspace_id=NEW.workspace_id AND id=NEW.policy_id;
      IF NOT FOUND OR p.reviewer_id<>NEW.reviewer_id THEN
        RAISE EXCEPTION 'independent route reviewer required' USING ERRCODE='55000'; END IF;
      SELECT * INTO o FROM model_route_outcomes WHERE workspace_id=NEW.workspace_id AND decision_id=NEW.decision_id;
      IF NOT FOUND OR o.policy_id<>NEW.policy_id OR o.outcome_sha256<>NEW.outcome_sha256
        OR NOT EXISTS(SELECT 1 FROM model_route_outcome_shares s WHERE s.workspace_id=o.workspace_id AND s.decision_id=o.decision_id AND s.reviewer_id=NEW.reviewer_id)
        OR (NEW.verdict='acceptable' AND (o.state<>'succeeded' OR o.dispatched_at IS NULL OR
          CASE WHEN o.native_evidence IS NOT NULL THEN
            o.native_evidence->>'finish_reason' IS DISTINCT FROM 'stop' OR
              (o.output_contract IS NOT NULL AND NOT (o.output_text IS JSON OBJECT WITH UNIQUE KEYS))
          ELSE o.result->>'completed_models' IS DISTINCT FROM '1' OR jsonb_array_length(o.provider_receipts)<>1
            OR o.provider_receipts->0->>'status' IS DISTINCT FROM 'completed' OR o.provider_receipts->0->>'finish_reason' IS DISTINCT FROM 'stop'
          END)) THEN
        RAISE EXCEPTION 'shared confirmed outcome required for this review' USING ERRCODE='55000'; END IF;
      NEW.created_at:=clock_timestamp();RETURN NEW;
    END $$""",
)
