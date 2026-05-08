from sqlalchemy import text

from app.core.config import settings
from app.db.session import SessionLocal


def _register(client, *, email: str, first_name: str = "Ada", last_name: str = "User") -> str:
    response = client.post(
        "/auth/register",
        json={
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "password": "supersecret123",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user_id(email: str) -> int:
    db = SessionLocal()
    try:
        return int(db.execute(text("SELECT id FROM users WHERE email = :email"), {"email": email}).scalar_one())
    finally:
        db.close()


def _create_group(*, name: str, member_roles: dict[int, str]) -> int:
    db = SessionLocal()
    try:
        group_id = int(
            db.execute(
                text("INSERT INTO groups (name, description) VALUES (:name, NULL) RETURNING id"),
                {"name": name},
            ).scalar_one()
        )
        for user_id, role in member_roles.items():
            db.execute(
                text(
                    """
                    INSERT INTO group_memberships (user_id, group_id, role)
                    VALUES (:user_id, :group_id, :role)
                    """
                ),
                {"user_id": user_id, "group_id": group_id, "role": role},
            )
        db.commit()
        return group_id
    finally:
        db.close()


def _meeting_count() -> int:
    db = SessionLocal()
    try:
        return int(db.execute(text("SELECT COUNT(*) FROM meetings")).scalar_one())
    finally:
        db.close()


def test_assistant_routes_are_hidden_from_openapi_docs(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200, response.text

    paths = response.json()["paths"]
    assert not any(path.startswith("/api/assistant") for path in paths)


def test_assistant_threads_are_scoped_to_current_user(client):
    ada_token = _register(client, email="ada@example.com", first_name="Ada")
    grace_token = _register(client, email="grace@example.com", first_name="Grace")

    create_response = client.post(
        "/api/assistant/threads",
        headers=_headers(ada_token),
        json={"title": "Schedule help"},
    )
    assert create_response.status_code == 200, create_response.text
    thread_id = create_response.json()["id"]

    list_response = client.get("/api/assistant/threads", headers=_headers(ada_token))
    assert list_response.status_code == 200, list_response.text
    assert [thread["id"] for thread in list_response.json()] == [thread_id]

    forbidden_response = client.get(f"/api/assistant/threads/{thread_id}", headers=_headers(grace_token))
    assert forbidden_response.status_code == 404, forbidden_response.text


def test_assistant_creates_draft_then_confirmation_writes_meeting_and_notifications(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    organizer_token = _register(client, email="ada@example.com", first_name="Ada")
    _register(client, email="grace@example.com", first_name="Grace")

    organizer_id = _user_id("ada@example.com")
    grace_id = _user_id("grace@example.com")
    _create_group(name="Planning", member_roles={organizer_id: "owner", grace_id: "member"})

    thread = client.post("/api/assistant/threads", headers=_headers(organizer_token), json={})
    assert thread.status_code == 200, thread.text
    thread_id = thread.json()["id"]

    draft_response = client.post(
        f"/api/assistant/threads/{thread_id}/messages",
        headers=_headers(organizer_token),
        json={"message": "Schedule called Project Sync on 2030-04-21T14:00 with grace@example.com"},
    )
    assert draft_response.status_code == 200, draft_response.text
    draft_payload = draft_response.json()
    assert draft_payload["pending_draft"]["action_type"] == "create_meeting"
    assert draft_payload["pending_draft"]["payload"]["attendee_emails"] == ["grace@example.com"]
    assert _meeting_count() == 0

    confirm_response = client.post(
        f"/api/assistant/threads/{thread_id}/confirm",
        headers=_headers(organizer_token),
        json={},
    )
    assert confirm_response.status_code == 200, confirm_response.text
    completed = confirm_response.json()["completed_action"]
    assert completed["title"] == "Project Sync"
    assert completed["current_user_status"] == "accepted"
    assert _meeting_count() == 1

    db = SessionLocal()
    try:
        attendee_status = db.execute(
            text(
                """
                SELECT ma.status
                FROM meeting_attendees ma
                JOIN users u ON u.id = ma.user_id
                WHERE u.email = 'grace@example.com'
                """
            )
        ).scalar_one()
        invite_notifications = db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM notifications n
                JOIN users u ON u.id = n.user_id
                WHERE u.email = 'grace@example.com'
                  AND n.channel = 'in_app'
                  AND n.type = 'invite'
                """
            )
        ).scalar_one()
    finally:
        db.close()

    assert attendee_status == "invited"
    assert int(invite_notifications) == 1


def test_assistant_rejects_invitees_without_one_common_group(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    organizer_token = _register(client, email="ada@example.com", first_name="Ada")
    _register(client, email="ben@example.com", first_name="Ben")
    _register(client, email="alan@example.com", first_name="Alan")

    organizer_id = _user_id("ada@example.com")
    ben_id = _user_id("ben@example.com")
    alan_id = _user_id("alan@example.com")
    _create_group(name="Ben Group", member_roles={organizer_id: "owner", ben_id: "member"})
    _create_group(name="Alan Group", member_roles={organizer_id: "owner", alan_id: "member"})

    thread = client.post("/api/assistant/threads", headers=_headers(organizer_token), json={})
    thread_id = thread.json()["id"]

    response = client.post(
        f"/api/assistant/threads/{thread_id}/messages",
        headers=_headers(organizer_token),
        json={"message": "Schedule called Cross Group on 2030-04-21T14:00 with ben@example.com and alan@example.com"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["pending_draft"] is None
    assert payload["pending_questions"]
    assert _meeting_count() == 0


def test_assistant_asks_when_name_lookup_is_ambiguous(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    organizer_token = _register(client, email="ada@example.com", first_name="Ada")
    _register(client, email="ben.one@example.com", first_name="Ben", last_name="One")
    _register(client, email="ben.two@example.com", first_name="Ben", last_name="Two")

    organizer_id = _user_id("ada@example.com")
    ben_one_id = _user_id("ben.one@example.com")
    ben_two_id = _user_id("ben.two@example.com")
    _create_group(name="Ambiguous", member_roles={organizer_id: "owner", ben_one_id: "member", ben_two_id: "member"})

    thread = client.post("/api/assistant/threads", headers=_headers(organizer_token), json={})
    thread_id = thread.json()["id"]

    response = client.post(
        f"/api/assistant/threads/{thread_id}/messages",
        headers=_headers(organizer_token),
        json={"message": "Schedule called Review on May 8 at 7 pm with Ben"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["pending_draft"] is None
    assert payload["pending_questions"] == ["Which Ben should I invite?"]
    assert {candidate["email"] for candidate in payload["candidate_invitees"]} == {
        "ben.one@example.com",
        "ben.two@example.com",
    }


def test_assistant_uses_follow_up_context_for_missing_datetime(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    organizer_token = _register(client, email="ada@example.com", first_name="Ada")
    _register(client, email="grace@example.com", first_name="Grace")

    organizer_id = _user_id("ada@example.com")
    grace_id = _user_id("grace@example.com")
    _create_group(name="Follow Up Planning", member_roles={organizer_id: "owner", grace_id: "member"})

    thread = client.post("/api/assistant/threads", headers=_headers(organizer_token), json={})
    thread_id = thread.json()["id"]

    first_response = client.post(
        f"/api/assistant/threads/{thread_id}/messages",
        headers=_headers(organizer_token),
        json={"message": "Schedule called Hoboken Sync with grace@example.com in Hoboken New Jersey"},
    )
    assert first_response.status_code == 200, first_response.text
    assert first_response.json()["pending_draft"] is None
    assert first_response.json()["pending_questions"] == ["What date and time should I use?"]

    follow_up_response = client.post(
        f"/api/assistant/threads/{thread_id}/messages",
        headers=_headers(organizer_token),
        json={"message": "May 11th, 2030 at 7 PM"},
    )
    assert follow_up_response.status_code == 200, follow_up_response.text
    payload = follow_up_response.json()
    draft_payload = payload["pending_draft"]["payload"]
    assert draft_payload["title"] == "Hoboken Sync"
    assert draft_payload["attendee_emails"] == ["grace@example.com"]
    assert draft_payload["location"] == "Hoboken New Jersey"
    assert draft_payload["start_time"].startswith("2030-05-11T23:00:00")
    assert payload["candidate_invitees"] == []
    assert "May 11, 2030 at 7:00 PM" in payload["assistant_message"]["content"]
    assert _meeting_count() == 0


def test_assistant_parses_weekday_day_names_and_location(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    organizer_token = _register(client, email="ada@example.com", first_name="Ada")
    _register(client, email="alex@example.com", first_name="Alex", last_name="Mercer")
    _register(client, email="cameron.kim@example.com", first_name="Cameron", last_name="Kim")

    organizer_id = _user_id("ada@example.com")
    alex_id = _user_id("alex@example.com")
    cameron_id = _user_id("cameron.kim@example.com")
    _create_group(
        name="Hoboken Planning",
        member_roles={organizer_id: "owner", alex_id: "member", cameron_id: "member"},
    )

    thread = client.post("/api/assistant/threads", headers=_headers(organizer_token), json={})
    thread_id = thread.json()["id"]

    response = client.post(
        f"/api/assistant/threads/{thread_id}/messages",
        headers=_headers(organizer_token),
        json={
            "message": "Schedule a meeting on Monday the 11th, 2030 at 7 PM with Alex and Cameron Kim in Hoboken New Jersey"
        },
    )
    assert response.status_code == 200, response.text
    draft_payload = response.json()["pending_draft"]["payload"]
    assert set(draft_payload["attendee_emails"]) == {"alex@example.com", "cameron.kim@example.com"}
    assert draft_payload["location"] == "Hoboken New Jersey"
    assert draft_payload["start_time"].startswith("2030-05-11T23:00:00")
    assert _meeting_count() == 0


def test_assistant_interprets_plain_language_noon_as_local_time(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    organizer_token = _register(client, email="ada@example.com", first_name="Ada")
    _register(client, email="grace@example.com", first_name="Grace")

    organizer_id = _user_id("ada@example.com")
    grace_id = _user_id("grace@example.com")
    _create_group(name="Noon Planning", member_roles={organizer_id: "owner", grace_id: "member"})

    thread = client.post("/api/assistant/threads", headers=_headers(organizer_token), json={})
    thread_id = thread.json()["id"]

    response = client.post(
        f"/api/assistant/threads/{thread_id}/messages",
        headers=_headers(organizer_token),
        json={"message": "Schedule called Lunch Sync on May 25th, 2026 at 12:00 pm with grace@example.com"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    draft_payload = payload["pending_draft"]["payload"]
    assert draft_payload["start_time"].startswith("2026-05-25T16:00:00")
    assert "May 25, 2026 at 12:00 PM" in payload["assistant_message"]["content"]
    assert _meeting_count() == 0


def test_assistant_discard_leaves_no_meeting_rows(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    organizer_token = _register(client, email="ada@example.com", first_name="Ada")
    _register(client, email="grace@example.com", first_name="Grace")

    organizer_id = _user_id("ada@example.com")
    grace_id = _user_id("grace@example.com")
    _create_group(name="Planning", member_roles={organizer_id: "owner", grace_id: "member"})

    thread = client.post("/api/assistant/threads", headers=_headers(organizer_token), json={})
    thread_id = thread.json()["id"]
    draft_response = client.post(
        f"/api/assistant/threads/{thread_id}/messages",
        headers=_headers(organizer_token),
        json={"message": "Schedule called Throwaway on 2030-04-21T14:00 with grace@example.com"},
    )
    draft_id = draft_response.json()["pending_draft"]["id"]

    discard_response = client.post(
        f"/api/assistant/threads/{thread_id}/discard",
        headers=_headers(organizer_token),
        json={"draft_action_id": draft_id},
    )
    assert discard_response.status_code == 200, discard_response.text
    assert discard_response.json()["pending_draft"] is None
    assert _meeting_count() == 0
