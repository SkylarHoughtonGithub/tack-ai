"""
OIDC / OAuth2 SSO support via Authlib.

When OIDC is not configured (OIDC_CLIENT_ID is unset) this module is a no-op
and the existing username/password login works unchanged.

Supported providers:
  Google Workspace  — set OIDC_PROVIDER=google
  GitHub OAuth App  — set OIDC_PROVIDER=github
  Generic OIDC      — set OIDC_PROVIDER=oidc and OIDC_DISCOVERY_URL

Role mapping:
  All OIDC logins default to the "viewer" role.
  Set OIDC_ADMIN_EMAILS to a comma-separated list of email addresses that should
  receive the "admin" role automatically.

Required env vars:
  OIDC_CLIENT_ID       — OAuth2 client ID
  OIDC_CLIENT_SECRET   — OAuth2 client secret
  OIDC_PROVIDER        — google | github | oidc (default: google)
  OIDC_REDIRECT_BASE   — base URL of this app, e.g. https://tack.example.com
                         (default: http://localhost:8000)

Optional env vars:
  OIDC_DISCOVERY_URL   — required when OIDC_PROVIDER=oidc
  OIDC_ADMIN_EMAILS    — comma-separated admin email addresses
  OIDC_DEFAULT_ROLE    — viewer (default) | admin
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# ── Configuration ─────────────────────────────────────────────────────────────

OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_PROVIDER = os.environ.get("OIDC_PROVIDER", "google").lower()
OIDC_REDIRECT_BASE = os.environ.get("OIDC_REDIRECT_BASE", "http://localhost:8000").rstrip("/")
OIDC_DISCOVERY_URL = os.environ.get("OIDC_DISCOVERY_URL", "")
OIDC_DEFAULT_ROLE = os.environ.get("OIDC_DEFAULT_ROLE", "viewer")
_ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("OIDC_ADMIN_EMAILS", "").split(",")
    if e.strip()
}

OIDC_ENABLED = bool(OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)

REDIRECT_URI = f"{OIDC_REDIRECT_BASE}/auth/callback"

# Provider-specific defaults
_PROVIDER_META: dict[str, dict[str, str]] = {
    "google": {
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
        "email_claim": "email",
        "name_claim": "name",
    },
    "github": {
        "authorization_endpoint": "https://github.com/login/oauth/authorize",
        "token_endpoint": "https://github.com/login/oauth/access_token",
        "userinfo_endpoint": "https://api.github.com/user",
        "scope": "read:user user:email",
        "email_claim": "email",
        "name_claim": "login",
    },
}

_resolved_meta: dict[str, str] | None = None


async def _get_provider_meta() -> dict[str, str]:
    global _resolved_meta
    if _resolved_meta is not None:
        return _resolved_meta

    if OIDC_PROVIDER in _PROVIDER_META:
        _resolved_meta = dict(_PROVIDER_META[OIDC_PROVIDER])
        return _resolved_meta

    if OIDC_PROVIDER == "oidc":
        if not OIDC_DISCOVERY_URL:
            raise RuntimeError("OIDC_DISCOVERY_URL is required when OIDC_PROVIDER=oidc")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(OIDC_DISCOVERY_URL)
            resp.raise_for_status()
            doc = resp.json()
        _resolved_meta = {
            "authorization_endpoint": doc["authorization_endpoint"],
            "token_endpoint": doc["token_endpoint"],
            "userinfo_endpoint": doc.get("userinfo_endpoint", ""),
            "scope": "openid email profile",
            "email_claim": "email",
            "name_claim": "name",
        }
        return _resolved_meta

    raise ValueError(f"Unknown OIDC_PROVIDER: {OIDC_PROVIDER!r}. Use google, github, or oidc.")


# ── Authorization URL ─────────────────────────────────────────────────────────

async def get_authorization_url(state: str) -> str:
    """Return the provider's authorization URL to redirect the user to."""
    meta = await _get_provider_meta()
    import urllib.parse  # noqa: PLC0415
    params = {
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": meta["scope"],
        "state": state,
    }
    if OIDC_PROVIDER in ("google", "oidc"):
        params["access_type"] = "online"
    return meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)


# ── Token exchange ────────────────────────────────────────────────────────────

async def exchange_code(code: str) -> dict[str, Any]:
    """Exchange an authorization code for tokens. Returns the token response dict."""
    meta = await _get_provider_meta()
    payload = {
        "client_id": OIDC_CLIENT_ID,
        "client_secret": OIDC_CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    headers = {}
    if OIDC_PROVIDER == "github":
        headers["Accept"] = "application/json"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(meta["token_endpoint"], data=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


# ── User info ─────────────────────────────────────────────────────────────────

async def get_userinfo(access_token: str) -> dict[str, Any]:
    """Fetch user profile from the provider's userinfo endpoint."""
    meta = await _get_provider_meta()
    endpoint = meta.get("userinfo_endpoint", "")
    if not endpoint:
        return {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_github_primary_email(access_token: str) -> str | None:
    """GitHub doesn't always include email in the user object; fetch it separately."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://api.github.com/user/emails",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            return None
        for entry in resp.json():
            if entry.get("primary") and entry.get("verified"):
                return str(entry["email"])
    return None


# ── Identity resolution ───────────────────────────────────────────────────────

async def resolve_identity(code: str) -> tuple[str, str]:
    """
    Complete the OIDC flow and return (username, role).

    username is the provider email address (or GitHub login if no email).
    role is "admin" if the email is in OIDC_ADMIN_EMAILS, else OIDC_DEFAULT_ROLE.
    """
    meta = await _get_provider_meta()
    tokens = await exchange_code(code)
    if "error" in tokens:
        raise RuntimeError(f"Token exchange failed: {tokens.get('error_description', tokens['error'])}")

    access_token = tokens.get("access_token", "")
    userinfo = await get_userinfo(access_token)

    email_claim = meta.get("email_claim", "email")
    name_claim = meta.get("name_claim", "name")

    email = str(userinfo.get(email_claim, "")).lower()
    if not email and OIDC_PROVIDER == "github":
        email = await get_github_primary_email(access_token) or ""

    username = email or str(userinfo.get(name_claim, "")) or "oidc-user"
    role = "admin" if email in _ADMIN_EMAILS else OIDC_DEFAULT_ROLE
    return username, role
