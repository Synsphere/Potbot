"""Migration41: source/build admission on the existing S11/S12 operation owner.

Recipes are immutable operator decisions, not another installed-tool registry.
Pretest rows cannot satisfy the existing tested-approval or release contracts.
"""

WORKSPACE_TABLES = ("ui_tool_acquisition_recipes",)
APPEND_ONLY_TABLES = WORKSPACE_TABLES
_PRINCIPAL = "NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid"

MIGRATION_STATEMENTS = (
    """CREATE TABLE ui_tool_acquisition_recipes (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        principal_id uuid NOT NULL,
        agent_id uuid NOT NULL,
        recipe_sha256 text NOT NULL CHECK(recipe_sha256 ~ '^[0-9a-f]{64}$'),
        content jsonb NOT NULL CHECK(jsonb_typeof(content)='object'),
        approval_mac text NOT NULL CHECK(approval_mac ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    f"""CREATE POLICY tool_recipe_owner ON ui_tool_acquisition_recipes
        AS RESTRICTIVE FOR ALL USING(principal_id={_PRINCIPAL})
        WITH CHECK(principal_id={_PRINCIPAL})""",
    "CREATE INDEX tool_recipe_owner ON ui_tool_acquisition_recipes(workspace_id,principal_id,id)",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN acquisition_phase text NOT NULL "
    "DEFAULT 'none' CHECK(acquisition_phase IN ('none','source_queued','sourcing','review',"
    "'build_queued','building','tested','failed','indeterminate','cancelled'))",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN acquisition_kind text "
    "CHECK(acquisition_kind IN ('install','upgrade'))",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN recipe_id uuid",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN recipe_sha256 text "
    "CHECK(recipe_sha256 IS NULL OR recipe_sha256 ~ '^[0-9a-f]{64}$')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN source_request_mac text "
    "CHECK(source_request_mac IS NULL OR source_request_mac ~ '^[0-9a-f]{64}$')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN source_receipt jsonb "
    "CHECK(source_receipt IS NULL OR jsonb_typeof(source_receipt)='object')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN execution_contract jsonb "
    "CHECK(execution_contract IS NULL OR jsonb_typeof(execution_contract)='object')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN review_sha256 text "
    "CHECK(review_sha256 IS NULL OR review_sha256 ~ '^[0-9a-f]{64}$')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN build_authorization jsonb "
    "CHECK(build_authorization IS NULL OR jsonb_typeof(build_authorization)='object')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN build_receipt jsonb "
    "CHECK(build_receipt IS NULL OR jsonb_typeof(build_receipt)='object')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN acquisition_worker_token uuid",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN host_action jsonb "
    "CHECK(host_action IS NULL OR COALESCE((jsonb_typeof(host_action)='object' "
    "AND host_action->>'action' IN ('rollback','reconcile','repin','postflight') "
    "AND host_action->>'status' IN ('queued','running','failed','indeterminate')),false))",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN acquisition_error text "
    "CHECK(acquisition_error IS NULL OR acquisition_error ~ '^[a-z][a-z0-9_]{0,63}$')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD CONSTRAINT acquisition_recipe "
    "FOREIGN KEY(workspace_id,recipe_id) REFERENCES ui_tool_acquisition_recipes(workspace_id,id)",
    "ALTER TABLE ui_tool_upgrade_approvals ALTER COLUMN old_commit DROP NOT NULL",
    "ALTER TABLE ui_tool_upgrade_approvals ALTER COLUMN evidence_sha256 DROP NOT NULL",
    "ALTER TABLE ui_tool_upgrade_approvals DROP CONSTRAINT ui_tool_upgrade_approvals_state_check",
    "ALTER TABLE ui_tool_upgrade_approvals DROP CONSTRAINT ui_tool_upgrade_approvals_passed_tests_check",
    "ALTER TABLE ui_tool_upgrade_approvals DROP CONSTRAINT ui_tool_upgrade_approvals_tests_check",
    """DO $$ DECLARE item record; BEGIN
        FOR item IN SELECT conname FROM pg_constraint
          WHERE conrelid='ui_tool_upgrade_approvals'::regclass AND contype='c'
            AND pg_get_constraintdef(oid) LIKE '%old_commit%'
        LOOP EXECUTE format('ALTER TABLE ui_tool_upgrade_approvals DROP CONSTRAINT %I',item.conname);
        END LOOP; END $$""",
    """ALTER TABLE ui_tool_upgrade_approvals ADD CONSTRAINT tool_operation_state CHECK(
        state IN ('acquiring','awaiting_approval','approved','rejected','executing',
            'awaiting_repin','active','indeterminate','rolled_back')
        AND (state='acquiring')=(acquisition_phase NOT IN ('none','tested')))""",
    """ALTER TABLE ui_tool_upgrade_approvals ADD CONSTRAINT tool_test_evidence_phase CHECK(
        (acquisition_phase IN ('none','tested','cancelled') AND evidence_sha256 IS NOT NULL
            AND passed_tests BETWEEN 2 AND 16 AND jsonb_typeof(tests)='array'
            AND jsonb_array_length(tests)=passed_tests)
        OR (acquisition_phase NOT IN ('none','tested') AND evidence_sha256 IS NULL
            AND passed_tests=0 AND tests='[]'::jsonb))""",
    """ALTER TABLE ui_tool_upgrade_approvals ADD CONSTRAINT tool_source_predecessor CHECK(
        (acquisition_kind IS NOT NULL AND acquisition_kind='install' AND old_commit IS NULL)
        OR (acquisition_kind IS NOT NULL AND acquisition_kind='upgrade' AND old_commit IS NOT NULL
            AND old_commit ~ '^[0-9a-f]{40}$' AND old_commit!=new_commit)
        OR (acquisition_kind IS NULL AND (
            (acquisition_phase='none' AND old_commit IS NOT NULL
                AND old_commit ~ '^[0-9a-f]{40}$' AND old_commit!=new_commit)
            OR (acquisition_phase IN ('source_queued','sourcing','failed','indeterminate','cancelled')
                AND old_commit IS NULL))))""",
    """ALTER TABLE ui_tool_upgrade_approvals ADD CONSTRAINT tool_acquisition_owner CHECK(
        (acquisition_phase='none' AND recipe_id IS NULL AND recipe_sha256 IS NULL
            AND source_request_mac IS NULL AND source_receipt IS NULL
            AND execution_contract IS NULL AND review_sha256 IS NULL
            AND build_authorization IS NULL AND build_receipt IS NULL
            AND acquisition_worker_token IS NULL AND host_action IS NULL)
        OR (acquisition_phase!='none' AND recipe_id IS NOT NULL AND recipe_sha256 IS NOT NULL
            AND source_request_mac IS NOT NULL))""",
    """ALTER TABLE ui_tool_upgrade_approvals ADD CONSTRAINT tool_acquisition_worker CHECK(
        (acquisition_worker_token IS NOT NULL)=(acquisition_phase IN ('sourcing','building')
            OR COALESCE(host_action->>'status'='running',false)))""",
    "ALTER TABLE ui_tool_upgrade_approvals DROP CONSTRAINT ui_tool_upgrade_approvals_release_phase_check",
    "ALTER TABLE ui_tool_upgrade_approvals ADD CONSTRAINT tool_release_phase CHECK("
    "release_phase IN ('none','repinned','testing','failed','indeterminate','released','withdrawn'))",
    "ALTER TABLE ui_tool_upgrade_approvals DROP CONSTRAINT tool_released_evidence",
    """ALTER TABLE ui_tool_upgrade_approvals ADD CONSTRAINT tool_released_evidence CHECK(
        release_phase NOT IN ('released','withdrawn') OR (release_binding IS NOT NULL
            AND release_evidence IS NOT NULL AND release_mac IS NOT NULL))""",
    "ALTER TABLE ui_tool_upgrade_audit DROP CONSTRAINT ui_tool_upgrade_audit_action_check",
    "ALTER TABLE ui_tool_upgrade_audit ADD CONSTRAINT ui_tool_upgrade_audit_action_check "
    "CHECK(action IN ('published','approved','rejected','claimed','activated','indeterminate',"
    "'reconciled','release_repinned','postflight_started','postflight_failed',"
    "'postflight_indeterminate','released','rollback_released','withdrawn',"
    "'source_requested','source_claimed','source_ready','build_approved','build_claimed',"
    "'build_tested','acquisition_failed','acquisition_indeterminate','acquisition_cancelled',"
    "'host_action_requested','host_action_claimed','host_action_finished','host_action_failed'))",
    "CREATE INDEX tool_acquisition_claim ON ui_tool_upgrade_approvals "
    "(workspace_id,principal_id,agent_id,acquisition_phase,created_at,id)",
    "CREATE UNIQUE INDEX tool_acquisition_active_slug ON ui_tool_upgrade_approvals "
    "(workspace_id,slug) WHERE acquisition_phase NOT IN ('none','cancelled','failed') "
    "AND (release_phase NOT IN ('released','withdrawn') "
    "OR host_action->>'status' IN ('queued','running'))",
    """CREATE FUNCTION check_tool_acquisition_transition() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF OLD.acquisition_phase!='none' AND (
            (NEW.workspace_id,NEW.id,NEW.principal_id,NEW.agent_id,NEW.slug,NEW.new_commit,
                NEW.recipe_id,NEW.recipe_sha256,NEW.source_request_mac,NEW.created_at)
            IS DISTINCT FROM
            (OLD.workspace_id,OLD.id,OLD.principal_id,OLD.agent_id,OLD.slug,OLD.new_commit,
                OLD.recipe_id,OLD.recipe_sha256,OLD.source_request_mac,OLD.created_at)
            OR NEW.acquisition_phase='none' OR NEW.revision!=OLD.revision+1
            OR (OLD.source_receipt IS NOT NULL AND
                (NEW.source_receipt,NEW.execution_contract,NEW.review_sha256,NEW.old_commit,NEW.acquisition_kind)
                IS DISTINCT FROM
                (OLD.source_receipt,OLD.execution_contract,OLD.review_sha256,OLD.old_commit,OLD.acquisition_kind))
            OR (OLD.build_authorization IS NOT NULL AND NEW.build_authorization IS DISTINCT FROM OLD.build_authorization)
            OR (OLD.build_receipt IS NOT NULL AND NEW.build_receipt IS DISTINCT FROM OLD.build_receipt)
            OR (OLD.acquisition_phase='tested' AND (NEW.acquisition_phase NOT IN ('tested','cancelled') OR
                (NEW.evidence_sha256,NEW.suite_sha256,NEW.passed_tests,NEW.tests)
                IS DISTINCT FROM
                (OLD.evidence_sha256,OLD.suite_sha256,OLD.passed_tests,OLD.tests))))
        THEN RAISE EXCEPTION 'invalid acquisition lifecycle transition' USING ERRCODE='55000'; END IF;
        RETURN NEW; END $$""",
    "CREATE TRIGGER tool_acquisition_transition BEFORE UPDATE ON ui_tool_upgrade_approvals "
    "FOR EACH ROW EXECUTE FUNCTION check_tool_acquisition_transition()",
)
