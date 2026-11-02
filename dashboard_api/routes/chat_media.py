"""Files an operator attaches to a message in the dashboard chat.

The composer's paperclip was a button with nothing behind it (reported by
testing). A file is uploaded here first, into the workspace's own uploads
directory, and the send that follows names it by the id returned. The agent
may stage files only from its product catalogue and this one directory
(tools/browser.py, `allowed_media_roots`), so nothing else on the machine can
ever be sent to a customer - and an id is checked to be a file this route
wrote, never a path a request supplies.
"""

import re
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile

from storage.paths import get_account_upload_dir


router = APIRouter()

# What the agent's browser will actually stage (tools/browser.py,
# attach_message_media), checked here too so a file it would refuse is
# refused at upload, with a reason, instead of turning into "Gửi thất bại".
# .m4v is left out: its MIME type is not in messaging.media.allowed_mime_types.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 25 * 1024 * 1024
MAX_VIDEO_SECONDS = 60


def _content_matches(suffix: str, header: bytes) -> bool:
    """The file really is what its name says (the agent checks the same)."""
    return {
        ".jpg": header.startswith(b"\xff\xd8\xff"),
        ".jpeg": header.startswith(b"\xff\xd8\xff"),
        ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        ".gif": header.startswith((b"GIF87a", b"GIF89a")),
        ".webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
        ".mp4": header[4:8] == b"ftyp",
        ".mov": header[4:8] == b"ftyp",
        ".webm": header.startswith(b"\x1aE\xdf\xa3"),
    }.get(suffix, False)


# One picture and one video per message, as ReplyMessageSkill allows.
MAX_ATTACHMENTS = 2

_ID = re.compile(r"^[0-9a-f]{32}\.(?:jpg|jpeg|png|webp|gif|mp4|mov|webm)$")


def _main():
    # Late, for the same reason as routes/orders.py.
    from dashboard_api import main

    return main


def kind_of(name: str) -> str | None:
    suffix = Path(name).suffix.casefold()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return None


@router.post("/api/accounts/{account_id}/chat-attachments")
async def upload_chat_attachment(account_id: str, file: UploadFile = File(...)):
    account = _main()._account_or_404(account_id)
    kind = kind_of(file.filename or "")
    if kind is None:
        raise HTTPException(
            status_code=400,
            detail="Chỉ gửi được ảnh (jpg, png, webp, gif) hoặc video (mp4, mov, webm)",
        )
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_VIDEO_BYTES
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"{'Ảnh' if kind == 'image' else 'Video'} tối đa {limit // (1024 * 1024)} MB",
        )
    if not content:
        raise HTTPException(status_code=400, detail="Tệp rỗng")
    suffix = Path(file.filename or "").suffix.casefold()
    if not _content_matches(suffix, content[:16]):
        raise HTTPException(
            status_code=400,
            detail="Nội dung tệp không đúng định dạng theo tên (ví dụ ảnh HEIC đổi đuôi .jpg) - hãy xuất lại ảnh/video",
        )
    # A random name: the uploaded filename is never used as a path.
    attachment_id = f"{uuid4().hex}{suffix}"
    directory = get_account_upload_dir(account.id)
    path = directory / attachment_id
    path.write_bytes(content)
    if kind == "video":
        error = _video_problem(path)
        if error:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=error)
    return {
        "id": attachment_id, "kind": kind,
        "name": file.filename or attachment_id, "size": len(content),
    }


def _video_problem(path: Path) -> str | None:
    """Why the agent would refuse this video, or None.

    The agent measures a video with ffprobe before staging it and refuses
    one it cannot measure - so on a server without ffmpeg installed every
    video fails. Said here, plainly, instead.
    """
    if shutil.which("ffprobe") is None:
        return "Máy chủ chưa cài ffmpeg nên chưa gửi được video - quản trị viên chạy: sudo apt install -y ffmpeg"
    # The agent's own measurement, so the two cannot disagree.
    from tools.browser import ChromeBrowserTool

    duration = ChromeBrowserTool._probe_video_duration(path)
    if duration is None:
        return "Không đọc được video này - hãy xuất lại video (mp4)"
    if duration > MAX_VIDEO_SECONDS:
        return f"Video dài {int(duration)} giây - TikTok chỉ cho gửi video tối đa {MAX_VIDEO_SECONDS} giây"
    return None


def resolve_attachments(account_id: str, ids: list) -> list[dict]:
    """Turn the ids a send names into the agent's attachment payload.

    Each must be an id this route produced and still on disk in this
    workspace's uploads directory; at most one picture and one video.
    """
    if not isinstance(ids, list) or len(ids) > MAX_ATTACHMENTS:
        raise HTTPException(status_code=400, detail="Tối đa một ảnh và một video mỗi tin")
    directory = get_account_upload_dir(account_id, create=False)
    attachments, kinds = [], set()
    for attachment_id in ids:
        attachment_id = str(attachment_id or "")
        path = directory / attachment_id
        if not _ID.fullmatch(attachment_id) or not path.is_file():
            raise HTTPException(status_code=400, detail="Tệp đính kèm không còn, hãy chọn lại")
        kind = kind_of(attachment_id)
        if kind in kinds:
            raise HTTPException(status_code=400, detail="Tối đa một ảnh và một video mỗi tin")
        kinds.add(kind)
        attachments.append({"kind": kind, "path": str(path.resolve()), "name": attachment_id})
    return attachments
