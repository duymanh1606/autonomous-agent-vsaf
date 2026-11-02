"""Pictures from a DM thread, kept on the server's own disk.

TikTok's own URLs are not a source the dashboard can display from:

* an image the agent has just uploaded is a `blob:` URL, which resolves only
  inside the Chrome tab that created it - nowhere else, ever;
* a DM picture lives on a `-private` CDN host that answers **403** without the
  TikTok session cookies, which the employee's browser does not have;
* even a public sticker URL is signed with `x-expires`, about a month out, so
  a thread that displays today is broken a month from now.

So every picture is downloaded once, through the agent's own Chrome (the only
place that holds the session), and written to
`data/accounts/<id>/media/<hash>.<ext>`. The stored `path` is what the
dashboard serves, with `src` kept beside it as the reference to where it came
from.

Two things keep this bounded. The cache key ignores the query string, so the
same sticker re-signed on every scan maps to one file instead of a new one
each time; a `blob:` URL has no stable part at all - it is a new object URL
per render - so those are keyed on the downloaded bytes instead. And a
total-size budget deletes the least recently used files before a new one is
written.
"""

import hashlib
import logging
from pathlib import Path
from urllib.parse import urlparse


logger = logging.getLogger(__name__)

# What a DM picture may be. Anything else (an SVG, an HTML error page served
# with 200) is not written to disk: the dashboard would embed whatever this
# says it is.
EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


