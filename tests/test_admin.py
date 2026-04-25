"""Auth + admin route tests."""
from __future__ import annotations

from fastapi.testclient import TestClient

# Browser-like Accept header — our 401->303 redirect only fires for HTML clients.
HTML = {"Accept": "text/html"}


# -- Login flow ---------------------------------------------------------------


def test_unauthenticated_admin_redirects_to_login(client: TestClient) -> None:
    r = client.get("/admin/chores", headers=HTML, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_unauthenticated_api_client_gets_401(client: TestClient) -> None:
    # Non-HTML clients see the real status code.
    r = client.get("/admin/chores", headers={"Accept": "application/json"})
    assert r.status_code == 401


def test_login_with_bad_password_returns_401(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"name": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text


def test_login_with_good_password_sets_session(client: TestClient) -> None:
    r = client.post(
        "/login",
        data={"name": "admin", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"


def test_logout_clears_session(client: TestClient) -> None:
    client.post("/login", data={"name": "admin", "password": "hunter2"})
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    after = client.get("/admin/chores", headers=HTML, follow_redirects=False)
    assert after.status_code == 303
    assert after.headers["location"] == "/login"


# -- Chores CRUD --------------------------------------------------------------


def _login(client: TestClient) -> None:
    r = client.post("/login", data={"name": "admin", "password": "hunter2"})
    assert r.status_code in (200, 303)


def test_chores_list_renders_when_authenticated(client: TestClient) -> None:
    _login(client)
    r = client.get("/admin/chores")
    assert r.status_code == 200
    assert "Chores" in r.text
    assert "No chores yet" in r.text


def test_create_chore_round_trip(client: TestClient) -> None:
    _login(client)
    r = client.post(
        "/admin/chores",
        data={
            "name": "Vacuum",
            "description": "Living room rug",
            "frequency_days": "7",
            "priority": "1",
            "estimated_minutes": "20",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    listing = client.get("/admin/chores").text
    assert "Vacuum" in listing
    assert "every 7d" in listing
    assert "high" in listing


def test_create_chore_rejects_zero_frequency(client: TestClient) -> None:
    _login(client)
    r = client.post(
        "/admin/chores",
        data={"name": "Bogus", "frequency_days": "0", "priority": "0"},
    )
    assert r.status_code == 400
    assert "Frequency" in r.text


def test_edit_chore_updates_fields(client: TestClient) -> None:
    _login(client)
    client.post(
        "/admin/chores",
        data={"name": "Mop", "frequency_days": "14", "priority": "0", "enabled": "1"},
    )
    edit = client.get("/admin/chores/1/edit")
    assert edit.status_code == 200
    assert "Mop" in edit.text

    r = client.post(
        "/admin/chores/1",
        data={"name": "Mop floors", "frequency_days": "10", "priority": "1", "enabled": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    after = client.get("/admin/chores").text
    assert "Mop floors" in after
    assert "every 10d" in after


def test_delete_chore_removes_it(client: TestClient) -> None:
    _login(client)
    client.post(
        "/admin/chores",
        data={"name": "Trash", "frequency_days": "3", "priority": "0", "enabled": "1"},
    )
    r = client.post("/admin/chores/1/delete", follow_redirects=False)
    assert r.status_code == 303
    after = client.get("/admin/chores").text
    assert "Trash" not in after
    assert "No chores yet" in after


# -- Members ------------------------------------------------------------------


def test_members_list_shows_admin(client: TestClient) -> None:
    _login(client)
    r = client.get("/admin/members")
    assert r.status_code == 200
    assert "admin" in r.text


def test_admin_cannot_deactivate_self(client: TestClient) -> None:
    _login(client)
    r = client.post("/admin/members/1/toggle", follow_redirects=False)
    assert r.status_code == 400
