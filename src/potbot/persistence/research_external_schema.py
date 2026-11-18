"""Migration 47: budgeted external attempts on the existing research queue.

Research and its content-addressed evidence remain workspace shared. Immutable
admission, per-send and response receipts retain the original operator and never
create an independently replayable network queue.
"""

WORKSPACE_TABLES = ("research_external_requests", "research_external_responses")
APPEND_ONLY_TABLES = WORKSPACE_TABLES

MIGRATION_STATEMENTS = (
    "ALTER TABLE research_runs ADD COLUMN execution_kind text NOT NULL DEFAULT 'local' "
    "CHECK(execution_kind IN ('local','external'))",
    "ALTER TABLE research_runs ADD COLUMN external_actor_id uuid",
    "ALTER TABLE research_runs ADD COLUMN external_authorization jsonb",
    "ALTER TABLE research_runs ADD COLUMN external_mac text",
    "ALTER TABLE research_runs ADD COLUMN external_reservation_id uuid",
    "ALTER TABLE research_runs ADD COLUMN external_decision_id uuid",
    "ALTER TABLE research_runs ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false",
    "ALTER TABLE research_runs ADD COLUMN revision bigint NOT NULL DEFAULT 1 CHECK(revision>=1)",
    "ALTER TABLE research_runs ADD CONSTRAINT research_external_actor_fk "
    "FOREIGN KEY(workspace_id,external_actor_id) "
    "REFERENCES workspace_memberships(workspace_id,principal_id)",
    "ALTER TABLE research_runs ADD CONSTRAINT research_external_reservation_fk "
    "FOREIGN KEY(workspace_id,external_reservation_id) REFERENCES budget_reservations(workspace_id,id)",
    "ALTER TABLE research_runs ADD CONSTRAINT research_external_decision_fk "
    "FOREIGN KEY(workspace_id,external_decision_id) REFERENCES evidence_events(workspace_id,id)",
    """ALTER TABLE research_runs ADD CONSTRAINT research_external_shape CHECK (
        (execution_kind='local' AND external_actor_id IS NULL
          AND external_authorization IS NULL AND external_mac IS NULL
          AND external_reservation_id IS NULL AND external_decision_id IS NULL)
        OR (execution_kind='external' AND external_actor_id IS NOT NULL
          AND external_reservation_id IS NOT NULL AND external_decision_id IS NOT NULL
          AND external_mac IS NOT NULL AND external_mac ~ '^[0-9a-f]{64}$'
          AND jsonb_typeof(external_authorization)='object'
          AND octet_length(external_authorization::text)<=65536
          AND (external_authorization->>'schema') IS NOT DISTINCT FROM
              'potbot-external-research-authorization/v1'
          AND (external_authorization->>'workspaceId') IS NOT DISTINCT FROM workspace_id::text
          AND (external_authorization->>'runId') IS NOT DISTINCT FROM id::text
          AND (external_authorization->>'actionId') IS NOT DISTINCT FROM action_id::text
          AND (external_authorization->>'actorPrincipalId') IS NOT DISTINCT FROM external_actor_id::text
          AND (external_authorization->>'reservationId') IS NOT DISTINCT FROM external_reservation_id::text
          AND (external_authorization->>'disclosureDecisionId') IS NOT DISTINCT FROM external_decision_id::text))""",
    """CREATE FUNCTION protect_research_external_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF (NEW.execution_kind,NEW.external_actor_id,NEW.external_authorization,
            NEW.external_mac,NEW.external_reservation_id,NEW.external_decision_id,
            NEW.workspace_id,NEW.id,NEW.episode_id,NEW.action_id,
            NEW.source_artifact_id,NEW.action_identity,NEW.source_revision)
            IS DISTINCT FROM
           (OLD.execution_kind,OLD.external_actor_id,OLD.external_authorization,
            OLD.external_mac,OLD.external_reservation_id,OLD.external_decision_id,
            OLD.workspace_id,OLD.id,OLD.episode_id,OLD.action_id,
            OLD.source_artifact_id,OLD.action_identity,OLD.source_revision) THEN
          RAISE EXCEPTION 'research admission identity is immutable' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
        END $$""",
    "CREATE TRIGGER research_external_identity BEFORE UPDATE ON research_runs "
    "FOR EACH ROW EXECUTE FUNCTION protect_research_external_identity()",
    "CREATE INDEX research_external_queue ON research_runs(workspace_id,execution_kind,state,created_at,id)",
    """CREATE TABLE research_external_requests (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        run_id uuid NOT NULL, ordinal integer NOT NULL CHECK(ordinal BETWEEN 1 AND 32),
        request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
        rate_key text NOT NULL CHECK(length(rate_key) BETWEEN 1 AND 300),
        event_id uuid NOT NULL,
        content jsonb NOT NULL CHECK(jsonb_typeof(content)='object' AND octet_length(content::text)<=65536),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,run_id,id),
        UNIQUE(workspace_id,run_id,ordinal), UNIQUE(workspace_id,run_id,request_sha256),
        FOREIGN KEY(workspace_id,episode_id,run_id) REFERENCES research_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,event_id) REFERENCES syn_execution_events(workspace_id,id)
    )""",
    "CREATE INDEX research_external_rate ON research_external_requests(workspace_id,rate_key,created_at DESC)",
    """CREATE TABLE research_external_responses (
        workspace_id uuid NOT NULL, id uuid NOT NULL, episode_id uuid NOT NULL,
        run_id uuid NOT NULL, request_id uuid NOT NULL, raw_artifact_id uuid,
        response_sha256 text NOT NULL CHECK(response_sha256 ~ '^[0-9a-f]{64}$'),
        content jsonb NOT NULL CHECK(jsonb_typeof(content)='object' AND octet_length(content::text)<=65536),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,request_id),
        FOREIGN KEY(workspace_id,episode_id,run_id) REFERENCES research_runs(workspace_id,episode_id,id),
        FOREIGN KEY(workspace_id,run_id,request_id) REFERENCES research_external_requests(workspace_id,run_id,id),
        FOREIGN KEY(workspace_id,raw_artifact_id) REFERENCES artifacts(workspace_id,id)
    )""",
)
