"""Where the hardening switches read their settings from.

Everything here reads `os.environ` **on every call** rather than at import
time. That is deliberate: `dashboard_api.auth` is imported before `.env` is
loaded in at least one entry point (see the ordering comment in
`create_admin.py`), and a module-level constant would freeze whatever the
environment happened to be at import. Call-time reads also let a test use
`mock.patch.dict(os.environ, ...)` without `importlib.reload`.

The one thing that cannot work this way is FastAPI's `docs_url`/`openapi_url`,
which the app object freezes at construction - `docs_urls()` is therefore read
once in `main.py` and assigned to module-level names it can assert on.

"Production" is opt-in and every switch below is off by default, so `dev.sh`
and the test suite keep today's behaviour without setting anything.
"""

import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _csv(name: str, default: str = "") -> list[str]:
    return [part.strip() for part in _env(name, default).split(",") if part.strip()]


def is_production() -> bool:
    """True when this process is serving something reachable from the internet.

    The second clause is a safety net rather than a convenience: an operator
    who sets up Cloudflare Access but forgets `DASHBOARD_ENV` still gets
    Secure cookies, no API docs, an empty CORS allowlist and the secret-key
    requirement. Forgetting the flag must not silently downgrade the origin.
    """
    if _env("DASHBOARD_ENV").lower() in {"production", "prod"}:
        return True
    return bool(cf_access_team_domain())


# --------------------------------------------------------------- sessions
# 7 days, not the 30 it used to be: an internet-facing dashboard should not
# hand out month-long bearer tokens. Sliding renewal is deliberately absent -
# a fixed ceiling is what makes "how long can a stolen cookie live" a number
# anyone can state.
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 3600


def session_ttl_seconds() -> int:
    raw = _env("DASHBOARD_SESSION_TTL_SECONDS")
    if not raw:
        return DEFAULT_SESSION_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SESSION_TTL_SECONDS
    # A zero or negative TTL would mint tokens that are already expired, which
    # reads as "nobody can log in" - refuse the footgun rather than obey it.
    return value if value > 0 else DEFAULT_SESSION_TTL_SECONDS


# ------------------------------------------------------- proxies / origins
def trusted_proxies() -> set[str]:
    """Peer addresses whose forwarding headers may be believed at all.

    In the supported topology every request arrives from loopback: cloudflared
    and the host-networked nginx container both talk to 127.0.0.1:28080. A
    peer that is *not* in this set is talking to the supervisor directly, and
    nothing it sends about its own identity is worth reading.
    """
    return set(_csv("DASHBOARD_TRUSTED_PROXIES", "127.0.0.1,::1"))


def trusted_origins() -> set[str]:
    """Origins allowed to make state-changing requests.

    `DASHBOARD_PUBLIC_URL` is folded in because it is the address operators
    actually configure and open; keeping the allowlist derived from it means
    the two cannot drift apart and lock everyone out of every POST.
    """
    origins = set(_csv("DASHBOARD_TRUSTED_ORIGINS"))
    public = _env("DASHBOARD_PUBLIC_URL").rstrip("/")
    if public:
        origins.add(public)
    return origins


def csrf_enabled() -> bool:
    """Origin checking is only enforced once an allowlist exists.

    Without this, every `TestClient` POST in the suite (none of which send an
    `Origin`) would start failing, and so would any curl an operator runs over
    an SSH tunnel. Configuring the allowlist is the act of opting in.
    """
    return bool(trusted_origins())


def cors_origins() -> list[str]:
    """Browser origins allowed to read credentialed responses.

    The Vite dev server pair is a development affordance; in production the
    SPA is same-origin behind nginx and nothing legitimate needs CORS at all.
    """
    configured = _csv("DASHBOARD_CORS_ORIGINS")
    if configured:
        return configured
    if is_production():
        return []
    return ["http://127.0.0.1:5173", "http://localhost:5173"]


# ------------------------------------------------------------------- docs
def docs_urls() -> tuple[str | None, str | None, str | None]:
    """`(docs_url, redoc_url, openapi_url)` for the FastAPI constructor.

    These paths sit outside `/api/`, so the auth middleware never guards them
    and the container nginx never proxies them - they are reachable only from
    the host or an `ssh -L` to the supervisor port. That is a small audience,
    but it is handed the complete route schema, so production turns them off.
    """
    if is_production():
        return (None, None, None)
    return ("/docs", "/redoc", "/openapi.json")


# ------------------------------------------------------ cloudflare access
def cf_access_team_domain() -> str:
    """e.g. `myteam.cloudflareaccess.com`. Empty disables the whole check."""
    return _env("CF_ACCESS_TEAM_DOMAIN").removeprefix("https://").rstrip("/")


def cf_access_aud() -> str:
    return _env("CF_ACCESS_AUD")


def cf_access_jwks_ttl_seconds() -> int:
    raw = _env("CF_ACCESS_JWKS_TTL_SECONDS", "3600")
    try:
        return max(60, int(raw))
    except ValueError:
        return 3600


def cf_access_enabled() -> bool:
    return bool(cf_access_team_domain())


def validate_at_startup() -> None:
    """Refuse to start on a configuration that only looks secure.

    Called from `main.main()`. Both cases below are ones where the process
    would otherwise come up and serve traffic while silently missing the
    protection the operator believes they configured.
    """
    if is_production() and not _env("DASHBOARD_SECRET_KEY"):
        raise RuntimeError(
            "DASHBOARD_SECRET_KEY must be set in production: without it every "
            "restart mints a new random signing key, which logs everyone out "
            "and makes session revocation untestable. Put a long random value "
            "in .env."
        )
    if cf_access_enabled() and not cf_access_aud():
        raise RuntimeError(
            "CF_ACCESS_TEAM_DOMAIN is set but CF_ACCESS_AUD is empty. Without "
            "the audience tag, any valid token from that Cloudflare team - "
            "including one issued for a different application - would be "
            "accepted here. Copy the Application Audience (AUD) tag from the "
            "Access application into .env."
        )
