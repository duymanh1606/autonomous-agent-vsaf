"""Provisioning against the egress-ip-gateway Admin API.

egw is replaced by an httpx.MockTransport that speaks its real contract
(status codes and JSON shapes from src/api/mod.rs). What is under test is the
order of calls, the one-IP-per-shop guard, token rotation on 409, and that
nothing here touches the environment at import time.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api import egw

ROUTES = [
    {"id": "route-01", "source_ip": "203.0.113.10", "enabled": True, "status": "HEALTHY",
     "observed_ip": "203.0.113.10", "last_check_at": None, "last_error": None},
    {"id": "route-02", "source_ip": "203.0.113.11", "enabled": True, "status": "UNHEALTHY",
     "observed_ip": None, "last_check_at": None, "last_error": "EADDRNOTAVAIL"},
    {"id": "route-03", "source_ip": "203.0.113.12", "enabled": False, "status": "UNKNOWN",
     "observed_ip": None, "last_check_at": None, "last_error": None},
]


class FakeEgw:
    """Just enough of egw's Admin API, recording every call in order."""

    def __init__(self, *, workspaces=None, live_labels=None):
        self.workspaces = dict(workspaces or {})
        self.live_labels = set(live_labels or ())
        self.calls = []
        self.issued = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.headers.get("X-Admin-Token") != "admin-secret":
            return httpx.Response(401, text="unauthorized")
        path = request.url.path
        if request.method == "GET" and path == "/v1/routes":
            return httpx.Response(200, json=ROUTES)
        if request.method == "GET" and path == "/v1/workspaces":
            return httpx.Response(200, json=[
                {"workspace_id": w, "route_id": r, "fallback_route_id": None,
                 "source": "api", "updated_at": "2026-09-18T00:00:00Z"}
                for w, r in self.workspaces.items()
            ])
        if request.method == "PUT" and path.startswith("/v1/workspaces/"):
            body = json.loads(request.content)
            if body["route"] not in {r["id"] for r in ROUTES}:
                return httpx.Response(400, text="route không tồn tại")
            self.workspaces[path.rsplit("/", 1)[1]] = body["route"]
            return httpx.Response(200, text="ok")
        if request.method == "POST" and path.endswith("/tokens"):
            ws = path.split("/")[3]
            label = json.loads(request.content)["label"]
            if (ws, label) in self.live_labels:
                return httpx.Response(409, text="label đã tồn tại và đang active")
            self.live_labels.add((ws, label))
            self.issued += 1
            return httpx.Response(200, json={
                "workspace_id": ws, "label": label,
                "credential_id": "c1", "token": f"tok-{self.issued}",
            })
        if request.method == "DELETE" and "/tokens/" in path:
            ws, label = path.split("/")[3], path.rsplit("/", 1)[1]
            if (ws, label) in self.live_labels:
                self.live_labels.remove((ws, label))
                return httpx.Response(200, text="ok")
            return httpx.Response(404, text="không tìm thấy token active")
        return httpx.Response(404)


class _EgwCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeEgw()
        self._transport_patch = mock.patch.object(egw, "_transport", httpx.MockTransport(self.fake.handler))
        self._transport_patch.start()
        self.addCleanup(self._transport_patch.stop)
        self._env = mock.patch.dict(os.environ, {"EGW_ENABLED": "1", "EGW_ADMIN_TOKEN": "admin-secret",
                                                 "EGW_ADMIN_URL": "http://egw.test"})
        self._env.start()
        self.addCleanup(self._env.stop)


