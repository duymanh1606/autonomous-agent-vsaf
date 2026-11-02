"""Verify Cloudflare Access at the origin, not just at the edge.

Cloudflare Access already authenticates everyone before the tunnel forwards a
request, so this is defence in depth against the one failure that layer cannot
protect against: its own misconfiguration. Delete the Access application, or
point a second hostname at the same tunnel, and the edge starts forwarding
anonymous traffic to a dashboard that would happily show its login form. With
this middleware, the origin refuses anything the edge did not vouch for.

Two things this is deliberately *not*:

  * It is not authentication for the dashboard. A verified Access token says
    "Cloudflare let this person through", not "this person is user 7". The
    session cookie remains the only source of authorization, and the Access
    identity is never mapped onto a dashboard account.
  * It is not a firewall. The origin is loopback-only; this exists so that a
    request arriving on the tunnel listener without a valid Access token is
    rejected even though it comes from 127.0.0.1 like everything else.

Which path a request came in on cannot be inferred from its peer address -
cloudflared and an `ssh -L` tunnel both arrive from 127.0.0.1. nginx therefore
stamps `X-Dashboard-Edge` per server block (`cloudflare` on the tunnel
listener, `local` on the SSH one), overwriting whatever the client sent, and
that stamp is what selects the policy here.

Off unless configured: with `CF_ACCESS_TEAM_DOMAIN` unset every request is
passed through with `cf_access_verified = False`, so dev.sh and the test suite
are unaffected. Configured but missing the audience tag is a startup error -
see security_config.validate_at_startup().
"""

import asyncio
import logging
from typing import Any

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse

from dashboard_api import security_config

logger = logging.getLogger(__name__)

EDGE_HEADER = "x-dashboard-edge"
CLOUDFLARE_EDGE = "cloudflare"
ACCESS_TOKEN_HEADER = "cf-access-jwt-assertion"
# Cloudflare also sets this cookie; reading it as a fallback means a direct
# browser hit that kept the cookie still verifies.
ACCESS_TOKEN_COOKIE = "CF_Authorization"

_jwks_client: Any = None
_jwks_for_domain: str = ""


def _certs_url(team_domain: str) -> str:
    return f"https://{team_domain}/cdn-cgi/access/certs"


def jwks_client():
    """One cached PyJWKClient per team domain.

    PyJWKClient fetches over the network and caches the signing keys itself;
    rebuilding it per request would mean an HTTP round trip per request.
    """
    global _jwks_client, _jwks_for_domain
    team_domain = security_config.cf_access_team_domain()
    if _jwks_client is None or _jwks_for_domain != team_domain:
        _jwks_client = jwt.PyJWKClient(
            _certs_url(team_domain),
            cache_keys=True,
            lifespan=security_config.cf_access_jwks_ttl_seconds(),
        )
        _jwks_for_domain = team_domain
    return _jwks_client


class AccessError(Exception):
    """Verification failed. `reason` is safe to log, never to return."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def verify_token(token: str) -> dict[str, Any]:
    """Validate an Access assertion and return its claims.

    Blocking: PyJWKClient may do an HTTP fetch when its key cache is cold, so
    async callers must route this through `asyncio.to_thread` rather than
    stalling the event loop on a network round trip.

    RS256 is what Cloudflare signs with; `audience` is checked because without
    it *any* valid token from this Cloudflare team - including one minted for a
    completely different application - would be accepted here.
    """
    team_domain = security_config.cf_access_team_domain()
    try:
        signing_key = jwks_client().get_signing_key_from_jwt(token)
    except Exception as exc:
        # Distinct from a bad token on purpose: this one means the origin
        # cannot reach *.cloudflareaccess.com (or its cache went cold at a bad
        # moment), which is an infrastructure symptom, not an attack.
        raise AccessError(f"jwks_unavailable:{type(exc).__name__}") from exc
    try:
        return jwt.decode(
            token, signing_key.key, algorithms=["RS256"],
            audience=security_config.cf_access_aud(),
            issuer=f"https://{team_domain}",
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise AccessError(f"invalid_token:{type(exc).__name__}") from exc


def _token_from(headers, cookies) -> str:
    return (
        headers.get(ACCESS_TOKEN_HEADER)
        or cookies.get(ACCESS_TOKEN_COOKIE)
        or ""
    ).strip()


def _from_cloudflare_edge(headers) -> bool:
    return (headers.get(EDGE_HEADER) or "").strip().lower() == CLOUDFLARE_EDGE


async def verify_request(headers, cookies) -> tuple[bool, str]:
    """`(verified, reason)`. `reason` is empty when the request may proceed."""
    if not security_config.cf_access_enabled():
        return False, ""
    if not _from_cloudflare_edge(headers):
        # The local/SSH listener. Not verified, but not refused either - the
        # session cookie is still required by the auth middleware.
        return False, ""
    token = _token_from(headers, cookies)
    if not token:
        return False, "missing_assertion"
    try:
        claims = await asyncio.to_thread(verify_token, token)
    except AccessError as exc:
        return False, exc.reason
    logger.debug("[cf-access] verified email=%s", claims.get("email", "?"))
    return True, ""


def install_middleware(app) -> None:
    """Refuse unvouched tunnel traffic before anything else looks at it.

    Registered so that it runs *outside* the session middleware: an anonymous
    stranger at the edge then never reaches that middleware's per-request user
    SELECT, which is otherwise a very cheap way to make the database work.
    """
    @app.middleware("http")
    async def _cf_access_middleware(request: Request, call_next):
        verified, reason = await verify_request(request.headers, request.cookies)
        request.state.cf_access_verified = verified
        if reason:
            logger.warning(
                "[cf-access] rejected %s %s reason=%s",
                request.method, request.url.path, reason,
            )
            # 403, not 401: a 401 makes the SPA show its own login form, which
            # points the operator at the wrong layer entirely.
            return JSONResponse(
                {"detail": "cloudflare access required"}, status_code=403,
            )
        return await call_next(request)


async def websocket_allowed(websocket) -> bool:
    """WebSocket upgrades bypass HTTP middleware, so /api/events asks here."""
    _, reason = await verify_request(websocket.headers, websocket.cookies)
    if reason:
        logger.warning("[cf-access] rejected websocket reason=%s", reason)
        return False
    return True
