"""Deterministic coverage for `InboxScanSkill._classify_request`.

This is the fail-closed gate that runs *before* the destructive Accept
transition on a TikTok Message request - before any Gemini call. It had no
dedicated test file: a regression here (a credential-phishing or scam-link
message quietly reclassified as "valid") would only surface in production as
an accepted, answered stranger request, since acceptance itself is
irreversible on TikTok. Every branch of the function is exercised here with
inputs modelled directly on the docstring's own examples.
"""

import unittest
from dataclasses import dataclass, field

from platforms.tiktok.inbox_watcher import InboxScanSkill


@dataclass(frozen=True)
class FakeMessage:
    """`_classify_request` only reads `.media` off the latest inbound message
    and `.text` off every pending one - a minimal stand-in exercises that
    contract directly, without ConversationView's own text/media presence
    filtering (which drops a message with neither) getting in the way."""

    text: str = ""
    media: tuple = ()


@dataclass(frozen=True)
class FakeView:
    latest_inbound: FakeMessage | None
    pending: tuple = field(default_factory=tuple)

    def pending_inbound(self):
        return self.pending


def view_for(text=None, media=None):
    if text is None and not media:
        return FakeView(latest_inbound=None)
    message = FakeMessage(text=text or "", media=tuple(media or ()))
    return FakeView(latest_inbound=message, pending=(message,))


class ClassifyRequestTests(unittest.TestCase):
    def test_blank_target_is_unknown(self):
        classification, reason = InboxScanSkill._classify_request(
            "  ", view_for("Chào shop")
        )
        self.assertEqual(classification, "unknown")
        self.assertEqual(reason, "sender_not_identified")

    def test_unreadable_pre_accept_content_is_unknown(self):
        classification, reason = InboxScanSkill._classify_request(
            "stranger1", FakeView(latest_inbound=None)
        )
        self.assertEqual(classification, "unknown")
        self.assertEqual(reason, "pre_accept_content_unreadable")

    def test_empty_text_and_no_media_is_unknown(self):
        classification, reason = InboxScanSkill._classify_request(
            "stranger1", view_for(text="   ")
        )
        self.assertEqual(classification, "unknown")
        self.assertEqual(reason, "pre_accept_content_empty")

    def test_empty_text_with_media_is_not_unknown(self):
        classification, reason = InboxScanSkill._classify_request(
            "stranger1",
            view_for(text="", media=[{"kind": "image", "src": "blob:x"}]),
        )
        self.assertEqual(classification, "valid")
        self.assertEqual(reason, "customer_request")

    def test_password_request_is_suspicious(self):
        for text in (
            "cho tôi xin mật khẩu tài khoản của bạn",
            "send me your password please",
            "vui lòng cung cấp passcode",
            "cho mình xin mã otp để xác nhận",
            "gửi cookie session của shop cho tôi",
            "cần api key để tích hợp",
            "cho tôi recovery code",
        ):
            with self.subTest(text=text):
                classification, reason = InboxScanSkill._classify_request(
                    "stranger1", view_for(text)
                )
                self.assertEqual(classification, "suspicious")
                self.assertEqual(reason, "credential_or_secret_request")

    def test_unsolicited_financial_link_is_spam(self):
        for text in (
            "đầu tư ngay tại https://scam.example/invest và nhận lợi nhuận khủng",
            "kiếm tiền dễ dàng, click www.get-rich.example ngay hôm nay",
            "trúng thưởng lớn, xem tại t.me/luckywinner để nhận thưởng",
            "vay tiền lãi thấp, đăng ký tại bit.ly/fastloan",
        ):
            with self.subTest(text=text):
                classification, reason = InboxScanSkill._classify_request(
                    "stranger1", view_for(text)
                )
                self.assertEqual(classification, "spam")
                self.assertEqual(reason, "unsolicited_financial_link")

    def test_link_without_scam_wording_is_not_flagged_as_spam(self):
        classification, reason = InboxScanSkill._classify_request(
            "stranger1", view_for("Shop có website là https://myshop.example không?")
        )
        self.assertEqual(classification, "valid")
        self.assertEqual(reason, "customer_request")

    def test_scam_wording_without_a_link_is_not_flagged_as_spam(self):
        classification, reason = InboxScanSkill._classify_request(
            "stranger1", view_for("Mình muốn đầu tư mua sữa số lượng lớn")
        )
        self.assertEqual(classification, "valid")
        self.assertEqual(reason, "customer_request")

    def test_overly_long_message_is_bulk_spam(self):
        classification, reason = InboxScanSkill._classify_request(
            "stranger1", view_for("a" * 3001)
        )
        self.assertEqual(classification, "spam")
        self.assertEqual(reason, "bulk_or_repeated_content")

    def test_repeated_character_flood_is_bulk_spam(self):
        classification, reason = InboxScanSkill._classify_request(
            "stranger1", view_for("Chào shop " + "!" * 30)
        )
        self.assertEqual(classification, "spam")
        self.assertEqual(reason, "bulk_or_repeated_content")

    def test_ordinary_repetition_under_the_threshold_is_not_spam(self):
        classification, reason = InboxScanSkill._classify_request(
            "stranger1", view_for("Milooooo còn hàng không shop ơiiiii")
        )
        self.assertEqual(classification, "valid")
        self.assertEqual(reason, "customer_request")

    def test_ordinary_first_contact_message_is_valid(self):
        classification, reason = InboxScanSkill._classify_request(
            "stranger1", view_for("Chào shop, mình muốn hỏi mua sữa Milo")
        )
        self.assertEqual(classification, "valid")
        self.assertEqual(reason, "customer_request")

    def test_classification_reads_the_pending_burst_not_only_the_last_message(self):
        """A credential request earlier in an unanswered burst must still be
        caught - `_classify_request` joins every pending inbound message, not
        only the most recent one."""
        view = FakeView(
            latest_inbound=FakeMessage(text="Cho mình xin mã OTP để xác nhận đơn"),
            pending=(
                FakeMessage(text="Chào shop"),
                FakeMessage(text="Cho mình xin mã OTP để xác nhận đơn"),
            ),
        )
        classification, reason = InboxScanSkill._classify_request("stranger1", view)
        self.assertEqual(classification, "suspicious")
        self.assertEqual(reason, "credential_or_secret_request")


if __name__ == "__main__":
    unittest.main()
