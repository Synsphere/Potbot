"""Migration 38: S12 release evidence on the existing PostgreSQL approval owner.

Existing approvals are not automatically released or reclassified. A host hold
opts each changed installation into the gate before its filesystem swap.
"""

WORKSPACE_TABLES: tuple[str, ...] = ()
APPEND_ONLY_TABLES: tuple[str, ...] = ()

MIGRATION_STATEMENTS = (
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN release_phase text NOT NULL "
    "DEFAULT 'none' CHECK(release_phase IN "
    "('none','repinned','testing','failed','indeterminate','released'))",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN release_generation bigint NOT NULL "
    "DEFAULT 0 CHECK(release_generation>=0)",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN release_binding jsonb "
    "CHECK(release_binding IS NULL OR jsonb_typeof(release_binding)='object')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN release_evidence jsonb "
    "CHECK(release_evidence IS NULL OR jsonb_typeof(release_evidence)='object')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN release_mac text "
    "CHECK(release_mac IS NULL OR release_mac ~ '^[a-f0-9]{64}$')",
    "ALTER TABLE ui_tool_upgrade_approvals ADD COLUMN postflight_token uuid",
    "ALTER TABLE ui_tool_upgrade_approvals ADD CONSTRAINT tool_postflight_token_state "
    "CHECK((release_phase='testing')=(postflight_token IS NOT NULL))",
    "ALTER TABLE ui_tool_upgrade_approvals ADD CONSTRAINT tool_released_evidence "
    "CHECK(release_phase!='released' OR "
    "(release_binding IS NOT NULL AND release_evidence IS NOT NULL AND release_mac IS NOT NULL))",
    "ALTER TABLE ui_tool_upgrade_audit DROP CONSTRAINT ui_tool_upgrade_audit_action_check",
    "ALTER TABLE ui_tool_upgrade_audit ADD CONSTRAINT ui_tool_upgrade_audit_action_check "
    "CHECK(action IN ('published','approved','rejected','claimed','activated','indeterminate',"
    "'reconciled','release_repinned','postflight_started','postflight_failed',"
    "'postflight_indeterminate','released','rollback_released'))",
)
