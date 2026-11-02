"""What `/api/accounts/{id}/conversation-media` is allowed to serve.

Inbox pictures come off this server's disk from two places - the account's
product catalogue (what the agent sent) and its media cache (what
`services/media_cache.py` downloaded out of the thread). The `path` in the
request comes from `media_json`, which is written from the page's DOM, so the
route has to treat it as untrusted input and not as a filename it may open.
"""

import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from dashboard_api import auth, main
from storage.product_repository import ProductRepository


class ConversationMediaRouteTests(unittest.TestCase):
    ACCOUNT = "acc_media"

    def setUp(self):
        self.products_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.products_dir.cleanup)
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.outside = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside.cleanup)

        self.catalogue_file = Path(self.products_dir.name) / "sp001" / "images" / "a.png"
        self.catalogue_file.parent.mkdir(parents=True)
        self.catalogue_file.write_bytes(b"catalogue-bytes")
        self.cached_file = Path(self.media_dir.name) / "abc123.webp"
        self.cached_file.write_bytes(b"cached-bytes")
        self.secret = Path(self.outside.name) / "env"
        self.secret.write_bytes(b"GEMINI_API_KEY=very-secret")

        patches = [
            mock.patch.object(main, "_account_or_404", return_value=mock.Mock(id=self.ACCOUNT)),
            mock.patch.object(
                main, "_products", return_value=ProductRepository(self.products_dir.name)
            ),
            mock.patch.object(
                main, "get_account_media_dir", return_value=Path(self.media_dir.name)
            ),
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            # `decode_session` (not `decode_session_token`) is what the auth
            # middleware and the /api/events handler read: it returns
            # `(user_id, issued_at)`, the second half being what a revocation
            # check compares against. issued_at=0 with a row that carries no
            # `sessions_valid_from` means "never revoked".
            mock.patch.object(main.auth, "decode_session", return_value=(1, 0)),
            mock.patch.object(
                main.users_store, "get_by_id",
                return_value={"id": 1, "email": "t@x", "is_admin": True, "is_active": True},
            ),
            mock.patch.object(main.users_store, "accessible_account_ids", return_value=[]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)
        self.client.cookies.set(auth.SESSION_COOKIE, "probe")

    def _get(self, path):
        return self.client.get(
            f"/api/accounts/{self.ACCOUNT}/conversation-media", params={"path": str(path)}
        )

    def test_an_agent_attachment_is_served(self):
        response = self._get(self.catalogue_file)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"catalogue-bytes")

    def test_a_cached_inbound_picture_is_served(self):
        response = self._get(self.cached_file)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"cached-bytes")

    def test_a_file_outside_both_roots_is_not_served(self):
        response = self._get(self.secret)

        self.assertEqual(response.status_code, 404)

    def test_a_traversal_out_of_the_media_root_is_not_served(self):
        response = self._get(Path(self.media_dir.name) / ".." / "etc" / "passwd")

        self.assertEqual(response.status_code, 404)

    def test_a_path_inside_a_root_that_does_not_exist_is_a_404(self):
        response = self._get(Path(self.media_dir.name) / "missing.webp")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
