"""Vietnamese translations under a Myanmar shop's messages in the dashboard.

Through the real HTTP route and a real cache table; only Gemini is faked.
What matters: nothing is translated for a Vietnamese shop, only the shop's
own script is sent to the model, a text is paid for once per workspace, and
a failure leaves the page usable instead of breaking it.
"""

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api import main
from dashboard_api.routes import translations as translation_routes
from dashboard_api.translations import TranslationStore
from tests.support import requires_postgres
from tests.test_dashboard_routes_split import DashboardRouteFixture


BURMESE_GREETING = "မင်္ဂလာပါ ဈေးဘယ်လောက်လဲ"
BURMESE_REPLY = "ဈေးနှုန်း ၂၅၀၀၀ ကျပ် ဖြစ်ပါတယ်"


def _message(message_id, direction, text):
    return {
        "message_id": message_id, "direction": direction, "text": text,
        "created_at": "2026-10-15T10:00:00+00:00", "source": "dom", "media": [],
    }


@requires_postgres
class ConversationTranslationTests(DashboardRouteFixture):
    def setUp(self):
        super().setUp()
        self.store = TranslationStore(self.schema.dsn)
        self.addCleanup(self.store.close)
        self.messages = [
            _message("m1", "INBOUND", BURMESE_GREETING),
            _message("m2", "OUTBOUND", BURMESE_REPLY),
            _message("m3", "OUTBOUND", "Dạ shop gửi ảnh ạ"),
            _message("m4", "INBOUND", "👍"),
            _message("m5", "INBOUND", BURMESE_GREETING),
        ]
        reader = mock.Mock(
            customer_identity=lambda _cid: {},
            conversation=lambda _cid, **_kw: {"messages": self.messages},
        )
        self.gemini = mock.AsyncMock(side_effect=self._fake_translate)
        for patcher in (
            mock.patch.object(main, "translation_store", self.store),
            mock.patch.object(main, "_reader", return_value=reader),
            mock.patch.object(translation_routes, "translate_texts", self.gemini),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.url = f"/api/accounts/{self.account_id}/conversations/conv-1/translations"

    @staticmethod
    async def _fake_translate(texts, **_kwargs):
        return {index: f"VI({text})" for index, text in enumerate(texts)}

    def set_language(self, code, api_key="gemini-key"):
        response = self.client.put(
            f"/api/accounts/{self.account_id}/reply-language", json={"language": code},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.registry.get(self.account_id).api_key = api_key

    def translate(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_vietnamese_shop_has_nothing_to_translate(self):
        self.set_language("vi")
        self.assertEqual(self.translate()["enabled"], False)
        self.gemini.assert_not_called()

    def test_only_burmese_text_is_translated_and_each_text_once(self):
        self.set_language("my")
        body = self.translate()
        self.assertTrue(body["enabled"])
        self.assertIsNone(body["error"])
        self.assertEqual(body["remaining"], 0)
        # Newest first, the repeated greeting sent once.
        self.assertEqual(self.gemini.await_args.args[0], [BURMESE_GREETING, BURMESE_REPLY])
        self.assertEqual(
            {(item["message_id"], item["text"]) for item in body["translations"]},
            {
                ("m1", f"VI({BURMESE_GREETING})"),
                ("m2", f"VI({BURMESE_REPLY})"),
                ("m5", f"VI({BURMESE_GREETING})"),
            },
        )

    def test_a_second_request_is_served_from_the_cache(self):
        self.set_language("my")
        first = self.translate()
        second = self.translate()
        self.assertEqual(self.gemini.await_count, 1)
        self.assertEqual(first["translations"], second["translations"])

    def test_without_a_workspace_key_nothing_is_called(self):
        self.set_language("my", api_key="")
        body = self.translate()
        self.gemini.assert_not_called()
        self.assertIn("API key", body["error"])
        self.assertEqual(body["translations"], [])

    def test_a_gemini_failure_is_reported_not_raised(self):
        self.set_language("my")
        self.gemini.side_effect = RuntimeError("Gemini request failed")
        body = self.translate()
        self.assertTrue(body["error"])
        self.assertEqual(body["remaining"], 2)

    def test_an_unanswered_batch_stops_the_page_from_polling(self):
        self.set_language("my")
        self.gemini.side_effect = None
        self.gemini.return_value = {}
        body = self.translate()
        self.assertTrue(body["error"])

    def test_the_cache_is_per_workspace(self):
        self.store.put_many("other_shop", {"h": "của shop khác"})
        self.assertEqual(self.store.get_many(self.account_id, ["h"]), {})
        self.assertEqual(self.store.get_many("other_shop", ["h"]), {"h": "của shop khác"})

    def test_a_stranger_cannot_translate_this_workspace(self):
        self.set_language("my")
        response = self._client(self.stranger).post(self.url)
        self.assertIn(response.status_code, (403, 404))
        self.gemini.assert_not_called()


class TranslateTextsTests(unittest.IsolatedAsyncioTestCase):
    async def test_batches_map_back_to_their_own_texts(self):
        from dashboard_api import translations

        calls = []

        class FakeProvider:
            def __init__(self, **_kwargs):
                pass

            async def generate_structured(self, prompt, model):
                calls.append(prompt)
                size = 2 if len(calls) == 1 else 1
                # Out of order, one id out of range, one empty: only the
                # real ones come back, each to its own text.
                items = [{"id": i, "vi": f"b{len(calls)}-{i}"} for i in reversed(range(size))]
                items += [{"id": 7, "vi": "lạc"}, {"id": 0, "vi": "  "}][: 1 if size == 2 else 0]
                return model.model_validate({"items": items})

            async def close(self):
                pass

        with mock.patch.object(translations, "GeminiFlashLiteProvider", FakeProvider):
            result = await translations.translate_texts(
                ["a", "b", "c"], source_language="my", api_key="k",
                model="m", account_id="acc", batch_size=2,
            )
        self.assertEqual(result, {0: "b1-0", 1: "b1-1", 2: "b2-0"})
        self.assertEqual(len(calls), 2)
        self.assertIn("Burmese", calls[0])
