import re

import pytest

from app.core.config import settings
from app.main import create_app


def _register_user(client, *, email: str, first_name: str = "Ada", last_name: str = "Lovelace") -> str:
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


def _web_login(client, *, email: str) -> None:
    response = client.post(
        "/login",
        data={"email": email, "password": "supersecret123"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def test_web_meeting_detail_requires_meeting_access(client):
    organizer_token = _register_user(client, email="organizer@example.com", first_name="Org", last_name="Owner")
    _register_user(client, email="outsider@example.com", first_name="Out", last_name="Sider")

    create_response = client.post(
        "/api/meetings/",
        headers={"Authorization": f"Bearer {organizer_token}"},
        json={
            "title": "Private Planning",
            "description": "Organizer-only meeting",
            "location": "Room 1",
            "meeting_type": "in_person",
            "start_time": "2030-04-21T14:00:00Z",
            "end_time": "2030-04-21T15:00:00Z",
            "attendee_emails": [],
        },
    )
    assert create_response.status_code == 200, create_response.text
    meeting_id = create_response.json()["id"]

    _web_login(client, email="outsider@example.com")
    blocked_response = client.get(f"/meetings/{meeting_id}", follow_redirects=False)

    assert blocked_response.status_code == 303
    assert blocked_response.headers["location"] == "/meetings"


def test_csrf_token_is_rendered_and_enforced_when_enabled(client, monkeypatch):
    page_response = client.get("/")
    assert page_response.status_code == 200
    token_match = re.search(r'name="csrf_token" value="([^"]+)"', page_response.text)
    assert token_match is not None
    csrf_token = token_match.group(1)

    monkeypatch.setattr(settings, "csrf_protection_enabled", True)
    blocked_response = client.post(
        "/login",
        data={"email": "nobody@example.com", "password": "supersecret123"},
        follow_redirects=False,
    )

    assert blocked_response.status_code == 403

    _register_user(client, email="csrf-user@example.com", first_name="Csrf", last_name="User")
    allowed_response = client.post(
        "/login",
        data={
            "email": "csrf-user@example.com",
            "password": "supersecret123",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert allowed_response.status_code == 303


def test_production_runtime_rejects_unsafe_defaults(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "jwt_secret", "dev-change-me")
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "csrf_protection_enabled", False)
    monkeypatch.setattr(settings, "frontend_origin", "http://localhost:5173")

    with pytest.raises(RuntimeError) as exc_info:
        create_app()

    message = str(exc_info.value)
    assert "JWT_SECRET" in message
    assert "COOKIE_SECURE" in message
    assert "CSRF_PROTECTION_ENABLED" in message
    assert "localhost" in message


def test_staging_runtime_allows_local_prod_like_ui_without_strict_security(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "staging")
    monkeypatch.setattr(settings, "jwt_secret", "dev-change-me")
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "csrf_protection_enabled", False)
    monkeypatch.setattr(settings, "frontend_origin", "http://localhost:5173")

    create_app()
