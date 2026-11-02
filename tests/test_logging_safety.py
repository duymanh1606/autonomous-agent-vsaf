import unittest

from core.logging_safety import content_fingerprint, redact_pii


class LoggingSafetyTests(unittest.TestCase):
    def test_phone_email_and_signed_query_are_redacted(self):
        source = (
            "Gọi 0901 234 567 hoặc a.customer@example.com "
            "https://cdn.test/image?token=secret&size=large"
        )
        redacted = redact_pii(source)
        self.assertNotIn("0901", redacted)
        self.assertNotIn("example.com", redacted)
        self.assertNotIn("secret", redacted)

    def test_content_fingerprint_is_stable_and_not_plaintext(self):
        self.assertEqual(content_fingerprint("xin chào"), content_fingerprint("xin chào"))
        self.assertNotIn("xin", content_fingerprint("xin chào"))


if __name__ == "__main__":
    unittest.main()
