from sqlalchemy import text

from app.db.session import SessionLocal


def _register_user(client, *, email: str = "ada@example.com", password: str = "supersecret123") -> None:
    response = client.post(
        "/auth/register",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": email,
            "password": password,
        },
    )
    assert response.status_code == 200, response.text


def _web_login(client, *, email: str = "ada@example.com", password: str = "supersecret123") -> None:
    response = client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def test_availability_page_includes_painted_calendar(client):
    _register_user(client)
    _web_login(client)

    response = client.get("/availability")

    assert response.status_code == 200, response.text
    assert "availability-card" in response.text
    assert "data-availability-calendar-root" in response.text
    assert "data-availability-selected-cells-input" in response.text
    assert "data-availability-save" in response.text
    assert "data-group-meeting-slot" in response.text
    assert "group-meeting-grid" in response.text
    assert "availability.js" in response.text
    assert "data-time-picker" not in response.text


def test_availability_add_supports_multiple_days(client):
    _register_user(client)
    _web_login(client)

    response = client.post(
        "/availability/add",
        data={
            "day_of_week": ["1", "3", "5"],
            "start_time": "09:00",
            "end_time": "10:00",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT day_of_week, start_time, end_time
                FROM time_slot_preferences
                ORDER BY day_of_week
                """
            )
        ).mappings().all()
    finally:
        db.close()

    assert [int(row["day_of_week"]) for row in rows] == [1, 3, 5]
    assert all(str(row["start_time"]) == "09:00:00" for row in rows)
    assert all(str(row["end_time"]) == "10:00:00" for row in rows)


def test_availability_painted_selection_replaces_and_can_clear(client):
    _register_user(client)
    _web_login(client)

    first_payload = '[{"day_of_week": 1, "start_minutes": 480}, {"day_of_week": 1, "start_minutes": 495}]'
    second_payload = '[{"day_of_week": 2, "start_minutes": 600}]'

    response_first = client.post(
        "/availability/add",
        data={"selected_cells": first_payload, "next": "/availability"},
        follow_redirects=False,
    )
    assert response_first.status_code == 303, response_first.text

    response_second = client.post(
        "/availability/add",
        data={"selected_cells": second_payload, "next": "/availability"},
        follow_redirects=False,
    )
    assert response_second.status_code == 303, response_second.text

    db = SessionLocal()
    try:
        rows_after_replace = db.execute(
            text(
                """
                SELECT day_of_week, start_time, end_time
                FROM time_slot_preferences
                ORDER BY day_of_week, start_time
                """
            )
        ).mappings().all()
    finally:
        db.close()

    assert len(rows_after_replace) == 1
    assert int(rows_after_replace[0]["day_of_week"]) == 2
    assert str(rows_after_replace[0]["start_time"]) == "10:00:00"
    assert str(rows_after_replace[0]["end_time"]) == "10:15:00"

    response_clear = client.post(
        "/availability/add",
        data={"selected_cells": "[]", "next": "/availability"},
        follow_redirects=False,
    )
    assert response_clear.status_code == 303, response_clear.text

    db = SessionLocal()
    try:
        rows_after_clear = db.execute(
            text("SELECT id FROM time_slot_preferences")
        ).mappings().all()
    finally:
        db.close()

    assert rows_after_clear == []
