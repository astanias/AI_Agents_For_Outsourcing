from sqlalchemy import text

from app.db.session import SessionLocal


class CalendarTravelWarningService:
    def evaluate_meeting(self, db, *, user, meeting, persist=False):
        return []

    def enrich_meetings(self, db, *, user, meetings, persist=False):
        enriched = []
        for meeting in meetings:
            item = dict(meeting)
            if item["title"] == "Client Review":
                item["travel_warnings"] = [
                    {
                        "severity": "critical",
                        "message": "You likely cannot arrive on time.",
                        "travel_minutes": 90,
                        "distance_miles": 48.2,
                        "available_minutes": 30,
                        "origin_source": "previous_meeting",
                    }
                ]
            else:
                item["travel_warnings"] = []
            enriched.append(item)
        return enriched


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


def _create_meeting(client, *, title: str, start_time: str, end_time: str, location: str = "") -> None:
    response = client.post(
        "/meetings/create",
        data={
            "title": title,
            "location": location,
            "location_raw": location,
            "location_latitude": "",
            "location_longitude": "",
            "start_time": start_time,
            "end_time": end_time,
            "invitees": "",
            "q": "",
            "status": "",
            "mine": "",
            "day": "2030-01-01",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _seed_meeting(
    *,
    organizer_email: str,
    title: str,
    attendee_statuses: list[tuple[str, str]],
    start_time: str = "2030-02-08T09:00:00Z",
    end_time: str = "2030-02-08T10:00:00Z",
) -> None:
    db = SessionLocal()
    try:
        organizer_id = int(
            db.execute(
                text("SELECT id FROM users WHERE email = :email"),
                {"email": organizer_email},
            ).scalar_one()
        )
        calendar_id = int(
            db.execute(
                text(
                    """
                    INSERT INTO calendars (name, owner_type, owner_id)
                    VALUES (:name, 'user', :owner_id)
                    RETURNING id
                    """
                ),
                {"name": f"{organizer_email} calendar", "owner_id": organizer_id},
            ).scalar_one()
        )
        meeting_id = int(
            db.execute(
                text(
                    """
                    INSERT INTO meetings (calendar_id, title, start_time, end_time, status, created_by)
                    VALUES (:calendar_id, :title, :start_time, :end_time, 'confirmed', :created_by)
                    RETURNING id
                    """
                ),
                {
                    "calendar_id": calendar_id,
                    "title": title,
                    "start_time": start_time,
                    "end_time": end_time,
                    "created_by": organizer_id,
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
        for attendee_email, attendee_status in attendee_statuses:
            attendee_id = int(
                db.execute(
                    text("SELECT id FROM users WHERE email = :email"),
                    {"email": attendee_email},
                ).scalar_one()
            )
            db.execute(
                text(
                    """
                    INSERT INTO meeting_attendees (meeting_id, user_id, status)
                    VALUES (:meeting_id, :user_id, :status)
                    """
                ),
                {"meeting_id": meeting_id, "user_id": attendee_id, "status": attendee_status},
            )
        db.commit()
    finally:
        db.close()


def test_calendar_page_renders_monthly_grid_and_modal_markup(client, monkeypatch):
    _register_user(client)
    _web_login(client)
    monkeypatch.setattr("app.web.routes.get_travel_warning_service", lambda: CalendarTravelWarningService())

    _create_meeting(
        client,
        title="Morning Sync",
        location="Campus Center",
        start_time="2030-01-08T09:00",
        end_time="2030-01-08T10:00",
    )
    _create_meeting(
        client,
        title="Client Review",
        location="Boston Office",
        start_time="2030-01-15T13:00",
        end_time="2030-01-15T14:00",
    )

    response = client.get("/calendar?month=2030-01")

    assert response.status_code == 200, response.text
    assert "January 2030" in response.text
    assert "Morning Sync" in response.text
    assert "Client Review" in response.text
    assert "data-calendar-open" in response.text
    assert "data-calendar-modal" in response.text
    assert "data-calendar-edit-toggle" in response.text
    assert "calendar-mobile-view" in response.text
    assert "calendar-mobile-day-2030-01-08" in response.text
    assert "data-warning-severity=\"critical\"" in response.text
    assert "Campus Center" in response.text


def test_calendar_page_shows_empty_month_state(client, monkeypatch):
    _register_user(client)
    _web_login(client)
    monkeypatch.setattr("app.web.routes.get_travel_warning_service", lambda: CalendarTravelWarningService())

    response = client.get("/calendar?month=2031-02")

    assert response.status_code == 200, response.text
    assert "No meetings scheduled" in response.text
    assert "This month is open." in response.text


def test_calendar_and_meetings_pages_scope_meeting_visibility(client, monkeypatch):
    _register_user(client, email="owner@example.com")
    _register_user(client, email="attendee@example.com")
    _register_user(client, email="outsider@example.com")
    monkeypatch.setattr("app.web.routes.get_travel_warning_service", lambda: CalendarTravelWarningService())

    _seed_meeting(
        organizer_email="owner@example.com",
        title="Owner Private Meeting",
        attendee_statuses=[],
    )
    _seed_meeting(
        organizer_email="owner@example.com",
        title="Pending Invite Meeting",
        attendee_statuses=[("attendee@example.com", "invited")],
    )
    _seed_meeting(
        organizer_email="owner@example.com",
        title="Accepted Invite Meeting",
        attendee_statuses=[("attendee@example.com", "accepted")],
    )
    _seed_meeting(
        organizer_email="owner@example.com",
        title="Maybe Invite Meeting",
        attendee_statuses=[("attendee@example.com", "maybe")],
    )
    _seed_meeting(
        organizer_email="owner@example.com",
        title="Declined Invite Meeting",
        attendee_statuses=[("attendee@example.com", "declined")],
    )

    _web_login(client, email="attendee@example.com")

    calendar_response = client.get("/calendar?month=2030-02")
    assert calendar_response.status_code == 200, calendar_response.text
    assert "Accepted Invite Meeting" in calendar_response.text
    assert "Maybe Invite Meeting" in calendar_response.text
    assert "Pending Invite Meeting" not in calendar_response.text
    assert "Declined Invite Meeting" not in calendar_response.text
    assert "Owner Private Meeting" not in calendar_response.text

    meetings_response = client.get("/meetings?day=2030-02-08")
    assert meetings_response.status_code == 200, meetings_response.text
    assert "Accepted Invite Meeting" in meetings_response.text
    assert "Maybe Invite Meeting" in meetings_response.text
    assert "Pending Invite Meeting" in meetings_response.text
    assert "Declined Invite Meeting" not in meetings_response.text
    assert "Owner Private Meeting" not in meetings_response.text


def test_calendar_modal_edit_route_updates_meeting_and_resets_attendees(client, monkeypatch):
    organizer_response = client.post(
        "/auth/register",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "password": "supersecret123",
        },
    )
    assert organizer_response.status_code == 200, organizer_response.text
    organizer_token = organizer_response.json()["access_token"]

    attendee_response = client.post(
        "/auth/register",
        json={
            "first_name": "Grace",
            "last_name": "Hopper",
            "email": "grace@example.com",
            "password": "supersecret123",
        },
    )
    assert attendee_response.status_code == 200, attendee_response.text
    attendee_token = attendee_response.json()["access_token"]

    create_response = client.post(
        "/api/meetings/",
        headers=_auth_headers(organizer_token),
        json={
            "title": "Design Review",
            "description": "Discuss the current prototype",
            "location": "Room 101",
            "meeting_type": "in_person",
            "start_time": "2030-01-08T09:00:00Z",
            "end_time": "2030-01-08T10:00:00Z",
            "attendee_emails": ["grace@example.com"],
        },
    )
    assert create_response.status_code == 200, create_response.text
    meeting_id = create_response.json()["id"]

    rsvp_response = client.post(
        f"/api/meetings/{meeting_id}/rsvp",
        headers=_auth_headers(attendee_token),
        json={"status": "accepted"},
    )
    assert rsvp_response.status_code == 200, rsvp_response.text

    _web_login(client)
    monkeypatch.setattr("app.web.routes.get_travel_warning_service", lambda: CalendarTravelWarningService())

    update_response = client.post(
        "/calendar/meetings/update",
        data={
            "meeting_id": str(meeting_id),
            "month": "2030-01",
            "title": "Updated Design Review",
            "description": "Review the revised prototype",
            "location": "Room 202",
            "meeting_type": "virtual",
            "start_time": "2030-01-08T11:00",
            "end_time": "2030-01-08T12:00",
        },
        follow_redirects=False,
    )
    assert update_response.status_code == 303, update_response.text
    assert update_response.headers["location"] == "/calendar?month=2030-01"

    db = SessionLocal()
    try:
        meeting_row = db.execute(
            text(
                """
                SELECT title, description, location, meeting_type, start_time, end_time
                FROM meetings
                WHERE id = :meeting_id
                """
            ),
            {"meeting_id": meeting_id},
        ).mappings().one()
        attendee_statuses = db.execute(
            text(
                """
                SELECT user_id, status
                FROM meeting_attendees
                WHERE meeting_id = :meeting_id
                ORDER BY user_id
                """
            ),
            {"meeting_id": meeting_id},
        ).mappings().all()
    finally:
        db.close()

    assert meeting_row["title"] == "Updated Design Review"
    assert meeting_row["description"] == "Review the revised prototype"
    assert meeting_row["location"] == "Room 202"
    assert meeting_row["meeting_type"] == "virtual"
    assert meeting_row["start_time"].strftime("%Y-%m-%dT%H:%M") == "2030-01-08T11:00"
    assert meeting_row["end_time"].strftime("%Y-%m-%dT%H:%M") == "2030-01-08T12:00"
    assert [(row["user_id"], row["status"]) for row in attendee_statuses] == [
        (1, "accepted"),
        (2, "invited"),
    ]