class MediaCache:
    """Downloads a thread's pictures and hands back local paths.

    `fetch` is `ChromeBrowserTool.fetch_media_bytes`, injected rather than
    reached for: this class does the bookkeeping, the browser does the
    transport.
    """

    def __init__(
        self, fetch, root, *, max_file_bytes=10 * 1024 * 1024,
        max_total_bytes=512 * 1024 * 1024,
    ):
        self.fetch = fetch
        self.root = Path(root)
        self.max_file_bytes = max(0, int(max_file_bytes))
        self.max_total_bytes = max(0, int(max_total_bytes))

    async def localize(self, media):
        """`media` with a local `path` added wherever one could be produced.

        Never raises and never drops an entry: an item whose download failed
        comes back exactly as it went in, still carrying its `src`, so the
        dashboard falls back to the CDN URL while it is still valid. Items
        that already have a `path` (an agent attachment from the product
        catalogue) are returned untouched and are never downloaded.
        """
        resolved = []
        for item in media or ():
            if not isinstance(item, dict):
                resolved.append(item)
                continue
            try:
                resolved.append(await self._localize_one(item))
            except Exception:
                logger.exception("[MEDIA] caching one picture failed")
                resolved.append(item)
        return resolved

    async def _localize_one(self, item):
        if item.get("path"):
            return item
        url = str(item.get("src") or "").strip()
        if not url:
            return item
        addressable = not url.startswith("blob:")
        if addressable:
            existing = self._existing(url)
            if existing is not None:
                return {**item, "path": str(existing)}
        data, content_type = await self.fetch(url, max_bytes=self.max_file_bytes)
        if not data:
            return item
        extension = self._extension(content_type, url, data)
        if extension is None:
            logger.info(
                "[MEDIA] not stored content_type=%r bytes=%s (not a picture "
                "by header, URL or content)", content_type, len(data),
            )
            return item
        if self.max_file_bytes and len(data) > self.max_file_bytes:
            return item
        # A `blob:` URL is a fresh object URL on every render - TikTok's own
        # message id for such a bubble changes with it, as the live thread
        # shows (the same just-sent picture read as dom-8ea12b11, dom-2b2a429e,
        # dom-b31e0c67 seconds apart). Keying the file on that URL wrote one
        # more copy of the same picture on every single scan, so a blob is
        # named after its *content* instead: re-reading it costs one in-page
        # fetch and lands on the file already there.
        target = self._target(
            self.content_key(data) if not addressable else self.cache_key(url),
            extension,
        )
        self._make_room(len(data))
        target.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and moved into place, so a crash halfway
        # through never leaves a truncated picture that `_existing` would
        # then hand to the dashboard forever.
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(target)
        logger.info(
            "[MEDIA] stored kind=%s bytes=%s name=%s",
            item.get("kind") or "unknown", len(data), target.name,
        )
        return {**item, "path": str(target)}

    # ------------------------------------------------------------- identity
    @staticmethod
    def cache_key(url):
        """What makes two URLs the same picture.

        The query string is dropped for http(s): TikTok re-signs the same
        image on every render (`x-expires`, `x-signature`), so keeping it
        would download and store one more copy on every single scan. A
        `blob:` URL has no query and identifies one upload in one page
        session, so it is used whole.
        """
        text = str(url or "").strip()
        if text.startswith("blob:"):
            return hashlib.sha256(text.encode("utf-8")).hexdigest()
        parsed = urlparse(text)
        return hashlib.sha256(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def content_key(data):
        """Identity for a picture that has no stable URL: its bytes."""
        return hashlib.sha256(data).hexdigest()

    def _target(self, key, extension):
        return self.root / f"{key[:32]}{extension}"

    def _existing(self, url):
        """A file already downloaded for this picture, whatever its type."""
        stem = self.cache_key(url)[:32]
        for extension in dict.fromkeys(EXTENSIONS.values()):
            candidate = self.root / f"{stem}{extension}"
            if candidate.is_file() and candidate.stat().st_size:
                return candidate
        return None

    @classmethod
    def _extension(cls, content_type, url, data=b""):
        """The file type: what the server said, else the URL, else the bytes.

        All three are needed. TikTok serves `.awebp` stickers with a correct
        `image/webp` content type, so the header is usually enough. But a
        `blob:` URL has no suffix and `fetch()` reports the Blob's own type,
        which is empty whenever the page created it without one - measured
        live on 2026-09-21, where ten pictures were downloaded and then
        dropped as `unsupported_content_type=''` with the bytes already in
        hand. Sniffing is the honest answer there: a picture's first bytes say
        what it is, and refusing to store something we already hold - and
        which the dashboard then cannot show - for want of a label is the
        wrong trade.

        Still a whitelist: anything that matches none of the three is not
        written, so an HTML error page served as 200 never becomes an <img>
        coming from our own origin.
        """
        mapped = EXTENSIONS.get(str(content_type or "").strip().casefold())
        if mapped:
            return mapped
        suffix = Path(urlparse(str(url or "")).path).suffix.casefold()
        aliases = {".awebp": ".webp", ".jpeg": ".jpg", ".heif": ".heic"}
        suffix = aliases.get(suffix, suffix)
        if suffix in set(EXTENSIONS.values()):
            return suffix
        return cls._sniff(data)

    @staticmethod
    def _sniff(data):
        """The file type from the bytes themselves, or `None`."""
        head = bytes(data[:16])
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if head.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if head.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ".webp"
        if head[4:8] == b"ftyp":
            # `ftypqt  ` is QuickTime; every other brand TikTok sends here is
            # an MP4 variant, and both are in EXTENSIONS.
            return ".mov" if head[8:12] == b"qt  " else ".mp4"
        return None

    # --------------------------------------------------------------- budget
    def _make_room(self, incoming_bytes):
        """Delete least recently used files until the new one fits."""
        if not self.max_total_bytes:
            return
        files = []
        for path in self.root.glob("*"):
            if not path.is_file() or path.name.endswith(".part"):
                continue
            stat = path.stat()
            # Nanosecond mtime, so two files written in the same millisecond
            # still have an order to evict in.
            files.append((stat.st_mtime_ns, stat.st_size, path))
        total = sum(size for _, size, _ in files) + int(incoming_bytes)
        if total <= self.max_total_bytes:
            return
        for _, size, path in sorted(files):
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            logger.info("[MEDIA] evicted name=%s bytes=%s", path.name, size)
            if total <= self.max_total_bytes:
                return
