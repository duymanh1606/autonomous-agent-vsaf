"""Which address the rate limiters are allowed to believe.

`client_ip()` used to return the left-most `X-Forwarded-For` entry. Both nginx
layers *append* to that header (`proxy_add_x_forwarded_for`), so the left-most
entry is whatever the client typed - which meant a fresh
`X-Forwarded-For: <random>` per request gave an attacker an unlimited number
of registration and password-reset buckets. Every IP-keyed limit in the
dashboard was decoration.

The replacement believes exactly two things: a peer that is not a trusted
proxy (it is talking to us directly, so its address is its address), and
`CF-Connecting-IP` on a request Cloudflare Access has verified (the Cloudflare
edge overwrites that header, so it cannot be forged from outside).

Everything else - notably an operator on an `ssh -L` tunnel - collapses to the
loopback address and shares one bucket. That is deliberate; these tests pin it
so nobody "fixes" it back into reading XFF.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.requests import Request

from dashboard_api import auth


def _request(headers: dict[str, str], peer: str | None = "127.0.0.1",
             cf_verified: bool = False) -> Request:
    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/register",
        "query_string": b"", "scheme": "http", "root_path": "", "app": None,
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in headers.items()
        ],
        "client": (peer, 40000) if peer else None,
        "state": {},
    }
    request = Request(scope)
    request.state.cf_access_verified = cf_verified
    return request


class ClientIpTrustTests(unittest.TestCase):
    def test_a_forwarded_for_header_is_never_believed(self):
        """The regression that matters: this header used to *be* the answer."""
        request = _request({"x-forwarded-for": "1.2.3.4"})

        self.assertEqual(auth.client_ip(request), "127.0.0.1")

    def test_a_forged_chain_is_not_believed_from_either_end(self):
        request = _request({"x-forwarded-for": "1.2.3.4, 5.6.7.8, 127.0.0.1"})

        self.assertEqual(auth.client_ip(request), "127.0.0.1")

    def test_a_direct_peer_is_its_own_answer(self):
        """Someone reaching the supervisor without going through a proxy: its
        own address is the truth and nothing it claims about itself counts."""
        request = _request(
            {"x-forwarded-for": "1.2.3.4", "cf-connecting-ip": "9.9.9.9"},
            peer="192.168.1.50", cf_verified=True,
        )

        self.assertEqual(auth.client_ip(request), "192.168.1.50")

    def test_cf_connecting_ip_is_used_only_once_access_verified_it(self):
        headers = {"cf-connecting-ip": "203.0.113.7"}

        unverified = auth.client_ip(_request(headers, cf_verified=False))
        verified = auth.client_ip(_request(headers, cf_verified=True))

        # Unverified, the header is just something the caller typed: a request
        # arriving on the local listener, or one the edge never touched, must
        # not be able to pick its own rate-limit bucket.
        self.assertEqual(unverified, "127.0.0.1")
        self.assertEqual(verified, "203.0.113.7")

    def test_an_empty_cf_header_falls_back_to_the_peer(self):
        request = _request({"cf-connecting-ip": "   "}, cf_verified=True)

        self.assertEqual(auth.client_ip(request), "127.0.0.1")

    def test_a_request_without_a_peer_is_not_a_crash(self):
        self.assertEqual(auth.client_ip(_request({}, peer=None)), "unknown")

    def test_the_trusted_proxy_list_is_configurable(self):
        with mock.patch.dict("os.environ", {"DASHBOARD_TRUSTED_PROXIES": "10.0.0.1"}):
            # 127.0.0.1 is no longer trusted, so its own address wins even
            # with a verified Access assertion.
            request = _request({"cf-connecting-ip": "203.0.113.7"}, cf_verified=True)
            self.assertEqual(auth.client_ip(request), "127.0.0.1")

            trusted = _request(
                {"cf-connecting-ip": "203.0.113.7"}, peer="10.0.0.1", cf_verified=True,
            )
            self.assertEqual(auth.client_ip(trusted), "203.0.113.7")


class RateLimiterKeyHygieneTests(unittest.TestCase):
    def test_an_exhausted_key_is_dropped_rather_than_left_empty(self):
        """A spray across many keys used to grow `_hits` forever - a slow leak
        on exactly the path that is supposed to contain abuse."""
        limiter = auth.RateLimiter(max_attempts=5, window_seconds=0.01)
        for index in range(50):
            limiter.hit(f"ip-{index}")
        self.assertEqual(len(limiter._hits), 50)

        # Once the window has passed, pruning must remove the keys, not blank
        # them: `check` is what runs on every request.
        with mock.patch("time.monotonic", return_value=1e6):
            for index in range(50):
                limiter.check(f"ip-{index}")

        self.assertEqual(limiter._hits, {})


if __name__ == "__main__":
    unittest.main()
