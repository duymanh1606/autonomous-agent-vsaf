"""An operator can attach a picture or a video to a chat message.

Reported by testing: the composer's paperclip did nothing - it was a button
with no handler, and the send API took text only. A file is now uploaded to
the workspace's own uploads directory and sent by the id that returns. The
agent may stage files from exactly two places, the product catalogue and
that directory, so these tests also pin down what an id can and cannot
name.
"""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException, UploadFile

import dashboard_api.main as main
import storage.paths as paths
from dashboard_api.routes import chat_media
from models.tasks import Task
from storage.repositories import TaskRepository
from tests.support import make_database
from tests.test_dashboard_conversation_control_api import register_conversation


# The first bytes a real file of each type starts with (the agent checks them).
_HEADERS = {
    ".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff\xe0",
    ".mp4": b"\x00\x00\x00\x18ftypmp42",
}


def _upload(name: str, size: int, header: bytes | None = None) -> UploadFile:
    if header is None:
        header = _HEADERS.get(Path(name).suffix.casefold(), b"")
    body = (header + b"x" * size)[:size] if size else b""
    return UploadFile(filename=name, file=io.BytesIO(body))


class ChatAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (
            # Measuring a video needs ffprobe; its own tests are below.
            mock.patch.object(chat_media, "_video_problem", return_value=None),
            mock.patch.object(paths, "ACCOUNTS_ROOT", Path(tmp.name)),
            mock.patch.object(main, "_account_or_404", lambda account_id: SimpleNamespace(id=account_id)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.uploads = Path(tmp.name) / "shop-1" / "uploads"

    async def test_a_picture_is_stored_in_the_workspace_uploads_under_a_random_name(self):
        result = await chat_media.upload_chat_attachment("shop-1", _upload("../../ảnh mẫu.JPG", 100))
        self.assertEqual(result["kind"], "image")
        self.assertRegex(result["id"], r"^[0-9a-f]{32}\.jpg$")
        self.assertTrue((self.uploads / result["id"]).is_file())

    async def test_other_types_and_oversized_files_are_refused(self):
        for name, size, status in (
            ("script.sh", 10, 400),
            ("tai-lieu.pdf", 10, 400),
            ("to.png", chat_media.MAX_IMAGE_BYTES + 1, 413),
            ("dai.mp4", chat_media.MAX_VIDEO_BYTES + 1, 413),
            ("rong.png", 0, 400),
            ("cu.m4v", 10, 400),                 # not in the agent's MIME list
        ):
            with self.subTest(name=name):
                with self.assertRaises(HTTPException) as caught:
                    await chat_media.upload_chat_attachment("shop-1", _upload(name, size))
                self.assertEqual(caught.exception.status_code, status)

    async def test_a_file_whose_content_is_not_its_name_is_refused(self):
        """An iPhone HEIC renamed to .jpg: the agent would refuse it at send
        time, so it is refused now, with a reason."""
        with self.assertRaises(HTTPException) as caught:
            await chat_media.upload_chat_attachment("shop-1", _upload("anh.jpg", 50, b"\x00\x00\x00\x18ftypheic"))
        self.assertIn("không đúng định dạng", caught.exception.detail)
        self.assertFalse(self.uploads.exists() and any(self.uploads.iterdir()))

    async def test_a_video_the_agent_would_refuse_is_refused_at_upload(self):
        chat_media._video_problem.return_value = "Video dài 95 giây - TikTok chỉ cho gửi video tối đa 60 giây"
        with self.assertRaises(HTTPException) as caught:
            await chat_media.upload_chat_attachment("shop-1", _upload("dai.mp4", 50))
        self.assertIn("95 giây", caught.exception.detail)
        self.assertEqual(list(self.uploads.iterdir()), [])  # nothing left behind

    async def test_an_id_names_only_a_file_this_route_wrote(self):
        image = await chat_media.upload_chat_attachment("shop-1", _upload("a.png", 10))
        video = await chat_media.upload_chat_attachment("shop-1", _upload("b.mp4", 10))
        resolved = chat_media.resolve_attachments("shop-1", [image["id"], video["id"]])
        self.assertEqual([item["kind"] for item in resolved], ["image", "video"])
        self.assertEqual(Path(resolved[0]["path"]).parent, self.uploads.resolve())

        second_image = await chat_media.upload_chat_attachment("shop-1", _upload("c.png", 10))
        for ids in (
            ["../../products/sp1/images/a.png"],
            ["/etc/passwd"],
            ["0" * 32 + ".png"],                   # well-formed, not on disk
            [image["id"], second_image["id"]],     # two pictures
            [image["id"], video["id"], second_image["id"]],
        ):
            with self.subTest(ids=ids):
                with self.assertRaises(HTTPException):
                    chat_media.resolve_attachments("shop-1", ids)
        # Another workspace cannot send this workspace's file.
        with self.assertRaises(HTTPException):
            chat_media.resolve_attachments("shop-2", [image["id"]])

    async def test_the_send_passes_the_files_to_the_agent(self):
        image = await chat_media.upload_chat_attachment("shop-1", _upload("a.png", 10))
        sent = {}

        async def control(account, method, path, payload=None):
            sent.update(payload or {})
            return {"task_id": payload["task_id"]}

        reader = mock.Mock(conversation=lambda cid: {
            "control": {"mode": "HUMAN"}, "profile": {"username": "khach"},
        })
        writer = mock.Mock(renew_human=mock.AsyncMock(return_value={"mode": "HUMAN"}))
        with mock.patch.object(main, "_control", control), \
                mock.patch.object(main, "_reader", return_value=reader), \
                mock.patch.object(main, "_control_writer", return_value=writer), \
                mock.patch.object(main.hub, "emit"):
            await main.send_conversation_message(
                "shop-1", "conv-1", text="", client_message_id="m1", attachments=[image["id"]],
            )
            with self.assertRaises(HTTPException) as caught:
                await main.send_conversation_message(
                    "shop-1", "conv-1", text="  ", client_message_id="m2", attachments=[],
                )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(sent["payload"]["attachments"][0]["kind"], "image")
        self.assertTrue(sent["payload"]["attachments"][0]["path"].endswith(image["id"]))

    async def _send(self, renewed_mode, takeover_mode=None, text="Cảm ơn bạn đã liên hệ ạ!"):
        queued, took_over = [], []

        async def control(account, method, path, payload=None):
            queued.append(payload)
            return {}

        async def takeover(account_id, conversation_id, operator_id="local_operator"):
            took_over.append(conversation_id)
            return {"mode": takeover_mode}

        writer = mock.Mock(renew_human=mock.AsyncMock(return_value={"mode": renewed_mode}))
        reader = mock.Mock(conversation=lambda cid: {"control": {"mode": "HUMAN"}, "profile": {}})
        with mock.patch.object(main, "_control", control), \
                mock.patch.object(main, "_reader", return_value=reader), \
                mock.patch.object(main, "_control_writer", return_value=writer), \
                mock.patch.object(main, "takeover_conversation", takeover), \
                mock.patch.object(main.hub, "emit"):
            try:
                await main.send_conversation_message(
                    "shop-1", "conv-1", text=text, client_message_id="m3", attachments=[],
                )
                error = None
            except HTTPException as caught:
                error = caught
        return queued, took_over, error

    async def test_a_lapsed_takeover_is_taken_over_again_and_the_message_sent(self):
        """A person sending by hand is taking the conversation over: a
        takeover that lapsed (or a conversation the agent handed over) no
        longer refuses their message."""
        for mode in ("AUTO", "WAITING_HUMAN"):
            with self.subTest(mode=mode):
                queued, took_over, error = await self._send(mode, takeover_mode="HUMAN")
                self.assertIsNone(error)
                self.assertEqual(took_over, ["conv-1"])
                self.assertEqual(queued[0]["payload"]["origin"], "human")

    async def test_a_live_takeover_is_not_taken_over_again(self):
        queued, took_over, error = await self._send("HUMAN")
        self.assertIsNone(error)
        self.assertEqual(took_over, [])
        self.assertEqual(len(queued), 1)

    async def test_when_the_takeover_cannot_be_made_nothing_is_queued(self):
        queued, _, error = await self._send("AUTO", takeover_mode="AUTO")
        self.assertEqual(error.status_code, 409)
        self.assertEqual(queued, [])

    async def test_a_message_the_send_skill_would_refuse_is_refused_here_with_a_reason(self):
        queued, _, error = await self._send("HUMAN", text="x" * 1001)
        self.assertEqual(error.status_code, 400)
        self.assertIn("1000", error.detail)
        self.assertEqual(queued, [])


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_sent_file_is_kept_in_the_conversation_history(self):
        database = make_database(self)
        repository = TaskRepository(database)
        register_conversation(database, "conv-1")
        task = Task(
            id="human-conv-1-m1", account_id=database.account_id,
            capability="send_message", platform="tiktok_web",
            payload={
                "conversation_id": "conv-1", "recipient": "khach", "response": "",
                "attachments": [{"kind": "image", "path": "/data/uploads/a.png", "name": "a.png"}],
                "confirm_send": True, "origin": "human", "client_message_id": "m1",
            },
        )
        await repository.submit(task)
        await repository.complete(task.id, True, None, {"status": "sent"}, task=task)

        row = database.conn.execute(
            "SELECT media_json FROM conversation_messages WHERE account_id = %s AND message_id = 'm1'",
            (database.account_id,),
        ).fetchone()
        self.assertIn("/data/uploads/a.png", row["media_json"])


class VideoCheckTests(unittest.TestCase):
    def test_no_ffprobe_is_said_plainly(self):
        with mock.patch.object(chat_media.shutil, "which", return_value=None):
            self.assertIn("chưa cài ffmpeg", chat_media._video_problem(Path("/x.mp4")))

    def test_a_video_over_the_limit_is_named(self):
        from tools.browser import ChromeBrowserTool

        with mock.patch.object(chat_media.shutil, "which", return_value="/usr/bin/ffprobe"), \
                mock.patch.object(ChromeBrowserTool, "_probe_video_duration", return_value=95.4):
            self.assertIn("95 giây", chat_media._video_problem(Path("/x.mp4")))
        with mock.patch.object(chat_media.shutil, "which", return_value="/usr/bin/ffprobe"), \
                mock.patch.object(ChromeBrowserTool, "_probe_video_duration", return_value=12.0):
            self.assertIsNone(chat_media._video_problem(Path("/x.mp4")))


if __name__ == "__main__":
    unittest.main()
