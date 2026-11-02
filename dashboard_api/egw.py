"""Client for the egress-ip-gateway Admin API (one public IP per workspace).

Contract and the egw side of it: docs/egress-proxy-integration.md and
~/code/egress-ip-gateway/deploy/autonomous-agent.md.

Two rules this module exists to enforce:

* Nothing here is read at import or at supervisor startup. `EGW_ADMIN_TOKEN`
  is looked up inside the call that needs it, so a machine that has never
  heard of egw boots the dashboard exactly as before; `configured()` is how
  the UI learns whether to offer egw at all.
* The Admin API is called with `httpx.AsyncClient`. The supervisor is
  FastAPI: a blocking client here would stall the WebSocket hub and health
  monitor of every other workspace for the duration of the call.
"""

import logging
import os

import httpx

from dashboard_api.accounts import egw_feature_enabled

logger = logging.getLogger(__name__)

# One label per workspace; egw refuses a second active token with the same
# label (409), which is what makes re-provisioning a deliberate rotation
# (revoke, then issue) rather than a silent pile-up of live tokens.
TOKEN_LABEL = "agent-prod"
DEFAULT_ADMIN_URL = "http://127.0.0.1:9090"


# Tests replace this with an httpx.MockTransport standing in for egw.
_transport: httpx.AsyncBaseTransport | None = None


class EgwNotConfigured(RuntimeError):
    """The operator asked for egw on a machine without EGW_ADMIN_TOKEN."""


class EgwError(RuntimeError):
    """egw answered, but not with what we needed; message is operator-facing."""


def configured() -> bool:
    """Feature switched on AND this machine can talk to egw's Admin API."""
    return egw_feature_enabled() and bool(os.environ.get("EGW_ADMIN_TOKEN", "").strip())


def _client() -> httpx.AsyncClient:
    if not egw_feature_enabled():
        raise EgwNotConfigured("EGW_ENABLED chưa bật trong .env - tính năng IP riêng đang tắt.")
    token = os.environ.get("EGW_ADMIN_TOKEN", "").strip()
    if not token:
        raise EgwNotConfigured(
            "EGW_ADMIN_TOKEN chưa được đặt trên máy này - egw chưa được cài hoặc "
            "chưa nối với autonomous-agent (xem docs/egress-proxy-integration.md)."
        )
    base = os.environ.get("EGW_ADMIN_URL", DEFAULT_ADMIN_URL).rstrip("/")
    return httpx.AsyncClient(
        base_url=base, timeout=10,
        headers={"X-Admin-Token": token, "content-type": "application/json"},
        # Never let a proxy env var on the host route Admin API calls anywhere.
        trust_env=False,
        transport=_transport,
    )


async def list_routes() -> list[dict]:
    """Every route with its health and who currently holds it.

    Two Admin API calls joined here so the UI can offer only free, HEALTHY
    routes: egw itself has no "is this route taken" concept (several
    workspaces may legally share a route); one-IP-per-shop is this project's
    rule, enforced by refusing to provision onto a held route below.
    """
    async with _client() as client:
        routes_resp, ws_resp = await client.get("/v1/routes"), await client.get("/v1/workspaces")
    _raise_for(routes_resp, "đọc danh sách route")
    _raise_for(ws_resp, "đọc mapping workspace")
    holders: dict[str, list[str]] = {}
    for row in ws_resp.json():
        holders.setdefault(row["route_id"], []).append(row["workspace_id"])
    result = []
    for route in routes_resp.json():
        result.append({
            "id": route["id"],
            "source_ip": route["source_ip"],
            "status": route["status"],
            "enabled": route["enabled"],
            "last_error": route.get("last_error"),
            "held_by": holders.get(route["id"], []),
        })
    return result


async def provision(workspace_id: str, route_id: str, *, current_holder: str = "") -> dict:
    """Map `workspace_id` onto `route_id` and issue its token.

    Order is load-bearing: `PUT` the mapping first, `POST` the token second.
    egw has no FK between credentials and mappings, so the reverse order
    "works" and then every CONNECT gets 403. Returns `{"token", "route",
    "expected_ip"}`; the token plaintext exists only in this return value.
    """
    async with _client() as client:
        routes = {r["id"]: r for r in (await _ok(client.get("/v1/routes"), "đọc route")).json()}
        route = routes.get(route_id)
        if route is None:
            raise EgwError(f"route '{route_id}' không tồn tại trên egw")
        if not route["enabled"]:
            raise EgwError(f"route '{route_id}' đã bị tắt trong config egw")
        holders = [
            w["workspace_id"]
            for w in (await _ok(client.get("/v1/workspaces"), "đọc mapping")).json()
            if w["route_id"] == route_id and w["workspace_id"] != workspace_id
        ]
        if holders and holders != [current_holder]:
            raise EgwError(
                f"route '{route_id}' đang được workspace {', '.join(holders)} dùng - "
                "mỗi shop một IP, chọn route khác hoặc gỡ egw khỏi workspace kia trước."
            )

        await _ok(client.put(f"/v1/workspaces/{workspace_id}", json={"route": route_id}),
                  "gán route")

        issue = await client.post(f"/v1/workspaces/{workspace_id}/tokens",
                                  json={"label": TOKEN_LABEL})
        if issue.status_code == 409:
            # A live token with our label already exists (re-provision, or a
            # previous attempt that failed after issuing). Rotate: revoke the
            # old one, then issue again - never leave two live tokens.
            await client.delete(f"/v1/workspaces/{workspace_id}/tokens/{TOKEN_LABEL}")
            issue = await client.post(f"/v1/workspaces/{workspace_id}/tokens",
                                      json={"label": TOKEN_LABEL})
        _raise_for(issue, "cấp token")

    logger.info("[egw] provisioned workspace=%s route=%s ip=%s",
                workspace_id, route_id, route["source_ip"])
    return {
        "token": issue.json()["token"],
        "route": route_id,
        "expected_ip": route["source_ip"],
    }


async def revoke(workspace_id: str) -> bool:
    """Revoke the workspace's token. Best-effort by design.

    Returns False (and logs) instead of raising: a workspace being deleted
    must not survive because egw was unreachable, and a token that is
    already gone (404) is the outcome we wanted anyway. The route mapping is
    left in place - with no live token it is inert, and `provision()` reuses
    or reassigns it explicitly.
    """
    if not configured():
        logger.warning("[egw] cannot revoke token for %s: EGW_ADMIN_TOKEN unset here", workspace_id)
        return False
    try:
        async with _client() as client:
            resp = await client.delete(f"/v1/workspaces/{workspace_id}/tokens/{TOKEN_LABEL}")
    except httpx.HTTPError as exc:
        logger.warning("[egw] revoke failed for %s error_type=%s", workspace_id, type(exc).__name__)
        return False
    if resp.status_code in (200, 404):
        logger.info("[egw] token revoked workspace=%s (status=%s)", workspace_id, resp.status_code)
        return True
    logger.warning("[egw] revoke for %s answered %s: %s", workspace_id, resp.status_code, resp.text[:200])
    return False


async def _ok(request, what: str) -> httpx.Response:
    response = await request
    _raise_for(response, what)
    return response


def _raise_for(response: httpx.Response, what: str) -> None:
    if response.is_success:
        return
    if response.status_code in (401, 403):
        raise EgwError(f"egw từ chối {what}: EGW_ADMIN_TOKEN không khớp với token trên egw")
    raise EgwError(f"egw không thể {what} (HTTP {response.status_code}): {response.text[:200]}")
