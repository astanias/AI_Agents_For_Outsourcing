from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db.session import SessionLocal


def _register_user(
    client,
    *,
    first_name: str,
    last_name: str,
    email: str,
    password: str = "supersecret123",
) -> None:
    response = client.post(
        "/auth/register",
        json={
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "password": password,
        },
    )
    assert response.status_code == 200, response.text


def _web_login(client, *, email: str, password: str = "supersecret123") -> None:
    response = client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _seed_invite_history() -> None:
    db = SessionLocal()
    try:
        organizer_id = int(
            db.execute(text("SELECT id FROM users WHERE email = 'organizer@example.com'")).scalar_one()
        )
        alex_id = int(db.execute(text("SELECT id FROM users WHERE email = 'alex@example.com'")).scalar_one())
        priya_id = int(db.execute(text("SELECT id FROM users WHERE email = 'priya@example.com'")).scalar_one())
        olivia_id = int(db.execute(text("SELECT id FROM users WHERE email = 'olivia@example.com'")).scalar_one())

        calendar_id = int(
            db.execute(
                text(
                    """
                    INSERT INTO calendars (name, owner_type, owner_id)
                    VALUES ('organizer@example.com calendar', 'user', :owner_id)
                    RETURNING id
                    """
                ),
                {"owner_id": organizer_id},
            ).scalar_one()
        )

        base_start = datetime(2030, 1, 1, 14, 0, tzinfo=timezone.utc)
        invite_sets = [
            [alex_id, priya_id, olivia_id],
            [alex_id, olivia_id],
        ]
        for index, attendee_ids in enumerate(invite_sets):
            meeting_id = int(
                db.execute(
                    text(
                        """
                        INSERT INTO meetings (
                            calendar_id,
                            title,
                            start_time,
                            end_time,
                            capacity,
                            setup_minutes,
                            cleanup_minutes
                        )
                        VALUES (
                            :calendar_id,
                            :title,
                            :start_time,
                            :end_time,
                            NULL,
                            0,
                            0
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "calendar_id": calendar_id,
                        "title": f"Planning Session {index + 1}",
                        "start_time": base_start + timedelta(days=index),
                        "end_time": base_start + timedelta(days=index, hours=1),
                    },
                ).scalar_one()
            )

            db.execute(
                text(
                    """
                    INSERT INTO meeting_attendees (meeting_id, user_id, status)
                    VALUES (:meeting_id, :user_id, 'accepted')
                    """
                ),
                {"meeting_id": meeting_id, "user_id": organizer_id},
            )
            for attendee_id in attendee_ids:
                db.execute(
                    text(
                        """
                        INSERT INTO meeting_attendees (meeting_id, user_id, status)
                        VALUES (:meeting_id, :user_id, 'invited')
                        """
                    ),
                    {"meeting_id": meeting_id, "user_id": attendee_id},
                )

        db.commit()
    finally:
        db.close()


def _seed_group_memberships(*emails: str) -> None:
    db = SessionLocal()
    try:
        group_id = int(
            db.execute(
                text(
                    """
                    INSERT INTO groups (name, description)
                    VALUES ('Privacy Test Group', 'Invitee suggestion visibility')
                    RETURNING id
                    """
                )
            ).scalar_one()
        )
        for index, email in enumerate(emails):
            user_id = int(db.execute(text("SELECT id FROM users WHERE email = :email"), {"email": email}).scalar_one())
            db.execute(
                text(
                    """
                    INSERT INTO group_memberships (user_id, group_id, role)
                    VALUES (:user_id, :group_id, :role)
                    """
                ),
                {
                    "user_id": user_id,
                    "group_id": group_id,
                    "role": "owner" if index == 0 else "member",
                },
            )
        db.commit()
    finally:
        db.close()


def test_invitee_suggestions_prioritize_frequent_users_then_matches(client):
    _register_user(client, first_name="Org", last_name="Owner", email="organizer@example.com")
    _register_user(client, first_name="Alex", last_name="Rivera", email="alex@example.com")
    _register_user(client, first_name="Maya", last_name="Stone", email="maya@example.com")
    _register_user(client, first_name="Priya", last_name="Shah", email="priya@example.com")
    _register_user(client, first_name="Olivia", last_name="Brooks", email="olivia@example.com")
    _register_user(client, first_name="Mallory", last_name="Mason", email="mallory@example.com")
    _web_login(client, email="organizer@example.com")
    _seed_group_memberships(
        "organizer@example.com",
        "alex@example.com",
        "maya@example.com",
        "priya@example.com",
        "olivia@example.com",
    )
    _seed_invite_history()

    response = client.get("/invitees/suggestions?q=ma")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["frequent"][0]["email"] == "alex@example.com"
    assert payload["frequent"][0]["reason"] == "Usually invite"
    assert any(item["email"] == "maya@example.com" for item in payload["matches"])
    assert not any(item["email"] == "mallory@example.com" for item in payload["matches"])
    assert not any(item["email"] == "mallory@example.com" for item in payload["frequent"])


def test_invitee_suggestions_can_show_frequent_user_in_handle_matches(client):
    _register_user(client, first_name="Org", last_name="Owner", email="organizer@example.com")
    _register_user(client, first_name="Alex", last_name="Rivera", email="alex@example.com")
    _register_user(client, first_name="Priya", last_name="Shah", email="priya@example.com")
    _register_user(client, first_name="Olivia", last_name="Brooks", email="olivia@example.com")
    _web_login(client, email="organizer@example.com")
    _seed_group_memberships("organizer@example.com", "alex@example.com", "priya@example.com", "olivia@example.com")
    _seed_invite_history()

    response = client.get("/invitees/suggestions?q=olivi")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert any(item["email"] == "olivia@example.com" for item in payload["frequent"])
    assert any(item["email"] == "olivia@example.com" for item in payload["matches"])


def test_invitee_suggestions_do_not_expose_registered_users_outside_shared_groups(client):
    _register_user(client, first_name="Org", last_name="Owner", email="organizer@example.com")
    _register_user(client, first_name="Alice", last_name="Groupmate", email="alice@example.com")
    _register_user(client, first_name="Avery", last_name="Private", email="avery@example.com")
    _web_login(client, email="organizer@example.com")
    _seed_group_memberships("organizer@example.com", "alice@example.com")

    response = client.get("/invitees/suggestions?q=a")

    assert response.status_code == 200, response.text
    payload = response.json()
    match_emails = {item["email"] for item in payload["matches"]}
    assert "alice@example.com" in match_emails
    assert "avery@example.com" not in match_emails


def test_meetings_page_includes_invitee_bubble_hook(client):
    _register_user(client, first_name="Ada", last_name="Lovelace", email="ada@example.com")
    _web_login(client, email="ada@example.com")

    response = client.get("/meetings")

    assert response.status_code == 200, response.text
    assert "data-invitee-picker-root" in response.text
    assert "data-invitee-url" in response.text
    assert "data-invitee-suggestions" in response.text
