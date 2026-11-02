"""Origin-side verification of Cloudflare Access.

Access authenticates at the edge, so this layer exists for one failure the
edge cannot cover: its own misconfiguration. Delete the Access application, or
point a second hostname at the same tunnel, and anonymous internet traffic
starts arriving at a dashboard that would show it a login form.

The awkward part of this deployment is that cloudflared and an `ssh -L` tunnel
both reach the supervisor from 127.0.0.1, so the peer address cannot say which
one a request came in on. nginx stamps `X-Dashboard-Edge` per listener and
that stamp selects the policy - which is why "cloudflare with no assertion" is
a 403 while "local with no assertion" is fine.

A real RS256 keypair is generated here and PyJWKClient is replaced with one
that hands back its public half, so the signature path is genuinely exercised
rather than stubbed away.
"""

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import dashboard_api.main as main
from dashboard_api import cf_access, security_config


TEAM = "team.cloudflareaccess.com"
AUD = "aud-tag-for-this-application"


def _configured(**extra):
    return mock.patch.dict(
        "os.environ",
        {"CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": AUD, **extra},
    )


class CloudflareAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        # PyJWKClient would otherwise fetch https://<team>/cdn-cgi/access/certs.
        patcher = mock.patch.object(
            cf_access, "jwks_client",
            return_value=SimpleNamespace(
                get_signing_key_from_jwt=lambda _token: SimpleNamespace(
                    key=self.key.public_key()
                ),
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _token(self, *, aud=AUD, issuer=f"https://{TEAM}", expires_in=3600,
               email="op@example.com"):
        now = int(time.time())
        return jwt.encode(
            {"aud": aud, "iss": issuer, "iat": now, "exp": now + expires_in,
             "email": email},
            self.key, algorithm="RS256",
        )

    # ------------------------------------------------------- token itself
    def test_a_well_formed_token_verifies(self):
        with _configured():
            claims = cf_access.verify_token(self._token())
        self.assertEqual(claims["email"], "op@example.com")

    def test_a_token_for_another_application_is_refused(self):
        """Without the audience check, any token from this Cloudflare team -
        including one minted for a completely different app - would pass."""
        with _configured():
            with self.assertRaises(cf_access.AccessError) as raised:
                cf_access.verify_token(self._token(aud="some-other-application"))
        self.assertIn("invalid_token", raised.exception.reason)

    def test_an_expired_token_is_refused(self):
        with _configured():
            with self.assertRaises(cf_access.AccessError):
                cf_access.verify_token(self._token(expires_in=-60))

    def test_a_token_from_another_team_is_refused(self):
        with _configured():
            with self.assertRaises(cf_access.AccessError):
                cf_access.verify_token(self._token(issuer="https://evil.cloudflareaccess.com"))

    def test_a_token_signed_by_someone_else_is_refused(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        forged = jwt.encode(
            {"aud": AUD, "iss": f"https://{TEAM}", "iat": now, "exp": now + 60},
            other, algorithm="RS256",
        )
        with _configured():
            with self.assertRaises(cf_access.AccessError):
                cf_access.verify_token(forged)


class EdgePolicyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        patcher = mock.patch.object(
            cf_access, "jwks_client",
            return_value=SimpleNamespace(
                get_signing_key_from_jwt=lambda _token: SimpleNamespace(
                    key=self.key.public_key()
                ),
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _token(self):
        now = int(time.time())
        return jwt.encode(
            {"aud": AUD, "iss": f"https://{TEAM}", "iat": now, "exp": now + 60},
            self.key, algorithm="RS256",
        )

    async def test_the_tunnel_listener_demands_an_assertion(self):
        with _configured():
            verified, reason = await cf_access.verify_request(
                {"x-dashboard-edge": "cloudflare"}, {},
            )
        self.assertFalse(verified)
        self.assertEqual(reason, "missing_assertion")

    async def test_the_tunnel_listener_accepts_a_valid_assertion(self):
        with _configured():
            verified, reason = await cf_access.verify_request(
                {"x-dashboard-edge": "cloudflare",
                 "cf-access-jwt-assertion": self._token()}, {},
            )
        self.assertTrue(verified)
        self.assertEqual(reason, "")

    async def test_the_local_listener_needs_nothing(self):
        """The `ssh -L` path. If this required an assertion, an operator
        would be locked out of the one route that works when Cloudflare is
        down - which is the whole reason it exists."""
        with _configured():
            verified, reason = await cf_access.verify_request(
                {"x-dashboard-edge": "local"}, {},
            )
        self.assertFalse(verified)
        self.assertEqual(reason, "")

    async def test_the_cookie_is_accepted_in_place_of_the_header(self):
        with _configured():
            verified, _ = await cf_access.verify_request(
                {"x-dashboard-edge": "cloudflare"},
                {cf_access.ACCESS_TOKEN_COOKIE: self._token()},
            )
        self.assertTrue(verified)

    async def test_unconfigured_means_out_of_the_way(self):
        with mock.patch.dict("os.environ", {"CF_ACCESS_TEAM_DOMAIN": ""}):
            verified, reason = await cf_access.verify_request(
                {"x-dashboard-edge": "cloudflare"}, {},
            )
        self.assertFalse(verified)
        self.assertEqual(reason, "")

    async def test_an_unreachable_jwks_is_reported_as_itself(self):
        """Distinct from a bad token on purpose: this one is an
        infrastructure symptom, and on-call needs to tell them apart."""
        def _boom(_token):
            raise ConnectionError("dns")

        with _configured(), mock.patch.object(
            cf_access, "jwks_client",
            return_value=SimpleNamespace(get_signing_key_from_jwt=_boom),
        ):
            _verified, reason = await cf_access.verify_request(
                {"x-dashboard-edge": "cloudflare",
                 "cf-access-jwt-assertion": "whatever"}, {},
            )
        self.assertIn("jwks_unavailable", reason)


class AccessMiddlewareTests(unittest.TestCase):
    """The same policy, seen from a request."""

    def setUp(self):
        patches = [
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.registry, "list", return_value=[]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_unvouched_tunnel_traffic_never_reaches_the_route(self):
        with _configured(), TestClient(main.app) as client:
            response = client.get(
                "/api/system", headers={"x-dashboard-edge": "cloudflare"},
            )

        # 403, not 401: a 401 makes the SPA show its own login form, which
        # points the operator at the wrong layer entirely.
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "cloudflare access required")

    def test_local_traffic_is_left_to_the_session_check(self):
        with _configured(), TestClient(main.app) as client:
            response = client.get("/api/system", headers={"x-dashboard-edge": "local"})

        self.assertEqual(response.status_code, 401)

    def test_a_client_supplied_edge_header_is_the_one_nginx_overwrites(self):
        """This test documents the dependency rather than a code path: the
        marker is only trustworthy because both nginx server blocks set it
        unconditionally (tests/test_deploy_nginx_config.py pins that). If it
        were ever passed through, claiming `local` would skip Access."""
        with _configured(), TestClient(main.app) as client:
            claimed_local = client.get(
                "/api/system", headers={"x-dashboard-edge": "local"},
            )
        self.assertEqual(claimed_local.status_code, 401)

    def test_startup_refuses_a_team_domain_without_an_audience(self):
        with mock.patch.dict(
            "os.environ", {"CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": ""},
        ):
            with self.assertRaises(RuntimeError):
                security_config.validate_at_startup()


if __name__ == "__main__":
    unittest.main()
