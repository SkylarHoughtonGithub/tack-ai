# OIDC / SSO Configuration

Tack-AI supports single sign-on via OAuth2/OIDC. When enabled, users can log in with Google, GitHub, or any generic OIDC provider instead of (or alongside) the built-in username/password form.

OIDC is **off by default**. Set `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` in your `.env` to enable it. The built-in `admin / changeme` login continues to work regardless.

The current OIDC status is visible (read-only) under **Admin → Settings → SSO / OIDC**.

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OIDC_CLIENT_ID` | Yes (to enable) | — | OAuth2 client ID from your provider |
| `OIDC_CLIENT_SECRET` | Yes (to enable) | — | OAuth2 client secret |
| `OIDC_PROVIDER` | No | `google` | `google`, `github`, or `oidc` |
| `OIDC_REDIRECT_BASE` | No | `http://localhost:8000` | Public base URL of this app (no trailing slash) |
| `OIDC_DISCOVERY_URL` | Required when `OIDC_PROVIDER=oidc` | — | OIDC discovery document URL (`.well-known/openid-configuration`) |
| `OIDC_ADMIN_EMAILS` | No | — | Comma-separated list of emails that receive the `admin` role |
| `OIDC_DEFAULT_ROLE` | No | `viewer` | Role assigned to all other OIDC logins (`viewer` or `admin`) |

Add these to your `.env` file (never commit secrets to version control).

---

## Provider setup

### Google

1. Open [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services → Credentials**.
2. Create an **OAuth 2.0 Client ID** (application type: *Web application*).
3. Add an authorised redirect URI: `https://your-domain.com/auth/callback`
4. Copy the **Client ID** and **Client secret**.

```env
OIDC_PROVIDER=google
OIDC_CLIENT_ID=123456789-xxxx.apps.googleusercontent.com
OIDC_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxx
OIDC_REDIRECT_BASE=https://your-domain.com
OIDC_ADMIN_EMAILS=alice@example.com,bob@example.com
```

### GitHub

1. Open **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**.
2. Set **Authorization callback URL** to `https://your-domain.com/auth/callback`.
3. Copy the **Client ID** and generate a **Client secret**.

```env
OIDC_PROVIDER=github
OIDC_CLIENT_ID=Iv1.xxxxxxxxxxxx
OIDC_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxx
OIDC_REDIRECT_BASE=https://your-domain.com
OIDC_ADMIN_EMAILS=githubusername@example.com
```

> GitHub usernames don't always have a public email. Tack-AI calls the `/user/emails` API to retrieve the primary verified email for role mapping. Ensure the OAuth App has the `user:email` scope (it is requested automatically).

### Generic OIDC

Any provider that publishes a discovery document works. Examples: Okta, Auth0, Keycloak, Dex.

1. Register a new **web application / client** in your IdP.
2. Set the redirect URI to `https://your-domain.com/auth/callback`.
3. Copy the client credentials and the discovery document URL.

```env
OIDC_PROVIDER=oidc
OIDC_CLIENT_ID=your-client-id
OIDC_CLIENT_SECRET=your-client-secret
OIDC_REDIRECT_BASE=https://your-domain.com
OIDC_DISCOVERY_URL=https://your-idp.example.com/.well-known/openid-configuration
OIDC_ADMIN_EMAILS=admin@example.com
```

---

## Role mapping

| Condition | Assigned role |
|---|---|
| Email is in `OIDC_ADMIN_EMAILS` | `admin` |
| All other OIDC logins | `OIDC_DEFAULT_ROLE` (default: `viewer`) |

Roles control access to the **Admin** nav section, `/admin/*` routes, and the policy reload endpoint.

---

## Local development

For local testing, register a redirect URI of `http://localhost:8000/auth/callback` with your provider. Google and GitHub both support `localhost` redirect URIs for development credentials.

```env
OIDC_REDIRECT_BASE=http://localhost:8000
```

---

## Docker / production

In `docker-compose.yml`, pass the variables via the `environment:` block or an `env_file:`:

```yaml
services:
  app:
    env_file: .env
```

Do **not** bake secrets into the Docker image. Use secrets management (e.g. Docker secrets, Kubernetes secrets, AWS SSM) in production.
