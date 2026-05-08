from sqlalchemy import text

from app.db.bootstrap import ensure_runtime_schema
from app.db.session import SessionLocal, engine


def test_runtime_schema_bootstrap_creates_base_tables(_db_cleanup):
    db = SessionLocal()
    try:
        database_name = db.execute(text("SELECT current_database()")).scalar_one()
        assert str(database_name).endswith("_test")
        db.execute(text("DROP SCHEMA public CASCADE"))
        db.execute(text("CREATE SCHEMA public"))
        db.commit()
    finally:
        db.close()

    ensure_runtime_schema(engine)

    db = SessionLocal()
    try:
        meetings_table = db.execute(text("SELECT to_regclass('public.meetings')")).scalar_one()
        attendees_table = db.execute(text("SELECT to_regclass('public.meeting_attendees')")).scalar_one()
        description_column = db.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'meetings'
                  AND column_name = 'description'
                """
            )
        ).scalar_one_or_none()
    finally:
        db.close()

    assert meetings_table == "meetings"
    assert attendees_table == "meeting_attendees"
    assert description_column == 1
