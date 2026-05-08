from urllib.parse import parse_qs, urlsplit

from sqlalchemy import text

from app.core.config import settings
from app.db.session import SessionLocal


def test_register_login_me_refresh_logout(client):
    r = client.post(
        "/auth/register",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "password": "supersecret123",
        },
    )
    assert r.status_code == 200, r.text
    access = r.json()["access_token"]
    assert access
    assert client.cookies.get("refresh_token")

    r = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "ada@example.com"
    assert r.json()["avatar_color"] == "blue"

    old_refresh = client.cookies.get("refresh_token")
    r = client.post("/auth/refresh")
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]
    new_refresh = client.cookies.get("refresh_token")
    assert new_refresh and new_refresh != old_refresh

    r = client.post("/auth/logout")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_login_invalid_password(client):
    client.post(
        "/auth/register",
        json={
            "first_name": "Grace",
            "last_name": "Hopper",
            "email": "grace@example.com",
            "password": "supersecret123",
        },
    )

    r = client.post("/auth/login", json={"email": "grace@example.com", "password": "wrong"})
    assert r.status_code == 401


def test_update_profile_and_password(client):
    register_response = client.post(
        "/auth/register",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "password": "supersecret123",
        },
    )
    assert register_response.status_code == 200, register_response.text
    access = register_response.json()["access_token"]

    update_response = client.patch(
        "/auth/me",
        headers={"Authorization": f"Bearer {access}"},
        json={
            "first_name": "Augusta",
            "last_name": "Byron",
            "email": "augusta@example.com",
            "avatar_color": "teal",
            "current_password": "supersecret123",
            "new_password": "newsupersecret123",
        },
    )
    assert update_response.status_code == 200, update_response.text
    assert update_response.json()["first_name"] == "Augusta"
    assert update_response.json()["email"] == "augusta@example.com"
    assert update_response.json()["avatar_color"] == "teal"

    old_login = client.post(
        "/auth/login",
        json={"email": "ada@example.com", "password": "supersecret123"},
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/auth/login",
        json={"email": "augusta@example.com", "password": "newsupersecret123"},
    )
    assert new_login.status_code == 200, new_login.text


def test_web_signup_creates_account_and_redirects_to_meetings(client):
    r = client.get("/signup")
    assert r.status_code == 200
    assert "Create your account" in r.text

    r = client.post(
        "/signup",
        data={
            "first_name": "Linus",
            "last_name": "Torvalds",
            "email": "linus@example.com",
            "phone": "",
            "password": "supersecret123",
            "confirm_password": "supersecret123",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/meetings"

    meetings_page = client.get("/meetings")
    assert meetings_page.status_code == 200
    assert "Signed in as <strong>linus@example.com</strong>" in meetings_page.text


def test_login_page_shows_dev_social_login_buttons(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "google_client_id", None)
    monkeypatch.setattr(settings, "google_client_secret", None)

    response = client.get("/")

    assert response.status_code == 200
    assert "Continue with Google" in response.text
    assert "Continue with Microsoft" in response.text


def test_login_page_shows_google_in_dev_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "google_client_id", "google-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "google-client-secret")

    response = client.get("/")

    assert response.status_code == 200
    assert "Continue with Google" in response.text
    assert "Continue with Microsoft" in response.text


def test_login_page_hides_social_login_in_production(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "google_client_id", "google-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "google-client-secret")

    response = client.get("/")

    assert response.status_code == 200
    assert "Continue with Google" not in response.text
    assert "Continue with Microsoft" not in response.text


def test_login_page_hides_social_login_in_staging(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "staging")
    monkeypatch.setattr(settings, "google_client_id", "google-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "google-client-secret")

    response = client.get("/")

    assert response.status_code == 200
    assert "Continue with Google" not in response.text
    assert "Continue with Microsoft" not in response.text


def test_microsoft_oauth_stub_redirects_with_flash(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")

    response = client.get("/web/auth/microsoft", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    login_page = client.get("/")
    assert "Microsoft OAuth flow is not wired yet." in login_page.text


def test_google_oauth_dev_button_without_config_redirects_with_flash(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "google_client_id", None)
    monkeypatch.setattr(settings, "google_client_secret", None)

    response = client.get("/web/auth/google", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    login_page = client.get("/")
    assert "Google login is not configured yet." in login_page.text


def test_web_google_login_redirect_uses_configured_callback(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "google-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "google-client-secret")
    monkeypatch.setattr(settings, "app_base_url", "https://schedulerai.tech")

    response = client.get("/web/auth/google", follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    parsed = urlsplit(location)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == ["google-client-id"]
    assert query["redirect_uri"] == ["https://schedulerai.tech/web/auth/google/callback"]
    assert query["scope"] == ["openid email profile"]
    assert query["state"][0]


def test_web_google_callback_auto_links_verified_existing_email(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "google-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "google-client-secret")
    monkeypatch.setattr(settings, "app_base_url", "http://127.0.0.1:8000")

    register_response = client.post(
        "/auth/register",
        json={
            "first_name": "Linus",
            "last_name": "Torvalds",
            "email": "linus@example.com",
            "password": "supersecret123",
        },
    )
    assert register_response.status_code == 200, register_response.text

    start_response = client.get("/web/auth/google", follow_redirects=False)
    state = parse_qs(urlsplit(start_response.headers["location"]).query)["state"][0]

    from app.api import auth as auth_api

    def fake_exchange_code(*, code, code_verifier, redirect_uri):
        assert code == "oauth-code"
        assert code_verifier is None
        assert redirect_uri == "http://127.0.0.1:8000/web/auth/google/callback"
        return {"id_token": "verified-id-token"}

    def fake_verify_id_token(id_token):
        assert id_token == "verified-id-token"
        return {
            "sub": "google-subject-123",
            "email": "linus@example.com",
            "email_verified": True,
            "given_name": "Linus",
            "family_name": "Torvalds",
        }

    monkeypatch.setattr(auth_api, "_google_exchange_code", fake_exchange_code)
    monkeypatch.setattr(auth_api, "_verify_google_id_token", fake_verify_id_token)

    callback_response = client.get(
        f"/web/auth/google/callback?code=oauth-code&state={state}",
        follow_redirects=False,
    )

    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/meetings"
    meetings_page = client.get("/meetings")
    assert meetings_page.status_code == 200
    assert "Signed in as <strong>linus@example.com</strong>" in meetings_page.text

    db = SessionLocal()
    try:
        identity = db.execute(
            text(
                """
                SELECT ai.provider_subject, u.email
                FROM auth_identities ai
                JOIN users u ON u.id = ai.user_id
                WHERE ai.provider = 'google'
                """
            )
        ).mappings().one()
    finally:
        db.close()

    assert identity["provider_subject"] == "google-subject-123"
    assert identity["email"] == "linus@example.com"
