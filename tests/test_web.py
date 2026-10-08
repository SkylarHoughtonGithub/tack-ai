"""
Integration tests for the web console using FastAPI's TestClient.

These tests mock the agent run so no Anthropic API key or OPA server is needed.
"""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
os.environ.setdefault("WEB_USERNAME", "admin")
os.environ.setdefault("WEB_PASSWORD", "testpassword")


@pytest.fixture()
def client():
    from tack_ai.web import app
    return TestClient(app, follow_redirects=False)


def _login(client: TestClient) -> TestClient:
    resp = client.post("/login", data={"username": "admin", "password": "testpassword"})
    assert resp.status_code == 303
    return client


def test_login_redirect_unauthenticated(client):
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_invalid_credentials(client):
    resp = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


def test_login_valid_credentials(client):
    resp = client.post("/login", data={"username": "admin", "password": "testpassword"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "session" in resp.cookies


def test_index_after_login(client):
    _login(client)
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert "tack-ai" in resp.text.lower()


def test_audit_page_requires_auth(client):
    resp = client.get("/audit")
    assert resp.status_code == 303


def test_approvals_page_requires_auth(client):
    resp = client.get("/approvals")
    assert resp.status_code == 303


def test_logout_clears_session(client):
    _login(client)
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    # After logout, accessing a protected page should redirect to login
    resp2 = client.get("/", follow_redirects=False)
    assert resp2.status_code == 303
    assert "/login" in resp2.headers.get("location", "")


def test_admin_users_page_requires_auth(client):
    resp = client.get("/admin/users")
    assert resp.status_code == 303


def test_admin_users_page_accessible_as_admin(client):
    _login(client)
    resp = client.get("/admin/users", follow_redirects=True)
    assert resp.status_code == 200
    assert "admin" in resp.text.lower()


def test_chat_requires_auth(client):
    resp = client.get("/chat", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers.get("location", "")
