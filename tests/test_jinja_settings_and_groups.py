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


def _user_id_for_email(email: str) -> int:
    db = SessionLocal()
    try:
        return int(
            db.execute(
                text("SELECT id FROM users WHERE email = :email"),
                {"email": email},
            ).scalar_one()
        )
    finally:
        db.close()


def _seed_group(
    *,
    owner_email: str,
    name: str = "Capstone Crew",
    description: str = "Senior design planning",
    extra_members: list[tuple[str, str]] | None = None,
) -> int:
    db = SessionLocal()
    try:
        owner_id = int(
            db.execute(
                text("SELECT id FROM users WHERE email = :email"),
                {"email": owner_email},
            ).scalar_one()
        )
        group_id = int(
            db.execute(
                text(
                    """
                    INSERT INTO groups (name, description)
                    VALUES (:name, :description)
                    RETURNING id
                    """
                ),
                {"name": name, "description": description},
            ).scalar_one()
        )
        db.execute(
            text(
                """
                INSERT INTO group_memberships (user_id, group_id, role)
                VALUES (:user_id, :group_id, 'owner')
                """
            ),
            {"user_id": owner_id, "group_id": group_id},
        )
        for email, role in extra_members or []:
            member_id = int(
                db.execute(
                    text("SELECT id FROM users WHERE email = :email"),
                    {"email": email},
                ).scalar_one()
            )
            db.execute(
                text(
                    """
                    INSERT INTO group_memberships (user_id, group_id, role)
                    VALUES (:user_id, :group_id, :role)
                    """
                ),
                {"user_id": member_id, "group_id": group_id, "role": role},
            )
        db.commit()
        return group_id
    finally:
        db.close()


def _seed_member_availability(email: str, windows: list[tuple[int, str, str]]) -> None:
    db = SessionLocal()
    try:
        user_id = int(
            db.execute(
                text("SELECT id FROM users WHERE email = :email"),
                {"email": email},
            ).scalar_one()
        )
        for day_of_week, start_time, end_time in windows:
            db.execute(
                text(
                    """
                    INSERT INTO time_slot_preferences (user_id, day_of_week, start_time, end_time)
                    VALUES (:user_id, :day_of_week, :start_time, :end_time)
                    """
                ),
                {
                    "user_id": user_id,
                    "day_of_week": day_of_week,
                    "start_time": start_time,
                    "end_time": end_time,
                },
            )
        db.commit()
    finally:
        db.close()


