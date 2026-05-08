import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db.session import engine


BASE_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
CREATE_INDEX_PATTERN = re.compile(r"\bCREATE INDEX\s+(?!IF NOT EXISTS\b)", re.IGNORECASE)
CREATE_TABLE_PATTERN = re.compile(r"\bCREATE TABLE\s+(?!IF NOT EXISTS\b)", re.IGNORECASE)


SCHEMA_PATCHES = (
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS description TEXT",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS meeting_type TEXT NOT NULL DEFAULT 'in_person'",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS color TEXT NOT NULL DEFAULT '#3498db'",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'confirmed'",
    (
        "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS created_by "
        "INTEGER REFERENCES users(id) ON DELETE SET NULL"
    ),
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_location TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_location_latitude DOUBLE PRECISION",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_location_longitude DOUBLE PRECISION",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_color TEXT NOT NULL DEFAULT 'blue'",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS location_raw TEXT",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS location_latitude DOUBLE PRECISION",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS location_longitude DOUBLE PRECISION",
    "ALTER TABLE meeting_attendees ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    """
    CREATE TABLE IF NOT EXISTS notification_preferences (
      user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
      email_enabled BOOLEAN NOT NULL DEFAULT TRUE,
      in_app_enabled BOOLEAN NOT NULL DEFAULT TRUE,
      meeting_reminders_enabled BOOLEAN NOT NULL DEFAULT TRUE,
      group_activity_enabled BOOLEAN NOT NULL DEFAULT TRUE,
      weekly_digest_enabled BOOLEAN NOT NULL DEFAULT FALSE,
      digest_frequency TEXT NOT NULL DEFAULT 'weekly' CHECK (digest_frequency IN ('daily', 'weekly')),
      quiet_hours_enabled BOOLEAN NOT NULL DEFAULT FALSE,
      quiet_hours_start TIME,
      quiet_hours_end TIME,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
      id BIGSERIAL PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      meeting_id INTEGER REFERENCES meetings(id) ON DELETE CASCADE,
      channel TEXT NOT NULL CHECK (channel IN ('email', 'in_app')),
      type TEXT NOT NULL CHECK (type IN ('invite', 'cancel', 'update', 'rsvp_update', 'reminder')),
      title TEXT NOT NULL,
      message TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'read', 'skipped')),
      provider_message_id TEXT,
      error_message TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      sent_at TIMESTAMPTZ,
      read_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notification_preferences_user_id ON notification_preferences(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_meeting_id ON notifications(meeting_id)",
    """
    CREATE TABLE IF NOT EXISTS location_cache (
      location_key TEXT PRIMARY KEY,
      location_label TEXT NOT NULL,
      latitude DOUBLE PRECISION NOT NULL,
      longitude DOUBLE PRECISION NOT NULL,
      provider TEXT NOT NULL DEFAULT 'openrouteservice',
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assistant_threads (
      id BIGSERIAL PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      title TEXT NOT NULL DEFAULT 'New scheduling chat',
      messages_json JSONB NOT NULL DEFAULT '[]'::jsonb,
      openai_thread_id TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assistant_draft_actions (
      id BIGSERIAL PRIMARY KEY,
      thread_id BIGINT NOT NULL REFERENCES assistant_threads(id) ON DELETE CASCADE,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      action_type TEXT NOT NULL CHECK (action_type IN ('create_meeting', 'update_meeting', 'cancel_meeting')),
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'discarded', 'failed')),
      target_meeting_id INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
      payload_json JSONB NOT NULL,
      result_json JSONB,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_assistant_threads_user_id ON assistant_threads(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_assistant_draft_actions_thread_id ON assistant_draft_actions(thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_assistant_draft_actions_user_status ON assistant_draft_actions(user_id, status)",
    """
    DO $$
    DECLARE constraint_name TEXT;
    BEGIN
        SELECT c.conname INTO constraint_name
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'meeting_attendees'
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) LIKE '%status%'
        LIMIT 1;

        IF constraint_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE meeting_attendees DROP CONSTRAINT %I', constraint_name);
        END IF;

        ALTER TABLE meeting_attendees
        ADD CONSTRAINT meeting_attendees_status_check
        CHECK (status IN ('invited', 'accepted', 'declined', 'maybe'));
    EXCEPTION
        WHEN duplicate_object THEN NULL;
    END $$;
    """,
    """
    DO $$
    BEGIN
        ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_type_check;

        ALTER TABLE notifications
        ADD CONSTRAINT notifications_type_check
        CHECK (type IN ('invite', 'cancel', 'update', 'rsvp_update', 'reminder'));
    EXCEPTION
        WHEN duplicate_object THEN NULL;
    END $$;
    """,
)


BOOTSTRAP_LOCK_ID = 2147483001


def _load_base_schema() -> str:
    schema_sql = BASE_SCHEMA_PATH.read_text(encoding="utf-8")
    schema_sql = CREATE_TABLE_PATTERN.sub("CREATE TABLE IF NOT EXISTS ", schema_sql)
    return CREATE_INDEX_PATTERN.sub("CREATE INDEX IF NOT EXISTS ", schema_sql)


def ensure_runtime_schema(db_engine: Engine = engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": BOOTSTRAP_LOCK_ID})
        conn.exec_driver_sql(_load_base_schema())
        for statement in SCHEMA_PATCHES:
            conn.execute(text(statement))
