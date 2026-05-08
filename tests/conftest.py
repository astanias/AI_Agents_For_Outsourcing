import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


def _derive_test_database_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    db_name = parsed.path.lstrip("/") or "appdb"
    if db_name.endswith("_test"):
        return database_url
    return urlunsplit(parsed._replace(path=f"/{db_name}_test"))


def _database_name(database_url: str) -> str:
    return urlsplit(database_url).path.lstrip("/")


def _admin_database_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit(parsed._replace(path="/postgres"))


def _psycopg2_url(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg2://", "postgresql://", 1)


def _configure_test_database_url() -> None:
    base_url = os.environ.get("DATABASE_URL", "postgresql+psycopg2://appuser:apppassword@localhost:5433/appdb")
    os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or _derive_test_database_url(base_url)


def _ensure_test_database_exists() -> None:
    database_url = os.environ["DATABASE_URL"]
    database_name = _database_name(database_url)
    if not database_name.endswith("_test"):
        raise RuntimeError(f"Refusing to run tests against non-test database: {database_name}")

    admin_url = _psycopg2_url(_admin_database_url(database_url))
    conn = psycopg2.connect(admin_url)
    try:
        conn.set_session(autocommit=True)
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
            if cursor.fetchone() is None:
                cursor.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        conn.close()


_configure_test_database_url()
_ensure_test_database_exists()

from app.db.bootstrap import ensure_runtime_schema
from app.db.session import SessionLocal
from app.main import create_app


def _ensure_base_schema() -> None:
    db = SessionLocal()
    try:
        users_table_exists = db.execute(text("SELECT to_regclass('public.users')")).scalar_one()
        if users_table_exists is not None:
            return
    finally:
        db.close()

    schema_path = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")
    db = SessionLocal()
    try:
        raw_connection = db.connection().connection
        with raw_connection.cursor() as cursor:
            cursor.execute(schema_sql)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@pytest.fixture(scope="session", autouse=True)
def _require_db():
    try:
        _ensure_base_schema()
        ensure_runtime_schema()
        db = SessionLocal()
        db.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("Postgres not running / DATABASE_URL not reachable")
    finally:
        try:
            db.close()
        except Exception:
            pass


def _truncate_all_tables() -> None:
    db = SessionLocal()
    try:
        database_name = db.execute(text("SELECT current_database()")).scalar_one()
        if not str(database_name).endswith("_test"):
            raise RuntimeError(f"Refusing to truncate non-test database: {database_name}")

        db.execute(
            text(
                """
                TRUNCATE TABLE
                    assistant_draft_actions,
                    assistant_threads,
                    notifications,
                    notification_preferences,
                    meeting_attendees,
                    meetings,
                    user_calendars,
                    calendars,
                    time_slot_preferences,
                    group_memberships,
                    groups,
                    location_cache,
                    refresh_tokens,
                    password_credentials,
                    auth_identities,
                    users
                RESTART IDENTITY CASCADE
                """
            )
        )
        db.commit()
    except Exception:
        # If DB isn't up / schema isn't applied, let individual tests fail with clearer errors.
        db.rollback()
    finally:
        db.close()


@pytest.fixture()
def _db_cleanup():
    _truncate_all_tables()
    yield
    _truncate_all_tables()


@pytest.fixture()
def client(_db_cleanup) -> TestClient:
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