def _seed_member_meeting(
    *,
    organizer_email: str,
    attendee_emails: list[str],
    title: str,
    start_time: str,
    end_time: str,
    meeting_type: str = "in_person",
    location: str = "Babbio 122",
) -> int:
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
                    INSERT INTO meetings (
                        calendar_id,
                        title,
                        location,
                        meeting_type,
                        start_time,
                        end_time,
                        status,
                        created_by
                    )
                    VALUES (
                        :calendar_id,
                        :title,
                        :location,
                        :meeting_type,
                        :start_time,
                        :end_time,
                        'confirmed',
                        :created_by
                    )
                    RETURNING id
                    """
                ),
                {
                    "calendar_id": calendar_id,
                    "title": title,
                    "location": location,
                    "meeting_type": meeting_type,
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
        for email in attendee_emails:
            attendee_id = int(
                db.execute(
                    text("SELECT id FROM users WHERE email = :email"),
                    {"email": email},
                ).scalar_one()
            )
            db.execute(
                text(
                    """
                    INSERT INTO meeting_attendees (meeting_id, user_id, status)
                    VALUES (:meeting_id, :user_id, 'accepted')
                    """
                ),
                {"meeting_id": meeting_id, "user_id": attendee_id},
            )
        db.commit()
        return meeting_id
    finally:
        db.close()


def test_settings_page_renders_profile_notifications_and_shared_availability(client):
    _register_user(client)
    _web_login(client)

    response = client.get("/settings")

    assert response.status_code == 200, response.text
    assert "Profile Settings" in response.text
    assert "Weekly Availability" in response.text
    assert "Notification Preferences" in response.text
    assert 'name="next" value="/settings"' in response.text
    assert "Scheduler AI" in response.text
    assert "app-shell.js" in response.text
    assert "data-avatar-color-input" in response.text


def test_settings_profile_updates_avatar_color_in_header_and_profile(client):
    _register_user(client, email="profile@example.com")
    _web_login(client, email="profile@example.com")

    response = client.post(
        "/settings/profile",
        data={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "profile@example.com",
            "avatar_color": "red",
            "current_password": "",
            "new_password": "",
            "confirm_password": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/settings"

    settings_response = client.get("/settings")
    assert settings_response.status_code == 200, settings_response.text
    assert "background: #ef4444;" in settings_response.text

    db = SessionLocal()
    try:
        saved_color = db.execute(
            text(
                """
                SELECT avatar_color
                FROM users
                WHERE email = :email
                """
            ),
            {"email": "profile@example.com"},
        ).scalar_one()
    finally:
        db.close()

    assert saved_color == "red"


def test_settings_and_availability_share_same_preference_data(client):
    _register_user(client, email="grace@example.com")
    _web_login(client, email="grace@example.com")

    create_response = client.post(
        "/availability/add",
        data={
            "day_of_week": ["1", "3"],
            "start_time": "09:00",
            "end_time": "11:00",
            "next": "/settings",
        },
        follow_redirects=False,
    )

    assert create_response.status_code == 303, create_response.text
    assert create_response.headers["location"] == "/settings"

    settings_response = client.get("/settings")
    assert settings_response.status_code == 200, settings_response.text
    assert "data-availability-calendar-root" in settings_response.text
    assert "16 cells selected" in settings_response.text
    assert "Mon" in settings_response.text
    assert "Wed" in settings_response.text
    assert "9:00 AM" in settings_response.text

    availability_response = client.get("/availability")
    assert availability_response.status_code == 200, availability_response.text
    assert "data-availability-calendar-root" in availability_response.text
    assert "16 cells selected" in availability_response.text
    assert "Mon" in availability_response.text
    assert "Wed" in availability_response.text

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

    assert [int(row["day_of_week"]) for row in rows] == [1, 3]
    assert all(str(row["start_time"]) == "09:00:00" for row in rows)
    assert all(str(row["end_time"]) == "11:00:00" for row in rows)


def test_groups_page_supports_create_and_join_token_forms(client):
    _register_user(client, email="owner@example.com")
    _web_login(client, email="owner@example.com")

    create_response = client.post(
        "/groups/create",
        data={"name": "Capstone Crew", "description": "Senior design planning"},
        follow_redirects=False,
    )
    assert create_response.status_code == 303, create_response.text
    assert create_response.headers["location"] == "/groups"

    db = SessionLocal()
    try:
        group_id = int(
            db.execute(
                text(
                    """
                    SELECT id
                    FROM groups
                    WHERE name = :name
                    """
                ),
                {"name": "Capstone Crew"},
            ).scalar_one()
        )
    finally:
        db.close()

    token = f"{group_id:09d}"

    owner_groups_page = client.get("/groups")
    assert owner_groups_page.status_code == 200, owner_groups_page.text
    assert "Capstone Crew" in owner_groups_page.text
    assert token in owner_groups_page.text

    _register_user(client, email="member@example.com")
    logout_response = client.post("/logout", follow_redirects=False)
    assert logout_response.status_code == 303, logout_response.text

    _web_login(client, email="member@example.com")
    join_response = client.post(
        "/groups/join-token",
        data={"token": token},
        follow_redirects=False,
    )
    assert join_response.status_code == 303, join_response.text
    assert join_response.headers["location"] == "/groups"

    joined_groups_page = client.get("/groups")
    assert joined_groups_page.status_code == 200, joined_groups_page.text
    assert "Capstone Crew" in joined_groups_page.text
    assert token in joined_groups_page.text
    assert "member" in joined_groups_page.text.lower()

    db = SessionLocal()
    try:
        membership_count = int(
            db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM group_memberships
                    WHERE group_id = :group_id
                    """
                ),
                {"group_id": group_id},
            ).scalar_one()
        )
    finally:
        db.close()

    assert membership_count == 2


