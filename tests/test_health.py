"""Smoke tests for the FastAPI scaffolding."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "choreizo"
    assert "version" in body


def test_root_redirects_unauthenticated_to_login(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Choreizo" in response.text
    assert "<!doctype html>" in response.text.lower()
    assert "Sign in" in response.text


def test_root_no_follow_returns_303(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
