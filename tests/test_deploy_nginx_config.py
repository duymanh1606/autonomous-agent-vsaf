"""The dashboard's nginx config, checked as text.

Nothing else in the suite can see this file: it is rendered by the nginx
image's entrypoint at container start, long after pytest has finished. But it
carries three properties that are load-bearing for a public deployment and
silent when broken:

* the listen address - `network_mode: host` means there is no port mapping to
  restrict, so this line is the only thing keeping the dashboard off every
  interface on the machine;
* `X-Dashboard-Edge` - the marker that tells the supervisor whether a request
  came in over the tunnel (and therefore needs a Cloudflare Access assertion)
  or over an SSH tunnel. Both arrive from 127.0.0.1, so nothing else can;
* the nested media location - which loses every inherited `proxy_set_header`
  the moment somebody adds one of its own, and silently sends the media upload
  to the supervisor without a Host or an edge marker.

Text assertions are crude, but the alternative is discovering a config typo
during a deployment window.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "dashboard" / "nginx.conf.template"
COMPOSE = ROOT / "compose.yaml"


def _directives(text: str) -> str:
    """The config with comments stripped.

    Every assertion below runs on this: the file explains itself at length,
    and a comment that *mentions* `proxy_add_x_forwarded_for` must not read as
    the file still using it.
    """
    return "\n".join(re.sub(r"#.*", "", line) for line in text.splitlines())


def _server_blocks(text: str) -> list[str]:
    """Split on top-level `server {`. Crude, but the file is ours and flat."""
    parts = text.split("\nserver {")
    return [part for part in parts[1:]]


class NginxTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = TEMPLATE.read_text(encoding="utf-8")
        cls.text = _directives(cls.raw)
        cls.blocks = _server_blocks(cls.text)

    def test_there_are_two_listeners(self):
        """One for cloudflared, one for `ssh -L`. Collapsing them back into
        one would make the Access requirement lock the operator out."""
        self.assertEqual(len(self.blocks), 2)

    def test_every_listen_is_address_qualified(self):
        """A bare `listen 25173;` binds every interface - which is what the
        file did before, and what `ss -ltn` showed as 0.0.0.0:25173."""
        listens = re.findall(r"^\s*listen\s+(\S+);", self.text, re.MULTILINE)
        self.assertTrue(listens)
        for listen in listens:
            with self.subTest(listen=listen):
                self.assertIn(":", listen)
                self.assertRegex(listen, r"^(\$\{DASHBOARD_LISTEN_ADDR\}|127\.0\.0\.1):")

    def test_the_default_listen_address_is_loopback(self):
        compose = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("DASHBOARD_LISTEN_ADDR: ${DASHBOARD_LISTEN_ADDR:-127.0.0.1}", compose)
        self.assertIn("DASHBOARD_LOCAL_PORT: ${DASHBOARD_LOCAL_PORT:-25174}", compose)

    def test_the_forwarded_for_chain_is_replaced_not_appended(self):
        """`$proxy_add_x_forwarded_for` appends, leaving the client's own
        value at the front - the exact shape that made every IP-keyed rate
        limit bypassable with one header."""
        self.assertNotIn("proxy_add_x_forwarded_for", self.text)
        sets = re.findall(r"proxy_set_header X-Forwarded-For (\S+);", self.text)
        self.assertTrue(sets, "nothing sets X-Forwarded-For at all")
        self.assertEqual(set(sets), {"$remote_addr"})

    def test_each_listener_stamps_its_own_edge(self):
        tunnel, local = self.blocks
        self.assertIn("proxy_set_header X-Dashboard-Edge cloudflare;", tunnel)
        self.assertIn("proxy_set_header X-Dashboard-Edge local;", local)

    def test_the_forwarded_proto_is_derived_never_hardcoded(self):
        """Hardcoding `https` here made the session cookie `Secure` even when
        the browser was on plain HTTP - and a browser will not send a Secure
        cookie back over HTTP, so login appeared to succeed and immediately
        logged you out. It has to follow the real hop, with `$scheme` as the
        fallback when nothing in front states one.

        Asserted as "every occurrence derives it" rather than as a count: the
        count was 2, then 4 once the noVNC auth subrequest started stamping
        it too, and a number that has to be edited whenever a block is added
        stops being a guard and becomes a chore."""
        self.assertNotIn("X-Forwarded-Proto https", self.text)
        sets = re.findall(r"proxy_set_header X-Forwarded-Proto (\S+);", self.text)
        self.assertTrue(sets, "nothing forwards the scheme at all")
        self.assertEqual(set(sets), {"$fwd_proto"})
        self.assertIn("map $http_x_forwarded_proto $fwd_proto", self.text)
        self.assertIn("''      $scheme;", self.text)

    def test_the_edge_marker_is_set_not_passed_through(self):
        """If nginx ever forwarded a client-supplied X-Dashboard-Edge, anyone
        could claim to be Cloudflare - or claim to be local and skip Access.

        What matters is that every occurrence is a hardcoded literal, not how
        many there are: each listener now stamps it twice (once for /api/,
        once for the noVNC auth subrequest), and both must be literals."""
        for block, expected in zip(self.blocks, ("cloudflare", "local")):
            with self.subTest(edge=expected):
                stamps = re.findall(r"X-Dashboard-Edge (\S+);", block)
                self.assertTrue(stamps, "listener does not stamp an edge at all")
                # Never `$http_x_dashboard_edge` or any other variable, and
                # never the other listener's value.
                self.assertEqual(set(stamps), {expected})

    # ------------------------------------------------------------- noVNC
    def test_every_vnc_location_is_gated_by_auth_request(self):
        """The one that would be catastrophic and silent. A `location /vnc/`
        without `auth_request` proxies straight to a passwordless x11vnc -
        anyone who can open the dashboard would see every workspace's screen,
        and nothing about it would look broken."""
        for block, listener in zip(self.blocks, ("tunnel", "local")):
            with self.subTest(listener=listener):
                vnc = re.findall(
                    r"location ~ \^/vnc/.*?\{(.*?)\n    \}", block, re.DOTALL,
                )
                self.assertEqual(len(vnc), 1, "expected exactly one /vnc/ location")
                self.assertIn("auth_request /_vnc_auth;", vnc[0])

    def test_the_vnc_auth_subrequest_is_internal_on_both_listeners(self):
        """Without `internal`, /_vnc_auth answers anyone who asks and hands
        back the workspace-id-to-port mapping."""
        for block, listener in zip(self.blocks, ("tunnel", "local")):
            with self.subTest(listener=listener):
                auth = re.findall(
                    r"location = /_vnc_auth \{(.*?)\n    \}", block, re.DOTALL,
                )
                self.assertEqual(len(auth), 1)
                self.assertIn("internal;", auth[0])
                # A GET subrequest must not carry the original body along.
                self.assertIn("proxy_pass_request_body off;", auth[0])

    def test_the_vnc_port_comes_from_the_auth_response(self):
        """Hardcoding ports here would mean every new workspace needs an
        nginx edit and a reload before its screen works."""
        self.assertEqual(
            self.text.count(
                "auth_request_set $vnc_port $upstream_http_x_novnc_port;"
            ), 2,
        )
        self.assertIn("proxy_pass http://127.0.0.1:$vnc_port/", self.text)

    def test_the_vnc_stream_keeps_its_websocket_upgrade(self):
        """noVNC is a WebSocket. Without the upgrade pair the page loads and
        then hangs; without the long read timeout it dies after 60s idle."""
        for block, listener in zip(self.blocks, ("tunnel", "local")):
            with self.subTest(listener=listener):
                vnc = re.search(
                    r"location ~ \^/vnc/.*?\{(.*?)\n    \}", block, re.DOTALL,
                ).group(1)
                self.assertIn("proxy_set_header Upgrade $http_upgrade;", vnc)
                self.assertIn("proxy_set_header Connection $connection_upgrade;", vnc)
                self.assertIn("proxy_read_timeout 3600s;", vnc)

    def test_ordinary_api_traffic_has_a_small_body_limit(self):
        self.assertEqual(self.text.count("client_max_body_size 2m;"), 2)
        self.assertEqual(self.text.count("client_max_body_size 80m;"), 2)
        self.assertEqual(self.text.count("client_max_body_size 26m;"), 2)

    def test_the_upload_locations_repeat_proxy_pass_and_declare_no_headers(self):
        """Two nginx rules this nesting depends on: `proxy_set_header` is
        inherited only while the inner level declares none of its own, and
        `proxy_pass` is not inherited at all. Product media and chat
        attachments each have one per listener."""
        nested = re.findall(
            r"location ~ (\^/api/accounts\S*) \{(.*?)\n        \}", self.text, re.DOTALL,
        )
        limits = {
            "products/.+/media$": "client_max_body_size 80m;",
            "chat-attachments$": "client_max_body_size 26m;",
        }
        self.assertEqual(len(nested), 4)
        for pattern, body in nested:
            with self.subTest(pattern=pattern):
                self.assertIn("proxy_pass", body)
                self.assertNotIn("proxy_set_header", body)
                limit = next(v for k, v in limits.items() if pattern.endswith(k))
                self.assertIn(limit, body)

    def test_the_websocket_upgrade_headers_survive_in_both_blocks(self):
        for block in self.blocks:
            with self.subTest():
                self.assertIn("proxy_set_header Upgrade $http_upgrade;", block)
                self.assertIn("proxy_set_header Connection $connection_upgrade;", block)

    def test_the_two_api_blocks_only_differ_where_they_must(self):
        """Duplicated config drifts. The only intended differences are the
        edge marker and the forwarded protocol."""
        def normalise(block: str) -> str:
            body = block.split("location /api/", 1)[1]
            body = re.sub(r"#.*", "", body)
            body = body.replace("X-Dashboard-Edge cloudflare", "EDGE")
            body = body.replace("X-Dashboard-Edge local", "EDGE")
            return re.sub(r"\s+", " ", body).strip()

        self.assertEqual(normalise(self.blocks[0]), normalise(self.blocks[1]))


if __name__ == "__main__":
    unittest.main()
