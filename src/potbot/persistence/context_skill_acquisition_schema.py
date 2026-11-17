"""Migration 40: operator-admitted portable packages in the existing skill registry.

Authored skill sessions remain genuine. Acquired versions instead reference an
immutable owner-scoped package; current control MACs are independently verified
by the runtime before loading any imported instruction or resource.
"""

WORKSPACE_TABLES = (
    "workspace_context_skill_packages",
    "workspace_context_skill_package_files",
    "workspace_context_skill_controls",
)
APPEND_ONLY_TABLES = WORKSPACE_TABLES
_PRINCIPAL = "NULLIF(pg_catalog.current_setting('potbot.principal_id',true),'')::uuid"
_OWNER = f"owner_principal_id={_PRINCIPAL}"
_AUTHORED = f"""origin='authored' AND {_OWNER} AND EXISTS (
    SELECT 1 FROM workspace_context_objects a
    WHERE a.workspace_id=workspace_context_skills.workspace_id
      AND a.id=workspace_context_skills.agent_object_id AND a.kind='agent'
      AND a.name=workspace_context_skills.owner_agent_id::text
      AND a.visibility='private' AND a.owner_principal_id={_PRINCIPAL})"""
_ACQUIRED = f"""origin='acquired' AND {_OWNER} AND owner_agent_id IS NULL
    AND agent_object_id IS NULL"""

