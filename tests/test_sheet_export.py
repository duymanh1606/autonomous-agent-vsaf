"""integrations/sheet_export.py must never let a Google Sheets problem reach
the caller - it sits behind the agent's reply pipeline and the dashboard's
order endpoints, both of which must keep working with the webhook unset,
timing out, or erroring."""

import json as _json
import os
import unittest
from decimal import Decimal
from datetime import datetime, timezone
from unittest import mock

import httpx

from integrations import sheet_export


class FakeState:
    def __init__(self, **kwargs):
        self.conversation_id = kwargs.get("conversation_id", "conv-1")
        self.customer_id = kwargs.get("customer_id", "cus-1")
        self.stage = kwargs.get("stage", "interested")
        self.intent = kwargs.get("intent", "hỏi giá")
        self.product_name = kwargs.get("product_name", "Sushi")
        self.size = kwargs.get("size")
        self.quantity = kwargs.get("quantity")
        self.updated_at = kwargs.get("updated_at", datetime.now(timezone.utc))


class _FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = {"ok": True} if body is None else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _RecordingClient:
    """Fake httpx.AsyncClient that records POST payloads."""
    is_closed = False

    def __init__(self, calls: list[dict]):
        self._calls = calls

    async def post(self, url, content, headers=None):
        # Decoded from the bytes actually sent, the way the webhook sees them.
        self._calls.append(_json.loads(content))
        return _FakeResponse(200)

    async def aclose(self):
        self.is_closed = True


class _BoomClient:
    """Fake httpx.AsyncClient that raises on every POST."""
    is_closed = False

    async def post(self, *args, **kwargs):
        raise httpx.ConnectTimeout("boom")

    async def aclose(self):
        self.is_closed = True


class _HttpErrorClient:
    """Fake httpx.AsyncClient whose POST 'succeeds' but the endpoint itself
    rejects it (e.g. a stale/misconfigured Apps Script deployment URL) -
    the failure mode that used to be indistinguishable from success."""
    is_closed = False

    def __init__(self, status_code: int, body=None):
        self._status_code = status_code
        self._body = body

    async def post(self, *args, **kwargs):
        return _FakeResponse(self._status_code, self._body)

    async def aclose(self):
        self.is_closed = True


class SheetExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher_env = mock.patch.dict(os.environ, {
            "GSHEET_WEBHOOK_URL": "https://example.com/exec",
            "GSHEET_WEBHOOK_TOKEN": "secret-token",
        })
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        # Reset the module-level client so each test starts clean.
        sheet_export._client = None
        sheet_export._last_exported.clear()
        # Retries are exercised explicitly below; elsewhere keep them instant.
        patcher_retry = mock.patch.object(sheet_export, "_RETRY_DELAYS_SECONDS", (0, 0))
        patcher_retry.start()
        self.addCleanup(patcher_retry.stop)

    async def test_does_nothing_when_webhook_not_configured(self):
        calls: list[dict] = []
        with mock.patch.dict(os.environ, {"GSHEET_WEBHOOK_URL": ""}), \
                mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_customer_state(FakeState())
        self.assertEqual(calls, [])

    async def test_env_loaded_after_import_is_used(self):
        """The agent imports this module before main.py loads `.env`. Reading
        the webhook at import left every agent's live export off while the
        backfill script (which loads `.env` first) worked."""
        calls: list[dict] = []
        with mock.patch.dict(os.environ, {"GSHEET_WEBHOOK_URL": ""}):
            self.assertFalse(sheet_export.configured())
            # ... then load_config() -> load_env_file() fills it in.
            os.environ["GSHEET_WEBHOOK_URL"] = "https://example.com/late"
            self.assertTrue(sheet_export.configured())
            with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
                await sheet_export.export_customer_state(FakeState())
        self.assertEqual(len(calls), 1)

    async def test_customer_state_posts_expected_payload(self):
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_customer_state(FakeState())

        self.assertEqual(len(calls), 1)
        payload = calls[0]
        self.assertEqual(payload["type"], "customer")
        self.assertEqual(payload["conversation_id"], "conv-1")
        self.assertEqual(payload["stage"], "interested")
        self.assertEqual(payload["token"], "secret-token")
        # Not yet scraped from the customer's TikTok profile - sent as None
        # (blank on the sheet), not omitted or defaulted to a placeholder.
        self.assertIsNone(payload["id_tiktok"])
        self.assertIsNone(payload["customer_name"])
        self.assertIsNone(payload["phone"])
        # The chat address goes under its own name: the Apps Script writes it
        # only into an empty cell, while an order's shipping_address overwrites.
        self.assertIsNone(payload["chat_address"])
        self.assertNotIn("email", payload)
        self.assertNotIn("shipping_address", payload)

    async def test_customer_state_includes_scraped_identity_when_available(self):
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_customer_state(
                FakeState(), id_tiktok="helloworld!!", customer_name="Nguyễn Văn A",
                phone="0912345678",
            )

        payload = calls[0]
        self.assertEqual(payload["id_tiktok"], "helloworld!!")
        self.assertEqual(payload["customer_name"], "Nguyễn Văn A")
        self.assertEqual(payload["phone"], "0912345678")

    async def test_shop_name_is_sent_with_customers_and_orders(self):
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_customer_state(FakeState(), shop="Shop Thời Trang A")
            await sheet_export.export_order({"conversation_id": "conv-1"}, shop="Shop Thời Trang A")
        self.assertEqual([c["shop"] for c in calls], ["Shop Thời Trang A"] * 2)
        # Still matched by the conversation, not by the name.
        self.assertEqual([c["conversation_id"] for c in calls], ["conv-1"] * 2)

    async def test_chat_address_is_sent(self):
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_customer_state(FakeState(), chat_address="5 Hai Bà Trưng, Hà Nội")
        self.assertEqual(calls[0]["chat_address"], "5 Hai Bà Trưng, Hà Nội")

    def test_chat_address_adds_the_city_once(self):
        fmt = sheet_export.format_chat_address
        self.assertEqual(fmt("5 Hai Bà Trưng", "Hà Nội"), "5 Hai Bà Trưng, Hà Nội")
        self.assertEqual(fmt("5 Hai Bà Trưng, Hà Nội", "hà nội"), "5 Hai Bà Trưng, Hà Nội")
        self.assertEqual(fmt("  5   Hai Bà Trưng ", None), "5 Hai Bà Trưng")
        self.assertIsNone(fmt("", "Hà Nội"))

    def test_product_interest_carries_size_and_quantity(self):
        interest = sheet_export.product_interest
        self.assertEqual(interest(FakeState(product_name="Áo Polo", size="L", quantity=2)),
                         "Áo Polo – size L – x2")
        self.assertEqual(interest(FakeState(product_name="Áo Polo", quantity=3)), "Áo Polo – x3")
        self.assertEqual(interest(FakeState(product_name="Áo Polo")), "Áo Polo")
        # A size with no product names nothing.
        self.assertIsNone(interest(FakeState(product_name=None, size="L", quantity=1)))

    async def test_a_new_size_is_resent(self):
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_customer_state(FakeState(size="M"))
            await sheet_export.export_customer_state(FakeState(size="L"))
        self.assertEqual([c["product_name"] for c in calls], ["Sushi – size M", "Sushi – size L"])

    async def test_unchanged_customer_state_is_not_resent(self):
        # Every inbox scan re-persists the on-screen thread's identity and
        # triggers an export; Apps Script meters execution time per day, so
        # a re-send of identical data must be dropped client-side.
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_customer_state(FakeState(), id_tiktok="a")
            await sheet_export.export_customer_state(FakeState(), id_tiktok="a")
            await sheet_export.export_customer_state(FakeState(), id_tiktok="a", phone="0912")
            await sheet_export.export_customer_state(FakeState(stage="ordering"), id_tiktok="a", phone="0912")
        self.assertEqual(
            [(c["id_tiktok"], c["phone"], c["stage"]) for c in calls],
            [("a", None, "interested"), ("a", "0912", "interested"), ("a", "0912", "ordering")],
        )

    async def test_failed_send_is_retried_on_next_call(self):
        # A transient Google outage must not mark the row as "already sent".
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_BoomClient()):
            await sheet_export.export_customer_state(FakeState())
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_customer_state(FakeState())
        self.assertEqual(len(calls), 1)

    async def test_transient_failure_is_retried_within_one_call(self):
        # Google answers a stray 404/HTML page every few calls even when
        # sending one request at a time; the row must still land.
        calls: list[dict] = []

        class _FlakyClient(_RecordingClient):
            attempts = 0

            async def post(self, url, content, headers=None):
                _FlakyClient.attempts += 1
                if _FlakyClient.attempts == 1:
                    return _FakeResponse(404, "<html>")
                if _FlakyClient.attempts == 2:
                    raise httpx.ReadTimeout("slow")
                return await super().post(url, content)

        with mock.patch.object(sheet_export, "_get_client", return_value=_FlakyClient(calls)), \
                mock.patch.object(sheet_export.logger, "warning") as warning:
            await sheet_export.export_customer_state(FakeState())
        self.assertEqual(len(calls), 1)
        warning.assert_not_called()

    async def test_definitive_rejection_is_not_retried(self):
        rejected = _HttpErrorClient(200, {"ok": False, "error": "invalid token"})
        with mock.patch.object(sheet_export, "_get_client", return_value=rejected), \
                mock.patch.object(rejected, "post", wraps=rejected.post) as post:
            await sheet_export.export_customer_state(FakeState())
        self.assertEqual(post.call_count, 1)

    async def test_webhook_calls_in_flight_are_capped(self):
        # Apps Script stalls when more than a few requests hit one deployment
        # at once (8 concurrent no-op calls exceeded 30s in practice).
        import asyncio

        class _SlowClient(_RecordingClient):
            in_flight = 0
            peak = 0

            async def post(self, url, content, headers=None):
                _SlowClient.in_flight += 1
                _SlowClient.peak = max(_SlowClient.peak, _SlowClient.in_flight)
                await asyncio.sleep(0.01)
                _SlowClient.in_flight -= 1
                return await super().post(url, content)

        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_SlowClient(calls)):
            await asyncio.gather(*[
                sheet_export.export_customer_state(FakeState(conversation_id=f"conv-{i}"))
                for i in range(10)
            ])
        self.assertEqual(len(calls), 10)
        self.assertEqual(_SlowClient.peak, sheet_export._MAX_IN_FLIGHT)

    async def test_order_without_conversation_id_is_skipped(self):
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_order({"status": "pending", "items": "Sushi x2"})
        self.assertEqual(calls, [])

    async def test_order_posts_expected_payload(self):
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_order({
                "conversation_id": "conv-1", "status": "packing", "items": "Sushi x2",
                "total_amount": 320000, "updated_at": datetime.now(timezone.utc),
                "customer_name": "Nguyễn Văn A", "id_tiktok": "helloworld!!",
                "shipping_address": "12 Lê Lợi, Q1",
            })

        self.assertEqual(len(calls), 1)
        payload = calls[0]
        self.assertEqual(payload["type"], "order")
        self.assertEqual(payload["shipping_address"], "12 Lê Lợi, Q1")
        self.assertEqual(payload["order_status"], "packing")
        self.assertEqual(payload["order_total"], 320000)
        # customer_name/id_tiktok are looked up by the caller
        # (dashboard_api/main.py's _export_order) and passed through as-is.
        self.assertEqual(payload["customer_name"], "Nguyễn Văn A")
        self.assertEqual(payload["id_tiktok"], "helloworld!!")

    async def test_order_total_from_the_database_is_sent(self):
        """psycopg returns the NUMERIC total as Decimal. JSON could not carry
        it, the TypeError was retried as a network blip, and no order with a
        total ever reached the sheet."""
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_order({
                "conversation_id": "conv-1", "status": "pending", "items": "Milo x2",
                "total_amount": Decimal("30000"), "shipping_address": "12 Lê Lợi",
            })
            await sheet_export.export_order({
                "conversation_id": "conv-2", "status": "pending",
                "total_amount": Decimal("12500.50"),
            })
        self.assertEqual([c["order_total"] for c in calls], [30000, 12500.5])

    async def test_a_payload_json_cannot_carry_is_not_retried(self):
        calls: list[dict] = []
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)), \
                mock.patch.object(sheet_export.logger, "exception") as logged:
            await sheet_export.export_customer_state(
                FakeState(), customer_name=object(),  # not JSON
            )
        self.assertEqual(calls, [])
        logged.assert_called_once()

    async def test_network_error_is_swallowed_not_raised(self):
        with mock.patch.object(sheet_export, "_get_client", return_value=_BoomClient()):
            # Must not raise - a Sheets outage cannot break the reply
            # pipeline or an operator's order save.
            await sheet_export.export_customer_state(FakeState())

    async def test_http_error_response_is_logged_not_silently_treated_as_success(self):
        # httpx does not raise for a non-2xx response by itself. A stale or
        # misconfigured webhook URL (e.g. the Apps Script deployment was
        # replaced or access revoked) then "succeeds" from _post's point of
        # view forever, with nothing ever telling an operator their orders
        # stopped reaching the sheet - this is the failure mode that came up
        # for real (webhook returning HTTP 401).
        with mock.patch.object(sheet_export, "_get_client", return_value=_HttpErrorClient(401)), \
                self.assertLogs(sheet_export.logger, level="WARNING") as logs:
            await sheet_export.export_order({
                "conversation_id": "conv-1", "status": "pending", "items": "Sushi x1",
            })
        self.assertTrue(any("401" in message for message in logs.output))

    async def test_rejection_in_body_is_logged_and_not_cached_as_sent(self):
        # Apps Script answers HTTP 200 even when doPost refused the payload
        # (wrong token, sheet missing) - the only signal is {"ok": false}.
        calls: list[dict] = []
        rejected = _HttpErrorClient(200, {"ok": False, "error": "invalid token"})
        with mock.patch.object(sheet_export, "_get_client", return_value=rejected), \
                self.assertLogs(sheet_export.logger, level="WARNING") as logs:
            await sheet_export.export_customer_state(FakeState())
        self.assertTrue(any("invalid token" in message for message in logs.output))
        with mock.patch.object(sheet_export, "_get_client", return_value=_RecordingClient(calls)):
            await sheet_export.export_customer_state(FakeState())
        self.assertEqual(len(calls), 1)

    async def test_html_error_page_is_logged(self):
        # Google serves its own HTML page with HTTP 200 when the deployment
        # has no doPost ("Script function not found") or requires sign-in.
        with mock.patch.object(sheet_export, "_get_client", return_value=_HttpErrorClient(200, "<html>")), \
                self.assertLogs(sheet_export.logger, level="WARNING") as logs:
            await sheet_export.export_order({
                "conversation_id": "conv-1", "status": "pending", "items": "Sushi x1",
            })
        self.assertTrue(any("non-JSON" in message for message in logs.output))

    async def test_http_success_response_logs_nothing(self):
        with mock.patch.object(sheet_export, "_get_client", return_value=_HttpErrorClient(200)), \
                mock.patch.object(sheet_export.logger, "warning") as warning:
            await sheet_export.export_order({
                "conversation_id": "conv-1", "status": "pending", "items": "Sushi x1",
            })
        warning.assert_not_called()

    async def test_close_client_gracefully(self):
        client = _RecordingClient([])
        sheet_export._client = client
        await sheet_export.close_client()
        self.assertTrue(client.is_closed)
        self.assertIsNone(sheet_export._client)

    async def test_get_client_reuses_existing(self):
        client = sheet_export._get_client()
        self.assertIsInstance(client, httpx.AsyncClient)
        same = sheet_export._get_client()
        self.assertIs(client, same)
        await client.aclose()

    async def test_get_client_recreates_after_close(self):
        client = sheet_export._get_client()
        await client.aclose()
        new_client = sheet_export._get_client()
        self.assertIsNot(client, new_client)
        await new_client.aclose()


if __name__ == "__main__":
    unittest.main()
