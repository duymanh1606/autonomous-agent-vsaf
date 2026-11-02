"""A media path or link in a reply's text is never legitimate (TikTok DM has
no inline links; the path is a leak or an invention). The prefix check used to
accept only start-of-string, `[`, `/` or `\\` before `images/` - so a path
after an ordinary space, or under any other directory, slipped through."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.reply_validator import ReplyValidator


class MediaPathReferenceTests(unittest.TestCase):
    def test_paths_and_links_anywhere_in_the_sentence_are_caught(self):
        for text in (
            "images/x.png",
            "Ảnh ở images/x.png nhé",
            "Ảnh ở /images/x.png nhé",
            "Xem [images/x.png]",
            "Ảnh: data/accounts/a/products/sp001/images/x.jpg",
            "file products/sp001/anh1.jpg đây",
            "(video/clip.mp4)",
            "https://cdn.example.com/images/x.png",
        ):
            with self.subTest(text=text):
                self.assertTrue(ReplyValidator._references_media_by_path_or_link(text))

    def test_ordinary_slashes_in_prices_units_and_dates_are_fine(self):
        for text in (
            "Giá 259.000đ/hộp ạ",
            "Dung tích 180ml/lon",
            "Combo 2/3 hộp",
            "Hạn dùng 12/2026",
            "Tỉ lệ 1/2 muỗng",
            "Dạ shop có ạ",
        ):
            with self.subTest(text=text):
                self.assertFalse(ReplyValidator._references_media_by_path_or_link(text))


if __name__ == "__main__":
    unittest.main()
