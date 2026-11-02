"""The switches that only flip once this dashboard faces the internet.

Everything in `dashboard_api/security_config.py` is off by default so that
`dev.sh` and this suite keep their behaviour without setting anything. That
convenience is also the risk: a production deployment that forgets one
variable looks exactly like a development one. These pin the two things that
make the difference detectable - the implied-production rule, and the startup
checks that refuse a configuration which only looks secure.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard_api.main as main
from dashboard_api import auth, security_config


def _env(**values):
    """Replace the whole environment: these functions read it directly, and a
    stray DASHBOARD_* from the developer's own .env would decide the result."""
    cleared = {
        name: "" for name in (
            "DASHBOARD_ENV", "DASHBOARD_SECRET_KEY", "DASHBOARD_PUBLIC_URL",
            "DASHBOARD_TRUSTED_ORIGINS", "DASHBOARD_CORS_ORIGINS",
            "DASHBOARD_SESSION_TTL_SECONDS",
            "CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD",
        )
    }
    return mock.patch.dict("os.environ", {**cleared, **values})


class IsProductionTests(unittest.TestCase):
    def test_unset_is_development(self):
        with _env():
            self.assertFalse(security_config.is_production())

    def test_the_flag_says_so(self):
        for value in ("production", "prod", "PRODUCTION"):
            with self.subTest(value=value), _env(DASHBOARD_ENV=value):
                self.assertTrue(security_config.is_production())

    def test_configuring_cloudflare_access_implies_production(self):
        """The safety net: an operator who sets up the edge but forgets the
        flag still gets Secure cookies, no docs and the secret-key check."""
        with _env(CF_ACCESS_TEAM_DOMAIN="team.cloudflareaccess.com"):
            self.assertTrue(security_config.is_production())

    def test_a_stray_value_is_not_production(self):
        with _env(DASHBOARD_ENV="staging-ish"):
            self.assertFalse(security_config.is_production())


class ProductionSwitchTests(unittest.TestCase):
    def test_docs_are_served_in_dev_and_withheld_in_production(self):
        with _env():
            self.assertEqual(
                security_config.docs_urls(), ("/docs", "/redoc", "/openapi.json"),
            )
        with _env(DASHBOARD_ENV="production"):
            self.assertEqual(security_config.docs_urls(), (None, None, None))

    def test_the_app_was_built_from_that_decision(self):
        """FastAPI freezes these at construction, so the app object is the
        only place the real answer lives."""
        self.assertEqual(main.app.openapi_url, main._OPENAPI_URL)
        self.assertEqual(main.app.docs_url, main._DOCS_URL)

    def test_cors_keeps_the_vite_pair_in_dev_and_nothing_in_production(self):
        with _env():
            self.assertIn("http://localhost:5173", security_config.cors_origins())
        with _env(DASHBOARD_ENV="production"):
            self.assertEqual(security_config.cors_origins(), [])

    def test_the_public_url_is_always_a_trusted_origin(self):
        """Derived rather than repeated: the address operators actually open
        cannot drift out of the allowlist and 403 every POST."""
        with _env(DASHBOARD_PUBLIC_URL="https://app.example.com/"):
            self.assertIn("https://app.example.com", security_config.trusted_origins())
            self.assertTrue(security_config.csrf_enabled())

    def test_origin_checking_is_off_until_an_allowlist_exists(self):
        with _env():
            self.assertFalse(security_config.csrf_enabled())

    def test_a_nonsense_session_ttl_falls_back_instead_of_locking_everyone_out(self):
        for value in ("0", "-1", "abc"):
            with self.subTest(value=value), _env(DASHBOARD_SESSION_TTL_SECONDS=value):
                self.assertEqual(
                    security_config.session_ttl_seconds(),
                    security_config.DEFAULT_SESSION_TTL_SECONDS,
                )


class CookieHardeningTests(unittest.TestCase):
    def test_production_marks_the_cookie_secure_without_asking_a_header(self):
        """An internet-facing origin must not decide this from a header the
        caller controls; over the tunnel the browser's hop is always HTTPS."""
        class _Response:
            def __init__(self):
                self.kwargs = None

            def set_cookie(self, *_args, **kwargs):
                self.kwargs = kwargs

        with _env(DASHBOARD_ENV="production"):
            response = _Response()
            auth.set_session_cookie(response, 1, None)

        self.assertTrue(response.kwargs["secure"])
        self.assertTrue(response.kwargs["httponly"])
        self.assertEqual(response.kwargs["samesite"], "lax")
        # Explicit, so it can never disagree with delete_cookie's path.
        self.assertEqual(response.kwargs["path"], "/")


class StartupValidationTests(unittest.TestCase):
    def test_production_without_a_signing_key_refuses_to_start(self):
        with _env(DASHBOARD_ENV="production"):
            with self.assertRaises(RuntimeError) as raised:
                security_config.validate_at_startup()
        self.assertIn("DASHBOARD_SECRET_KEY", str(raised.exception))

    def test_access_without_an_audience_refuses_to_start(self):
        """Without `aud`, any token from that Cloudflare team - including one
        minted for a different application - would be accepted here."""
        with _env(CF_ACCESS_TEAM_DOMAIN="team.cloudflareaccess.com",
                  DASHBOARD_SECRET_KEY="x" * 40):
            with self.assertRaises(RuntimeError) as raised:
                security_config.validate_at_startup()
        self.assertIn("CF_ACCESS_AUD", str(raised.exception))

    def test_a_complete_production_configuration_passes(self):
        with _env(DASHBOARD_ENV="production", DASHBOARD_SECRET_KEY="x" * 40,
                  CF_ACCESS_TEAM_DOMAIN="team.cloudflareaccess.com",
                  CF_ACCESS_AUD="aud-tag"):
            security_config.validate_at_startup()   # must not raise

    def test_development_needs_no_configuration_at_all(self):
        with _env():
            security_config.validate_at_startup()   # must not raise


class MiddlewareOrderTests(unittest.TestCase):
    def test_the_stack_runs_cors_access_csrf_session(self):
        """Registration order is the reverse of execution order, so this is
        exactly the kind of thing a later refactor reorders without noticing.

        Outermost first: CORS has to answer preflights before auth sees them
        and put its headers on a 401; Cloudflare Access has to reject unvouched
        traffic before the session middleware spends a database read on it.
        """
        names = [
            getattr(m.cls, "__name__", type(m.cls).__name__)
            for m in main.app.user_middleware
        ]
        dispatchers = [
            (m.kwargs.get("dispatch") or (m.args[0] if m.args else None))
            for m in main.app.user_middleware
        ]
        labels = [
            getattr(dispatch, "__name__", name)
            for name, dispatch in zip(names, dispatchers)
        ]

        self.assertEqual(labels[0], "CORSMiddleware")
        self.assertEqual(
            labels[1:],
            ["_cf_access_middleware", "_csrf_middleware", "_auth_middleware"],
        )


if __name__ == "__main__":
    unittest.main()
