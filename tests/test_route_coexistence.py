def _register_user(client, *, email: str = "ada@example.com", password: str = "supersecret123") -> str:
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
    return response.json()["access_token"]


def _web_login(client, *, email: str = "ada@example.com", password: str = "supersecret123") -> None:
    response = client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_meetings_page_and_api_can_coexist(client):
    token = _register_user(client)
    _web_login(client)

    page_response = client.get("/meetings")
    assert page_response.status_code == 200, page_response.text
    assert page_response.headers["content-type"].startswith("text/html")

    api_response = client.get("/api/meetings/", headers=_auth_headers(token))
    assert api_response.status_code == 200, api_response.text
    assert api_response.headers["content-type"].startswith("application/json")
    assert api_response.json() == []


def test_availability_page_and_api_can_coexist(client):
    token = _register_user(client, email="grace@example.com")
    _web_login(client, email="grace@example.com")

    page_response = client.get("/availability")
    assert page_response.status_code == 200, page_response.text
    assert page_response.headers["content-type"].startswith("text/html")

    api_get_response = client.get("/api/availability/", headers=_auth_headers(token))
    assert api_get_response.status_code == 200, api_get_response.text
    assert api_get_response.headers["content-type"].startswith("application/json")
    assert api_get_response.json() == []

    api_post_response = client.post(
        "/api/availability/",
        headers=_auth_headers(token),
        json=[{"day_of_week": 1, "start_time": "09:00:00", "end_time": "17:00:00"}],
    )
    assert api_post_response.status_code == 200, api_post_response.text
    assert len(api_post_response.json()) == 1


def test_groups_page_and_api_can_coexist(client):
    token = _register_user(client, email="linus@example.com")
    _web_login(client, email="linus@example.com")

    page_response = client.get("/groups")
    assert page_response.status_code == 200, page_response.text
    assert page_response.headers["content-type"].startswith("text/html")

    api_response = client.get("/groups/", headers=_auth_headers(token))
    assert api_response.status_code == 200, api_response.text
    assert api_response.headers["content-type"].startswith("application/json")
    assert api_response.json() == []

    create_response = client.post(
        "/api/groups/",
        headers=_auth_headers(token),
        json={"name": "Kernel Crew", "description": "Systems planning"},
    )
    assert create_response.status_code == 200, create_response.text
    group_id = create_response.json()["id"]

    detail_page_response = client.get(f"/groups/{group_id}")
    assert detail_page_response.status_code == 200, detail_page_response.text
    assert detail_page_response.headers["content-type"].startswith("text/html")
    assert "Kernel Crew" in detail_page_response.text

    api_detail_response = client.get(f"/api/groups/{group_id}", headers=_auth_headers(token))
    assert api_detail_response.status_code == 200, api_detail_response.text
    assert api_detail_response.headers["content-type"].startswith("application/json")
    assert api_detail_response.json()["name"] == "Kernel Crew"
