"""Migration 46: immutable selected-task provenance on existing owned jobs."""

WORKSPACE_TABLES: tuple[str, ...] = ()
APPEND_ONLY_TABLES: tuple[str, ...] = ()

MIGRATION_STATEMENTS = (
    "ALTER TABLE task_discovery_selections ADD CONSTRAINT task_selection_owner_identity "
    "UNIQUE(workspace_id,principal_id,id)",
    "ALTER TABLE jobs ADD COLUMN task_selection_id uuid",
    "ALTER TABLE jobs ADD COLUMN task_execution_binding jsonb",
    "ALTER TABLE jobs ADD CONSTRAINT jobs_task_selection_owner "
    "FOREIGN KEY(workspace_id,owner_principal_id,task_selection_id) "
    "REFERENCES task_discovery_selections(workspace_id,principal_id,id)",
    "ALTER TABLE jobs ADD CONSTRAINT jobs_task_execution_shape CHECK ("
    "(task_selection_id IS NULL)=(task_execution_binding IS NULL) AND "
    "(task_selection_id IS NULL OR (owner_principal_id IS NOT NULL AND "
    "(job_type='operation.check' OR operation_check_id IS NOT NULL) AND "
    "jsonb_typeof(task_execution_binding)='object' AND "
    "octet_length(task_execution_binding::text)<=262144 AND "
    "(task_execution_binding->>'schema') IS NOT DISTINCT FROM 'potbot-task-execution-binding/v1' AND "
    "(task_execution_binding->>'selectionId') IS NOT DISTINCT FROM task_selection_id::text AND "
    "(task_execution_binding->>'workspaceId') IS NOT DISTINCT FROM workspace_id::text AND "
    "(task_execution_binding->>'principalId') IS NOT DISTINCT FROM owner_principal_id::text)))",
    "CREATE INDEX jobs_task_selection ON jobs(workspace_id,owner_principal_id,task_selection_id,created_at DESC,id DESC) "
    "WHERE task_selection_id IS NOT NULL",
    """CREATE FUNCTION protect_job_task_execution_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.task_selection_id IS DISTINCT FROM OLD.task_selection_id
           OR NEW.task_execution_binding IS DISTINCT FROM OLD.task_execution_binding THEN
            RAISE EXCEPTION 'job task execution identity is immutable' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
        END $$""",
    "CREATE TRIGGER jobs_task_execution_identity BEFORE UPDATE ON jobs "
    "FOR EACH ROW EXECUTE FUNCTION protect_job_task_execution_identity()",
)