def test_group_workspace_owner_can_invite_managers_change_roles_and_remove_members(client):
    _register_user(client, email="owner@example.com")
    _register_user(client, email="manager@example.com")
    _register_user(client, email="member@example.com")
    _web_login(client, email="owner@example.com")

    group_id = _seed_group(owner_email="owner@example.com", name="Workspace Team")

    manager_invite = client.post(
        f"/groups/{group_id}/invite",
        data={"invitees": "manager@example.com", "role": "admin", "month": "", "member_id": ""},
        follow_redirects=False,
    )
    assert manager_invite.status_code == 303, manager_invite.text
    assert manager_invite.headers["location"] == f"/groups/{group_id}"

    member_invite = client.post(
        f"/groups/{group_id}/invite",
        data={"invitees": "member@example.com", "role": "member", "month": "", "member_id": ""},
        follow_redirects=False,
    )
    assert member_invite.status_code == 303, member_invite.text

    member_user_id = _user_id_for_email("member@example.com")
    promote_response = client.post(
        f"/groups/{group_id}/members/{member_user_id}/role",
        data={"role": "admin", "month": "", "member_id": ""},
        follow_redirects=False,
    )
    assert promote_response.status_code == 303, promote_response.text

    remove_response = client.post(
        f"/groups/{group_id}/members/{member_user_id}/remove",
        data={"month": "", "member_id": ""},
        follow_redirects=False,
    )
    assert remove_response.status_code == 303, remove_response.text

    db = SessionLocal()
    try:
        membership_rows = db.execute(
            text(
                """
                SELECT u.email, gm.role
                FROM group_memberships gm
                JOIN users u ON u.id = gm.user_id
                WHERE gm.group_id = :group_id
                ORDER BY u.email
                """
            ),
            {"group_id": group_id},
        ).mappings().all()
    finally:
        db.close()

    membership_map = {row["email"]: row["role"] for row in membership_rows}
    assert membership_map["owner@example.com"] == "owner"
    assert membership_map["manager@example.com"] == "admin"
    assert "member@example.com" not in membership_map


def test_group_workspace_manager_has_same_privileges_as_owner_for_invites(client):
    _register_user(client, email="owner@example.com")
    _register_user(client, email="manager@example.com")
    _register_user(client, email="newmember@example.com")

    group_id = _seed_group(
        owner_email="owner@example.com",
        name="Manager Rights Team",
        extra_members=[("manager@example.com", "admin")],
    )

    _web_login(client, email="manager@example.com")

    response = client.post(
        f"/groups/{group_id}/invite",
        data={"invitees": "newmember@example.com", "role": "member", "month": "", "member_id": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/groups/{group_id}"

    db = SessionLocal()
    try:
        inserted_role = db.execute(
            text(
                """
                SELECT gm.role
                FROM group_memberships gm
                JOIN users u ON u.id = gm.user_id
                WHERE gm.group_id = :group_id
                  AND u.email = 'newmember@example.com'
                """
            ),
            {"group_id": group_id},
        ).scalar_one()
    finally:
        db.close()

    assert inserted_role == "member"


def test_group_detail_shows_roster_group_calendar_and_member_availability_insights(client):
    _register_user(client, email="owner@example.com")
    _register_user(client, email="member@example.com")
    _register_user(client, email="guest@example.com")

    group_id = _seed_group(
        owner_email="owner@example.com",
        name="Insight Team",
        extra_members=[("member@example.com", "member")],
    )
    member_user_id = _user_id_for_email("member@example.com")

    _seed_member_availability(
        "member@example.com",
        [(1, "09:00", "12:00"), (3, "13:00", "16:00")],
    )
    _seed_member_meeting(
        organizer_email="member@example.com",
        attendee_emails=["guest@example.com"],
        title="Member Demo Review",
        start_time="2099-01-05T14:00:00+00:00",
        end_time="2099-01-05T15:00:00+00:00",
        meeting_type="virtual",
        location="https://zoom.example.com/member-demo",
    )

    _web_login(client, email="owner@example.com")

    response = client.get(f"/groups/{group_id}?member_id={member_user_id}")

    assert response.status_code == 200, response.text
    assert "Invite People" in response.text
    assert "Roster" in response.text
    assert "Group Calendar" in response.text
    assert "Member Insights" in response.text
    assert "Team Planning" in response.text
    assert "data-group-meeting-planner-root" in response.text
    assert "Member Demo Review" in response.text
    assert "Weekly Availability" in response.text
    assert "group-availability-grid" in response.text
    assert "Mon" in response.text
    assert "Wed" in response.text
    assert "member@example.com" in response.text


def test_group_meeting_planner_is_manager_only(client):
    _register_user(client, email="owner@example.com")
    _register_user(client, email="member@example.com")

    group_id = _seed_group(
        owner_email="owner@example.com",
        name="Planner Permissions Team",
        extra_members=[("member@example.com", "member")],
    )

    _web_login(client, email="member@example.com")
    member_response = client.get(f"/groups/{group_id}")

    assert member_response.status_code == 200, member_response.text
    assert "Planner Permissions Team" in member_response.text
    assert "data-group-meeting-planner-root" not in member_response.text
    assert "Team Planning" not in member_response.text
