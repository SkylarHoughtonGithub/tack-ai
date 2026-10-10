"""
Playwright E2E tests for the Tack-AI web console.

Requires:
  - Web server running at BASE_URL (default http://localhost:8765)
  - WEB_USERNAME / WEB_PASSWORD env vars set
  - `uv run playwright install --with-deps chromium` run once

Run with:
  BASE_URL=http://localhost:8765 uv run pytest tests/e2e/ -v
"""

import os

import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
USERNAME = os.environ.get("WEB_USERNAME", "admin")
PASSWORD = os.environ.get("WEB_PASSWORD", "changeme")


def test_login_page_renders(page: Page):
    page.goto(f"{BASE_URL}/login")
    expect(page.locator("h1")).to_contain_text("TACK-AI")
    expect(page.locator("input[name='username']")).to_be_visible()
    expect(page.locator("input[name='password']")).to_be_visible()


def test_invalid_login_shows_error(page: Page):
    page.goto(f"{BASE_URL}/login")
    page.fill("input[name='username']", USERNAME)
    page.fill("input[name='password']", "wrong-password")
    page.click("button[type='submit']")
    expect(page).to_have_url(f"{BASE_URL}/login?error=Invalid+credentials")


def test_valid_login_redirects_to_home(page: Page):
    page.goto(f"{BASE_URL}/login")
    page.fill("input[name='username']", USERNAME)
    page.fill("input[name='password']", PASSWORD)
    page.click("button[type='submit']")
    expect(page).to_have_url(f"{BASE_URL}/")
    expect(page.locator("nav")).to_contain_text("tack-ai")


def test_unauthenticated_access_redirects_to_login(page: Page):
    page.goto(f"{BASE_URL}/")
    expect(page).to_have_url(f"{BASE_URL}/login")


def test_audit_page_accessible_after_login(page: Page):
    page.goto(f"{BASE_URL}/login")
    page.fill("input[name='username']", USERNAME)
    page.fill("input[name='password']", PASSWORD)
    page.click("button[type='submit']")
    page.click("#dd-compliance button")
    page.click("a[href='/audit'].dd-item")
    expect(page).to_have_url(f"{BASE_URL}/audit")


def test_approvals_page_accessible_after_login(page: Page):
    page.goto(f"{BASE_URL}/login")
    page.fill("input[name='username']", USERNAME)
    page.fill("input[name='password']", PASSWORD)
    page.click("button[type='submit']")
    page.click("#dd-compliance button")
    page.click("a[href='/approvals'].dd-item")
    expect(page).to_have_url(f"{BASE_URL}/approvals")
    expect(page.locator("h1")).to_contain_text("Approvals")


def test_logout_redirects_to_login(page: Page):
    page.goto(f"{BASE_URL}/login")
    page.fill("input[name='username']", USERNAME)
    page.fill("input[name='password']", PASSWORD)
    page.click("button[type='submit']")
    page.click("text=logout")
    expect(page).to_have_url(f"{BASE_URL}/login")


def test_admin_users_page_visible_for_admin(page: Page):
    page.goto(f"{BASE_URL}/login")
    page.fill("input[name='username']", USERNAME)
    page.fill("input[name='password']", PASSWORD)
    page.click("button[type='submit']")
    page.click("#dd-admin button")
    page.click("a[href='/admin/users'].dd-item")
    expect(page).to_have_url(f"{BASE_URL}/admin/users")
    expect(page.locator("h1")).to_contain_text("User Management")
