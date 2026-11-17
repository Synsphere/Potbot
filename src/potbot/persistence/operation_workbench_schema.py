"""Migration 39: private operation checks on the existing durable job queue.

Legacy ownership is recovered only from an unambiguous immutable submit receipt.
Unknown ownership is retained as NULL and unavailable to application principals.
No installation, qualification, or permission is manufactured by migration.
"""

WORKSPACE_TABLES: tuple[str, ...] = ()
APPEND_ONLY_TABLES: tuple[str, ...] = ()

MIGRATION_STATEMENTS = (
    "ALTER TABLE jobs DROP CONSTRAINT jobs_job_type_check",
    "ALTER TABLE jobs ADD CONSTRAINT jobs_job_type_check "
    "CHECK(job_type IN ('model.generate','cog.execute','operation.check') "
    "AND (job_type='model.generate' OR selected_models='[]'::jsonb))",
    "ALTER TABLE jobs ADD COLUMN owner_principal_id uuid",
    "ALTER TABLE jobs ADD CONSTRAINT jobs_owner_membership "
    "FOREIGN KEY(workspace_id,owner_principal_id) "
    "REFERENCES workspace_memberships(workspace_id,principal_id)",
    """UPDATE jobs j SET owner_principal_id=owned.principal_id FROM (
        SELECT workspace_id,result->>'id' AS job_id,
               (array_agg(DISTINCT principal_id))[1] AS principal_id
        FROM command_results WHERE operation='submit_api_job'
        GROUP BY workspace_id,result->>'id'
        HAVING count(DISTINCT principal_id)=1
    ) owned WHERE owned.workspace_id=j.workspace_id AND owned.job_id=j.id::text""",
    "ALTER TABLE jobs ADD COLUMN operation_check_id uuid",
    "ALTER TABLE jobs ADD COLUMN operation_authorization jsonb "
    "CHECK(operation_authorization IS NULL OR jsonb_typeof(operation_authorization)='object')",
    "ALTER TABLE jobs ADD CONSTRAINT jobs_owner_identity "
    "UNIQUE(workspace_id,owner_principal_id,id)",
    "ALTER TABLE jobs ADD CONSTRAINT jobs_operation_check_owner "
    "FOREIGN KEY(workspace_id,owner_principal_id,operation_check_id) "
    "REFERENCES jobs(workspace_id,owner_principal_id,id)",
    "ALTER TABLE jobs ADD CONSTRAINT jobs_operation_check_shape "
    "CHECK((job_type!='operation.check' OR (owner_principal_id IS NOT NULL "
    "AND operation_check_id IS NULL AND operation_authorization IS NULL)) "
    "AND (operation_check_id IS NULL)=(operation_authorization IS NULL))",
    "CREATE UNIQUE INDEX jobs_one_run_per_operation_check "
    "ON jobs(workspace_id,operation_check_id) WHERE operation_check_id IS NOT NULL",
    "CREATE INDEX jobs_owner_queue ON jobs(workspace_id,owner_principal_id,state,created_at,id)",
    """CREATE FUNCTION protect_job_operation_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.owner_principal_id IS DISTINCT FROM OLD.owner_principal_id
           OR NEW.operation_check_id IS DISTINCT FROM OLD.operation_check_id
           OR NEW.operation_authorization IS DISTINCT FROM OLD.operation_authorization
           OR ((OLD.job_type='operation.check' OR OLD.operation_check_id IS NOT NULL)
               AND (NEW.job_type IS DISTINCT FROM OLD.job_type
                 OR NEW.episode_id IS DISTINCT FROM OLD.episode_id
                 OR NEW.contract_revision_id IS DISTINCT FROM OLD.contract_revision_id
                 OR NEW.input IS DISTINCT FROM OLD.input
                 OR NEW.selected_models IS DISTINCT FROM OLD.selected_models)) THEN
            RAISE EXCEPTION 'job operation identity is immutable';
        END IF;
        RETURN NEW;
        END $$""",
    "CREATE TRIGGER jobs_operation_identity BEFORE UPDATE ON jobs "
    "FOR EACH ROW EXECUTE FUNCTION protect_job_operation_identity()",
    """CREATE POLICY jobs_principal_isolation ON jobs AS RESTRICTIVE FOR ALL
        USING (owner_principal_id =
            NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)
        WITH CHECK (owner_principal_id =
            NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid)""",
    """CREATE POLICY job_events_principal_isolation ON job_events AS RESTRICTIVE FOR ALL
        USING (EXISTS(SELECT 1 FROM jobs j WHERE j.workspace_id=job_events.workspace_id
            AND j.id=job_events.job_id))
        WITH CHECK (EXISTS(SELECT 1 FROM jobs j WHERE j.workspace_id=job_events.workspace_id
            AND j.id=job_events.job_id))""",
)