MIGRATION_STATEMENTS = (
    """CREATE TABLE workspace_context_skill_packages (
        workspace_id uuid NOT NULL,
        id uuid NOT NULL,
        owner_principal_id uuid NOT NULL,
        name text NOT NULL CHECK(length(name) BETWEEN 1 AND 64),
        description text NOT NULL CHECK(octet_length(description)<=8192),
        release_sha256 text NOT NULL CHECK(release_sha256 ~ '^[0-9a-f]{64}$'),
        content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
        source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
        manifest jsonb NOT NULL CHECK(jsonb_typeof(manifest)='object'),
        source jsonb NOT NULL CHECK(jsonb_typeof(source)='object'),
        total_bytes bigint NOT NULL CHECK(total_bytes BETWEEN 1 AND 8388608),
        file_count integer NOT NULL CHECK(file_count BETWEEN 1 AND 128),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,id),
        FOREIGN KEY(workspace_id,owner_principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id)
    )""",
    "CREATE INDEX skill_package_owner ON workspace_context_skill_packages "
    "(workspace_id,owner_principal_id,id)",
    """CREATE TABLE workspace_context_skill_package_files (
        workspace_id uuid NOT NULL,
        package_id uuid NOT NULL,
        path text NOT NULL CHECK(octet_length(path) BETWEEN 1 AND 512),
        sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
        byte_length integer NOT NULL CHECK(byte_length BETWEEN 0 AND 1048576),
        content bytea NOT NULL CHECK(octet_length(content)=byte_length),
        PRIMARY KEY(workspace_id,package_id,path),
        FOREIGN KEY(workspace_id,package_id)
            REFERENCES workspace_context_skill_packages(workspace_id,id)
    )""",
    "ALTER TABLE workspace_context_skills ADD COLUMN origin text NOT NULL DEFAULT 'authored' "
    "CHECK(origin IN ('authored','acquired'))",
    "ALTER TABLE workspace_context_skills ADD COLUMN enabled boolean NOT NULL DEFAULT true",
    "ALTER TABLE workspace_context_skills ADD COLUMN control_revision bigint NOT NULL DEFAULT 0 "
    "CHECK(control_revision BETWEEN 0 AND 9007199254740991)",
    "ALTER TABLE workspace_context_skills ADD COLUMN control_mac text "
    "CHECK(control_mac IS NULL OR control_mac ~ '^[0-9a-f]{64}$')",
    "ALTER TABLE workspace_context_skills ADD COLUMN current_package_id uuid",
    "ALTER TABLE workspace_context_skills ADD CONSTRAINT skill_current_package "
    "FOREIGN KEY(workspace_id,current_package_id) "
    "REFERENCES workspace_context_skill_packages(workspace_id,id)",
    "ALTER TABLE workspace_context_skills ALTER COLUMN owner_agent_id DROP NOT NULL",
    "ALTER TABLE workspace_context_skills ALTER COLUMN agent_object_id DROP NOT NULL",
    "ALTER TABLE workspace_context_skills DROP CONSTRAINT workspace_context_skills_slug_check",
    "ALTER TABLE workspace_context_skills DROP CONSTRAINT workspace_context_skills_state_check",
    """DO $$ DECLARE item record; BEGIN
        FOR item IN SELECT conname FROM pg_constraint
          WHERE conrelid='workspace_context_skills'::regclass AND contype='c'
            AND pg_get_constraintdef(oid) LIKE '%context_name%'
        LOOP EXECUTE format('ALTER TABLE workspace_context_skills DROP CONSTRAINT %I',item.conname);
        END LOOP; END $$""",
    """ALTER TABLE workspace_context_skills ADD CONSTRAINT skill_origin_identity CHECK(
        (origin='authored' AND owner_agent_id IS NOT NULL AND agent_object_id IS NOT NULL
            AND slug ~ '^[a-z][a-z0-9-]{1,62}$'
            AND context_name='agent-' || replace(owner_agent_id::text,'-','') || '.' || slug
            AND state IN ('active','archived') AND enabled AND control_revision=0
            AND control_mac IS NULL AND current_package_id IS NULL)
        OR (origin='acquired' AND owner_agent_id IS NULL AND agent_object_id IS NULL
            AND length(slug) BETWEEN 1 AND 64 AND slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
            AND context_name='imported-' || replace(owner_principal_id::text,'-','') || '.' || slug
            AND state IN ('active','revoked') AND published AND control_revision>0
            AND control_mac IS NOT NULL AND current_package_id IS NOT NULL
            AND (state!='revoked' OR NOT enabled)))""",
    "ALTER TABLE workspace_context_skill_versions ADD COLUMN package_id uuid",
    "ALTER TABLE workspace_context_skill_versions ALTER COLUMN session_id DROP NOT NULL",
    "ALTER TABLE workspace_context_skill_versions ADD CONSTRAINT skill_version_package "
    "FOREIGN KEY(workspace_id,package_id) REFERENCES workspace_context_skill_packages(workspace_id,id)",
    "ALTER TABLE workspace_context_skill_versions ADD CONSTRAINT skill_version_authority "
    "CHECK((session_id IS NULL)=(package_id IS NOT NULL))",
    "ALTER TABLE workspace_context_skill_releases ADD COLUMN content_sha256 text",
    "ALTER TABLE workspace_context_skill_releases DISABLE TRIGGER immutable_record",
    "UPDATE workspace_context_skill_releases SET content_sha256=sha256",
    "ALTER TABLE workspace_context_skill_releases ENABLE TRIGGER immutable_record",
    "ALTER TABLE workspace_context_skill_releases ALTER COLUMN content_sha256 SET NOT NULL",
    "ALTER TABLE workspace_context_skill_releases ADD CONSTRAINT skill_release_content_digest "
    "CHECK(content_sha256 ~ '^[0-9a-f]{64}$')",
    "ALTER TABLE workspace_context_skill_releases ADD COLUMN package_id uuid",
    "ALTER TABLE workspace_context_skill_releases ALTER COLUMN session_id DROP NOT NULL",
    "ALTER TABLE workspace_context_skill_releases ADD CONSTRAINT skill_release_package "
    "FOREIGN KEY(workspace_id,package_id) REFERENCES workspace_context_skill_packages(workspace_id,id)",
    "ALTER TABLE workspace_context_skill_releases ADD CONSTRAINT skill_release_authority "
    "CHECK((session_id IS NULL)=(package_id IS NOT NULL) "
    "AND (package_id IS NOT NULL OR sha256=content_sha256))",
    """DO $$ DECLARE item record; BEGIN
        FOR item IN SELECT conname FROM pg_constraint
          WHERE conrelid='workspace_context_skill_releases'::regclass AND contype='f'
            AND confrelid='workspace_context_skill_versions'::regclass
        LOOP EXECUTE format('ALTER TABLE workspace_context_skill_releases DROP CONSTRAINT %I',item.conname);
        END LOOP; END $$""",
    "ALTER TABLE workspace_context_skill_releases ADD CONSTRAINT skill_release_source_version "
    "FOREIGN KEY(workspace_id,skill_id,source_revision,content_sha256) "
    "REFERENCES workspace_context_skill_versions(workspace_id,skill_id,revision,sha256)",
    """CREATE TABLE workspace_context_skill_controls (
        workspace_id uuid NOT NULL,
        skill_id uuid NOT NULL,
        revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
        owner_principal_id uuid NOT NULL,
        action text NOT NULL CHECK(action IN ('install','update','activate','disable','rollback','revoke')),
        release_sha256 text NOT NULL CHECK(release_sha256 ~ '^[0-9a-f]{64}$'),
        package_id uuid NOT NULL,
        source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
        plan_sha256 text CHECK(plan_sha256 IS NULL OR plan_sha256 ~ '^[0-9a-f]{64}$'),
        state text NOT NULL CHECK(state IN ('active','revoked')),
        enabled boolean NOT NULL,
        mac text NOT NULL CHECK(mac ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(workspace_id,skill_id,revision),
        FOREIGN KEY(workspace_id,skill_id,release_sha256)
            REFERENCES workspace_context_skill_releases(workspace_id,skill_id,sha256),
        FOREIGN KEY(workspace_id,owner_principal_id)
            REFERENCES workspace_memberships(workspace_id,principal_id),
        FOREIGN KEY(workspace_id,package_id)
            REFERENCES workspace_context_skill_packages(workspace_id,id)
    )""",
    f"""ALTER POLICY skill_catalog_insert ON workspace_context_skills
        WITH CHECK (({_AUTHORED}) OR ({_ACQUIRED}))""",
    f"""ALTER POLICY skill_catalog_update ON workspace_context_skills
        USING ({_OWNER}) WITH CHECK (({_AUTHORED}) OR ({_ACQUIRED}))""",
    *tuple(
        f"""ALTER POLICY skill_author_insert ON {table} WITH CHECK (
            EXISTS(SELECT 1 FROM workspace_context_skills k
            WHERE k.workspace_id={table}.workspace_id AND k.id={table}.skill_id
              AND k.owner_principal_id={_PRINCIPAL} AND k.state='active'
              AND ((k.origin='authored' AND {table}.package_id IS NULL AND EXISTS(
                  SELECT 1 FROM workspace_context_sessions s
                  WHERE s.workspace_id=k.workspace_id AND s.agent_id=k.owner_agent_id
                    AND s.principal_id=k.owner_principal_id AND s.id={table}.session_id
                    AND s.state='active' AND s.expires_at>pg_catalog.clock_timestamp()))
                OR (k.origin='acquired' AND {table}.session_id IS NULL AND EXISTS(
                  SELECT 1 FROM workspace_context_skill_packages p
                  WHERE p.workspace_id=k.workspace_id AND p.id={table}.package_id
                    AND p.owner_principal_id=k.owner_principal_id AND p.name=k.slug
                    AND p.content_sha256={table}.{content_column}
                    {release_check})))))"""
        for table, content_column, release_check in (
            ("workspace_context_skill_versions", "sha256", ""),
            (
                "workspace_context_skill_releases",
                "content_sha256",
                "AND p.release_sha256=workspace_context_skill_releases.sha256",
            ),
        )
    ),
    f"""CREATE POLICY skill_package_read ON workspace_context_skill_packages
        AS RESTRICTIVE FOR SELECT USING ({_OWNER} OR EXISTS(
            SELECT 1 FROM workspace_context_skills k
            WHERE k.workspace_id=workspace_context_skill_packages.workspace_id
              AND k.current_package_id=workspace_context_skill_packages.id AND k.published
              AND k.origin='acquired' AND k.state='active' AND k.enabled))""",
    f"""CREATE POLICY skill_package_insert ON workspace_context_skill_packages
        AS RESTRICTIVE FOR INSERT WITH CHECK ({_OWNER})""",
    """CREATE POLICY skill_package_file_read ON workspace_context_skill_package_files
        AS RESTRICTIVE FOR SELECT USING (EXISTS(SELECT 1 FROM workspace_context_skill_packages p
            WHERE p.workspace_id=workspace_context_skill_package_files.workspace_id
              AND p.id=workspace_context_skill_package_files.package_id))""",
    f"""CREATE POLICY skill_package_file_insert ON workspace_context_skill_package_files
        AS RESTRICTIVE FOR INSERT WITH CHECK (EXISTS(SELECT 1 FROM workspace_context_skill_packages p
            WHERE p.workspace_id=workspace_context_skill_package_files.workspace_id
              AND p.id=workspace_context_skill_package_files.package_id
              AND p.owner_principal_id={_PRINCIPAL}))""",
    f"""CREATE POLICY skill_control_insert ON workspace_context_skill_controls
        AS RESTRICTIVE FOR INSERT WITH CHECK ({_OWNER} AND EXISTS(
            SELECT 1 FROM workspace_context_skills k
            WHERE k.workspace_id=workspace_context_skill_controls.workspace_id
              AND k.id=workspace_context_skill_controls.skill_id
              AND k.owner_principal_id={_PRINCIPAL} AND k.origin='acquired'))""",
    """CREATE OR REPLACE FUNCTION check_context_skill_transition() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF (NEW.workspace_id,NEW.id,NEW.owner_principal_id,NEW.owner_agent_id,
            NEW.agent_object_id,NEW.slug,NEW.context_object_id,NEW.context_name,NEW.created_at,NEW.origin)
            IS DISTINCT FROM
           (OLD.workspace_id,OLD.id,OLD.owner_principal_id,OLD.owner_agent_id,
            OLD.agent_object_id,OLD.slug,OLD.context_object_id,OLD.context_name,OLD.created_at,OLD.origin)
           OR OLD.state IN ('archived','revoked') OR (OLD.published AND NOT NEW.published)
           OR NEW.draft_revision NOT IN (OLD.draft_revision,OLD.draft_revision+1)
           OR (OLD.origin='acquired' AND NEW.control_revision!=OLD.control_revision+1)
        THEN RAISE EXCEPTION 'invalid skill lifecycle transition' USING ERRCODE='55000'; END IF;
        RETURN NEW; END $$""",
)
