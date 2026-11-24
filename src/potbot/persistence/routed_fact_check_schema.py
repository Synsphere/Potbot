"""Migration 55: adopt only answers verified against the frozen reasoning plan."""

WORKSPACE_TABLES: tuple[str, ...] = ()
APPEND_ONLY_TABLES: tuple[str, ...] = ()

MIGRATION_STATEMENTS = (
    """CREATE OR REPLACE FUNCTION routed_reasoning_compilation_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE d model_route_decisions%ROWTYPE; r routed_reasoning_runs%ROWTYPE;
        outputs jsonb; context jsonb;
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
        RETURN NEW;
    END $$""",
    # Plain JSON preserves number spellings, so fractional and exponent forms
    # cannot pass as integers through JSONB numeric equality.
    """CREATE FUNCTION routed_reasoning_answer_matches(rule jsonb, raw text)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
    DECLARE answer json; expected jsonb; cheapest numeric; threshold numeric;
        integer_literal constant text:='^[[:space:]]*-?(0|[1-9][0-9]{0,15})[[:space:]]*$';
    BEGIN
        IF jsonb_typeof(rule) IS DISTINCT FROM 'object'
          OR raw IS NULL OR octet_length(raw) NOT BETWEEN 1 AND 16384
          OR NOT (raw IS JSON OBJECT WITH UNIQUE KEYS)
        THEN RETURN false; END IF;
        answer:=raw::json;
        CASE rule->>'kind'
        WHEN 'minimum_cost' THEN
            IF rule - ARRAY['kind','records','minimum_capacity'] <> '{}'::jsonb
              OR jsonb_typeof(rule->'records') IS DISTINCT FROM 'array'
              OR jsonb_typeof(rule->'minimum_capacity') IS DISTINCT FROM 'number'
              OR (rule->>'minimum_capacity') !~ '^(0|[1-9][0-9]{0,15})$'
              OR json_typeof(answer->'record_id') IS DISTINCT FROM 'string'
              OR NOT COALESCE((answer->'capacity')::text ~ integer_literal,false)
              OR NOT COALESCE((answer->'cost')::text ~ integer_literal,false)
              OR EXISTS (SELECT 1 FROM json_object_keys(answer) AS k(name)
                WHERE k.name NOT IN ('record_id','capacity','cost'))
            THEN RETURN false; END IF;
            threshold:=(rule->>'minimum_capacity')::numeric;
            IF threshold>9007199254740991
              OR jsonb_array_length(rule->'records') NOT BETWEEN 1 AND 32
              OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(rule->'records') AS r(value)
                WHERE CASE WHEN jsonb_typeof(r.value)<>'object' THEN true ELSE
                  r.value - ARRAY['id','capacity','cost'] <> '{}'::jsonb
                  OR jsonb_typeof(r.value->'id') IS DISTINCT FROM 'string'
                  OR char_length(r.value->>'id') NOT BETWEEN 1 AND 64
                  OR jsonb_typeof(r.value->'capacity') IS DISTINCT FROM 'number'
                  OR jsonb_typeof(r.value->'cost') IS DISTINCT FROM 'number'
                  OR (r.value->>'capacity') !~ '^(0|[1-9][0-9]{0,15})$'
                  OR (r.value->>'cost') !~ '^(0|[1-9][0-9]{0,15})$'
                END
              )
            THEN RETURN false; END IF;
            IF EXISTS (
                SELECT 1 FROM jsonb_array_elements(rule->'records') AS r(value)
                  WHERE (r.value->>'capacity')::numeric>9007199254740991
                     OR (r.value->>'cost')::numeric>9007199254740991
              ) OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(rule->'records') AS r(value)
                  GROUP BY r.value->>'id' HAVING count(*)>1
              )
            THEN RETURN false; END IF;
            SELECT min((r.value->>'cost')::numeric) INTO cheapest
              FROM jsonb_array_elements(rule->'records') AS r(value)
              WHERE (r.value->>'capacity')::numeric>=threshold;
            SELECT jsonb_build_object('record_id',r.value->'id',
                'capacity',r.value->'capacity','cost',r.value->'cost') INTO expected
              FROM jsonb_array_elements(rule->'records') AS r(value)
              WHERE r.value->'id'=(answer->'record_id')::jsonb
                AND (r.value->>'capacity')::numeric>=threshold
                AND (r.value->>'cost')::numeric=cheapest;
        WHEN 'counts' THEN
            IF rule - ARRAY['kind','labels','values'] <> '{}'::jsonb
              OR jsonb_typeof(rule->'labels') IS DISTINCT FROM 'array'
              OR jsonb_typeof(rule->'values') IS DISTINCT FROM 'array'
              OR json_typeof(answer->'counts') IS DISTINCT FROM 'object'
              OR EXISTS (SELECT 1 FROM json_object_keys(answer) AS k(name)
                WHERE k.name<>'counts')
            THEN RETURN false; END IF;
            IF jsonb_array_length(rule->'labels') NOT BETWEEN 1 AND 32
              OR jsonb_array_length(rule->'values')>32
              OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(rule->'labels') AS label(value)
                  WHERE jsonb_typeof(label.value)<>'string'
                    OR char_length(label.value #>> '{}') NOT BETWEEN 1 AND 64
              ) OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(rule->'labels') AS label(value)
                  GROUP BY label.value HAVING count(*)>1
              ) OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(rule->'values') AS sample(value)
                  WHERE NOT (rule->'labels' @> jsonb_build_array(sample.value))
              ) OR EXISTS (
                SELECT 1 FROM json_each(answer->'counts') AS entry(key,value)
                  WHERE entry.value::text !~ integer_literal
              )
            THEN RETURN false; END IF;
            SELECT jsonb_build_object('counts',jsonb_object_agg(t.label,t.frequency))
              INTO expected FROM (
                SELECT label.value AS label, count(observation.value) AS frequency
                  FROM jsonb_array_elements_text(rule->'labels') AS label(value)
                  LEFT JOIN jsonb_array_elements_text(rule->'values') AS observation(value)
                    ON label.value=observation.value
                  GROUP BY label.value
              ) AS t;
        ELSE RETURN false;
        END CASE;
        RETURN COALESCE(answer::jsonb=expected,false);
    END $$""",
    # The existing run guard verifies the phase transition and original job first.
    # The completion guard reads the same job without taking additional locks.
    """CREATE FUNCTION routed_reasoning_verified_completion()
        RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE j jobs%ROWTYPE; observed text; confirmed boolean; receipt jsonb;
    BEGIN
        IF NEW.state<>'completed' OR NOT (OLD.plan ? 'answer_check') THEN RETURN NEW; END IF;
        receipt:=NEW.results->2;
        SELECT job.* INTO j FROM model_route_decisions d JOIN jobs job
          ON job.workspace_id=d.workspace_id AND job.id=d.job_id
            AND job.owner_principal_id=d.principal_id
          JOIN model_route_compiled_prompts c ON c.workspace_id=d.workspace_id
            AND c.principal_id=d.principal_id AND c.decision_id=d.id
          WHERE d.workspace_id=OLD.workspace_id AND d.principal_id=OLD.principal_id
            AND d.id::text=receipt->>'decision_id'
            AND d.allocation_key='rrun-v1:'||OLD.id::text||':2'
            AND c.facts->'context'->'answer_check'=OLD.plan->'answer_check'
            AND job.job_type='model.generate' AND job.output_bytes BETWEEN 1 AND 16384;
        IF NOT FOUND THEN
          RAISE EXCEPTION 'routed reasoning verification requires its checked hosted answer'
            USING ERRCODE='55000';
        END IF;
        SELECT count(*)=1 AND bool_and(i.status='completed'
            AND i.result->>'finish_reason'='stop'
            AND i.result->>'model'=j.selected_models->0->>'model'
            AND i.result->>'provider'=j.selected_models->0->>'provider'
            AND i.result->>'role'=j.selected_models->0->>'role')
          AND jsonb_agg(h.id::text ORDER BY h.ordinal)=j.result->'invocation_ids'
          INTO confirmed FROM hosted_requests h JOIN hosted_invocations i
            ON i.workspace_id=h.workspace_id AND i.id=h.id
          WHERE h.workspace_id=j.workspace_id AND h.job_id=j.id;
        SELECT COALESCE(string_agg(data->>'text','' ORDER BY sequence),'') INTO observed
          FROM job_events
          WHERE workspace_id=j.workspace_id AND job_id=j.id AND event_type='job.output';
        IF confirmed IS DISTINCT FROM true OR octet_length(observed)<>j.output_bytes
          OR receipt->>'output_sha256' IS DISTINCT FROM
            encode(sha256(convert_to(to_json(observed)::text,'UTF8')),'hex')
          OR NOT routed_reasoning_answer_matches(OLD.plan->'answer_check',observed)
        THEN RAISE EXCEPTION 'routed reasoning answer does not match its frozen facts'
          USING ERRCODE='55000'; END IF;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER routed_reasoning_verified_completion
        BEFORE UPDATE ON routed_reasoning_runs
        FOR EACH ROW EXECUTE FUNCTION routed_reasoning_verified_completion()""",
)