class NotConfiguredTests(unittest.IsolatedAsyncioTestCase):
    async def test_nothing_is_required_until_a_call_needs_it(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("EGW_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertFalse(egw.configured())
            with self.assertRaises(egw.EgwNotConfigured):
                await egw.list_routes()
            with self.assertRaises(egw.EgwNotConfigured):
                await egw.provision("shop-x", "route-01")
            # Deleting a workspace on a machine without egw must not fail.
            self.assertFalse(await egw.revoke("shop-x"))

    async def test_blank_token_counts_as_unset(self):
        with mock.patch.dict(os.environ, {"EGW_ADMIN_TOKEN": "   "}):
            self.assertFalse(egw.configured())


class ListRoutesTests(_EgwCase):
    async def test_routes_are_annotated_with_their_holders(self):
        self.fake.workspaces = {"shop-a": "route-01"}
        routes = {r["id"]: r for r in await egw.list_routes()}
        self.assertEqual(routes["route-01"]["held_by"], ["shop-a"])
        self.assertEqual(routes["route-02"]["held_by"], [])
        self.assertEqual(routes["route-01"]["source_ip"], "203.0.113.10")
        self.assertEqual(routes["route-02"]["status"], "UNHEALTHY")

    async def test_wrong_admin_token_is_an_operator_facing_error(self):
        with mock.patch.dict(os.environ, {"EGW_ADMIN_TOKEN": "wrong"}):
            with self.assertRaises(egw.EgwError) as ctx:
                await egw.list_routes()
        self.assertIn("EGW_ADMIN_TOKEN", str(ctx.exception))


class ProvisionTests(_EgwCase):
    async def test_maps_before_issuing_and_returns_route_ip(self):
        result = await egw.provision("shop-a", "route-01")
        self.assertEqual(result, {"token": "tok-1", "route": "route-01",
                                  "expected_ip": "203.0.113.10"})
        writes = [c for c in self.fake.calls if c[0] in ("PUT", "POST")]
        self.assertEqual(writes, [
            ("PUT", "/v1/workspaces/shop-a"),
            ("POST", "/v1/workspaces/shop-a/tokens"),
        ])
        self.assertEqual(self.fake.workspaces, {"shop-a": "route-01"})

    async def test_refuses_a_route_held_by_another_workspace(self):
        self.fake.workspaces = {"shop-b": "route-01"}
        with self.assertRaises(egw.EgwError) as ctx:
            await egw.provision("shop-a", "route-01")
        self.assertIn("shop-b", str(ctx.exception))
        self.assertNotIn(("PUT", "/v1/workspaces/shop-a"), self.fake.calls)
        self.assertEqual(self.fake.issued, 0)

    async def test_reprovisioning_the_same_workspace_rotates_the_token(self):
        self.fake.workspaces = {"shop-a": "route-01"}
        self.fake.live_labels = {("shop-a", egw.TOKEN_LABEL)}
        result = await egw.provision("shop-a", "route-01", current_holder="route-01")
        self.assertEqual(result["token"], "tok-1")
        tail = [c for c in self.fake.calls if "/tokens" in c[1]]
        self.assertEqual(tail, [
            ("POST", "/v1/workspaces/shop-a/tokens"),      # 409
            ("DELETE", f"/v1/workspaces/shop-a/tokens/{egw.TOKEN_LABEL}"),
            ("POST", "/v1/workspaces/shop-a/tokens"),      # fresh token
        ])
        self.assertEqual(self.fake.live_labels, {("shop-a", egw.TOKEN_LABEL)})

    async def test_moving_a_workspace_to_a_free_route(self):
        self.fake.workspaces = {"shop-a": "route-01"}
        self.fake.live_labels = {("shop-a", egw.TOKEN_LABEL)}
        result = await egw.provision("shop-a", "route-02", current_holder="route-01")
        self.assertEqual(result["expected_ip"], "203.0.113.11")
        self.assertEqual(self.fake.workspaces, {"shop-a": "route-02"})

    async def test_unknown_and_disabled_routes_are_refused_before_any_write(self):
        for route in ("route-99", "route-03"):
            with self.assertRaises(egw.EgwError):
                await egw.provision("shop-a", route)
        self.assertEqual([c for c in self.fake.calls if c[0] != "GET"], [])


class RevokeTests(_EgwCase):
    async def test_revoke_is_idempotent(self):
        self.fake.live_labels = {("shop-a", egw.TOKEN_LABEL)}
        self.assertTrue(await egw.revoke("shop-a"))
        self.assertTrue(await egw.revoke("shop-a"))  # 404 now: still "gone"
        self.assertEqual(self.fake.live_labels, set())

    async def test_unreachable_egw_does_not_raise(self):
        def explode(request):
            raise httpx.ConnectError("refused", request=request)
        with mock.patch.object(egw, "_transport", httpx.MockTransport(explode)):
            self.assertFalse(await egw.revoke("shop-a"))


if __name__ == "__main__":
    unittest.main()
