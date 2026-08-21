-- Single-workspace account profile and creation preferences.
-- API key and session secrets remain in the tables created by 0001; only hashes are persisted.

BEGIN;

ALTER TABLE users
  ADD COLUMN avatar_url text,
  ADD COLUMN locale text NOT NULL DEFAULT 'zh-CN',
  ADD COLUMN timezone text NOT NULL DEFAULT 'Asia/Shanghai',
  ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
  ADD CONSTRAINT users_avatar_url_safe CHECK (
    avatar_url IS NULL OR avatar_url ~ '^https://'
    OR avatar_url ~ '^http://(localhost|127\.0\.0\.1)(:[0-9]+)?(/.*)?$'
  ),
  ADD CONSTRAINT users_locale_format CHECK (
    locale ~ '^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$'
  ),
  ADD CONSTRAINT users_timezone_not_blank CHECK (btrim(timezone) <> '');

CREATE TABLE creation_preferences (
  workspace_id uuid NOT NULL,
  user_id uuid NOT NULL,
  default_language text NOT NULL DEFAULT 'zh-CN',
  default_aspect_ratio text NOT NULL DEFAULT '16:9',
  default_duration_seconds integer NOT NULL DEFAULT 180,
  default_visibility resource_visibility NOT NULL DEFAULT 'private',
  auto_quality_check boolean NOT NULL DEFAULT true,
  revision integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id),
  FOREIGN KEY (workspace_id, user_id)
    REFERENCES workspace_members(workspace_id, user_id) ON DELETE CASCADE,
  CONSTRAINT creation_preferences_language_format CHECK (
    default_language ~ '^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$'
  ),
  CONSTRAINT creation_preferences_aspect_ratio CHECK (
    default_aspect_ratio IN ('16:9', '9:16', '1:1', '4:3')
  ),
  CONSTRAINT creation_preferences_duration CHECK (
    default_duration_seconds BETWEEN 15 AND 3600
  ),
  CONSTRAINT creation_preferences_revision CHECK (revision > 0)
);

CREATE TRIGGER creation_preferences_set_updated_at
  BEFORE UPDATE ON creation_preferences
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE creation_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE creation_preferences FORCE ROW LEVEL SECURITY;
CREATE POLICY creation_preferences_owner ON creation_preferences FOR ALL
  USING (
    app.is_system_actor()
    OR (user_id = app.current_user_id() AND app.can_access_workspace(workspace_id))
  )
  WITH CHECK (
    app.is_system_actor()
    OR (user_id = app.current_user_id() AND app.can_write_workspace(workspace_id))
  );

COMMIT;
