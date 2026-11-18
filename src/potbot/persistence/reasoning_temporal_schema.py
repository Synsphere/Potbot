"""Migration 48: temporal control on the existing fenced reasoning owner.

Dispatch records describe possible effects, not a second worker queue. Source
components are immutable; only the fenced owner records their decisions.
"""

WORKSPACE_TABLES = ("reasoning_runtime_dispatches",)
APPEND_ONLY_TABLES: tuple[str, ...] = ()

MIGRATION_STATEMENTS = (
    "ALTER TABLE reasoning_runtime_runs ADD COLUMN interrupt_requested boolean NOT NULL DEFAULT false",
    "ALTER TABLE reasoning_runtime_runs ADD COLUMN context_epoch bigint NOT NULL DEFAULT 0 CHECK(context_epoch>=0)",
    "ALTER TABLE reasoning_runtime_runs ADD COLUMN active_dispatch_id uuid",
    "ALTER TABLE reasoning_runtime_runs DROP CONSTRAINT reasoning_runtime_runs_state_check",
    "ALTER TABLE reasoning_runtime_runs ADD CONSTRAINT reasoning_runtime_runs_state_check CHECK(state IN ('running','completed','failed','indeterminate','interrupted'))",
    "ALTER TABLE reasoning_runtime_steering ADD COLUMN principal_id uuid",
    "ALTER TABLE reasoning_runtime_steering ADD COLUMN components jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(components)='array' AND jsonb_array_length(components)<=16)",
    "ALTER TABLE reasoning_runtime_steering ADD COLUMN component_results jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(component_results)='object')",
    "ALTER TABLE reasoning_runtime_steering ADD CONSTRAINT reasoning_steering_principal FOREIGN KEY(workspace_id,principal_id) REFERENCES workspace_memberships(workspace_id,principal_id)",
    """CREATE TABLE reasoning_runtime_dispatches (
        workspace_id uuid NOT NULL,
        run_id uuid NOT NULL,
        id uuid NOT NULL,
        node_id uuid NOT NULL,
        lease_token uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN ('model','evidence')),
        context_epoch bigint NOT NULL CHECK(context_epoch>=0),
        status text NOT NULL CHECK(status IN ('dispatched','completed','indeterminate','not_started')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        finished_at timestamptz,
        PRIMARY KEY(workspace_id,run_id,id),
        FOREIGN KEY(workspace_id,run_id) REFERENCES reasoning_runtime_runs(workspace_id,id),
        CHECK((status='dispatched')=(finished_at IS NULL))
    )""",
    "CREATE INDEX reasoning_runtime_dispatch_history ON reasoning_runtime_dispatches(workspace_id,run_id,created_at,id)",
    """CREATE FUNCTION reasoning_temporal_sources_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
         OR NEW.run_id IS DISTINCT FROM OLD.run_id
         OR NEW.id IS DISTINCT FROM OLD.id
         OR NEW.request_id IS DISTINCT FROM OLD.request_id
         OR NEW.prompt IS DISTINCT FROM OLD.prompt
         OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
         OR NEW.components IS DISTINCT FROM OLD.components
         OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'reasoning steering source is immutable';
      END IF;
      IF NOT NEW.component_results @> OLD.component_results THEN
        RAISE EXCEPTION 'reasoning component decisions are immutable';
      END IF;
      RETURN NEW;
    END $$""",
    "CREATE TRIGGER reasoning_temporal_sources_immutable BEFORE UPDATE ON reasoning_runtime_steering FOR EACH ROW EXECUTE FUNCTION reasoning_temporal_sources_immutable()",
)
