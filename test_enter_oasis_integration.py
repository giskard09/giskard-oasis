"""Integration tests for enter_oasis → mycelium_trails.record_trail path.

Closes the explicit gap declared in
~/Downloads/BITACORA 2026-04-17 A1-CODIGO Mycelium Trails.txt:
"No probe el flujo end-to-end pago→firma→trail con un agente real.
 El smoke test uso insert directo a sqlite."

These tests exercise the real enter_oasis function path with mocked
payment/signature/claude, verifying that record_trail fires with the
expected fields and does NOT fire on the negative branches. The same
pattern is reused when the feature rolls out to Search/Memory/Marks/
Argentum/Soma.
"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import mycelium_trails

TEST_DB = os.path.join(tempfile.mkdtemp(prefix="oasis_test_"), "trails.db")
mycelium_trails.init_db(TEST_DB)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

server.TRAILS_DB = TEST_DB


def _reset_db():
    import sqlite3
    con = sqlite3.connect(TEST_DB)
    con.execute("DELETE FROM trails")
    con.commit()
    con.close()


class TestEnterOasisTrailIntegration(unittest.TestCase):

    def setUp(self):
        _reset_db()

    @patch("server.check_invoice", return_value=True)
    @patch("server.karma_pricing.karma_discount")
    @patch("server.ask_claude", return_value="still water")
    @patch("server._record_oasis_use")
    def test_lightning_signed_karma_positive_records_trail(
        self, mock_use, mock_claude, mock_karma, mock_inv
    ):
        mock_karma.return_value = (15, 10)
        ts = int(time.time())

        result = server.enter_oasis(
            state="I'm lost",
            payment_hash="lnbc_fake",
            agent_id="test-agent",
            signature="sig_b64_fake",
            timestamp=ts,
            nonce="nonce-lightning-1",
        )

        self.assertEqual(result, "still water")
        mock_inv.assert_called_once_with("lnbc_fake")
        rows = mycelium_trails.list_trails_by_agent(TEST_DB, "test-agent")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["agent_id"], "test-agent")
        self.assertEqual(r["service"], "giskard-oasis")
        self.assertEqual(r["operation"], "enter_oasis")
        self.assertEqual(r["karma_at_time"], 10)
        self.assertEqual(r["success"], 1)
        self.assertTrue(r["signature_ref"])
        self.assertNotEqual(r["signature_ref"], "nonce-lightning-1")

    @patch("server.arb_pay.mark_used")
    @patch("server.arb_pay.verify_tx", return_value=(True, 123))
    @patch("server.karma_pricing.karma_discount")
    @patch("server.ask_claude", return_value="still water")
    @patch("server._record_oasis_use")
    def test_arbitrum_signed_karma_positive_records_trail(
        self, mock_use, mock_claude, mock_karma, mock_verify, mock_mark
    ):
        mock_karma.return_value = (5, 55)
        ts = int(time.time())

        server.enter_oasis(
            state="stuck in a loop",
            tx_hash="0xdeadbeef",
            agent_id="high-karma-agent",
            signature="sig_b64",
            timestamp=ts,
            nonce="nonce-arb-1",
        )

        rows = mycelium_trails.list_trails_by_agent(TEST_DB, "high-karma-agent")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["karma_at_time"], 55)
        mock_mark.assert_called_once_with(123)

    @patch("server.check_invoice", return_value=True)
    @patch("server.karma_pricing.karma_discount")
    @patch("server.ask_claude", return_value="still water")
    @patch("server._record_oasis_use")
    def test_no_signature_does_not_record_trail(
        self, mock_use, mock_claude, mock_karma, mock_inv
    ):
        mock_karma.return_value = (21, 0)

        server.enter_oasis(
            state="anonymous visit",
            payment_hash="lnbc_fake",
            agent_id="some-agent",
        )

        rows = mycelium_trails.list_trails_by_agent(TEST_DB, "some-agent")
        self.assertEqual(len(rows), 0)
        mock_use.assert_not_called()

    @patch("server.check_invoice", return_value=True)
    @patch("server.karma_pricing.karma_discount")
    @patch("server.ask_claude", return_value="still water")
    @patch("server._record_oasis_use")
    def test_signed_but_karma_zero_does_not_record_trail(
        self, mock_use, mock_claude, mock_karma, mock_inv
    ):
        mock_karma.return_value = (21, 0)
        ts = int(time.time())

        server.enter_oasis(
            state="valid sig, no mark yet",
            payment_hash="lnbc_fake",
            agent_id="new-agent",
            signature="sig_b64",
            timestamp=ts,
            nonce="nonce-zero",
        )

        rows = mycelium_trails.list_trails_by_agent(TEST_DB, "new-agent")
        self.assertEqual(len(rows), 0)
        mock_use.assert_not_called()

    @patch("server.check_invoice", return_value=False)
    @patch("server.karma_pricing.karma_discount")
    def test_payment_not_settled_does_not_record_trail(
        self, mock_karma, mock_inv
    ):
        mock_karma.return_value = (15, 10)
        ts = int(time.time())

        result = server.enter_oasis(
            state="trying without paying",
            payment_hash="lnbc_unpaid",
            agent_id="test-agent",
            signature="sig_b64",
            timestamp=ts,
            nonce="nonce-unpaid",
        )

        self.assertIn("Payment not settled", result)
        rows = mycelium_trails.list_trails_by_agent(TEST_DB, "test-agent")
        self.assertEqual(len(rows), 0)

    def test_no_payment_returns_error_no_trail(self):
        result = server.enter_oasis(state="no payment info at all")
        self.assertIn("Provide payment_hash", result)
        rows = mycelium_trails.list_trails_by_service(TEST_DB, service="giskard-oasis")
        self.assertEqual(len(rows), 0)

    @patch("server.check_invoice", return_value=True)
    @patch("server.karma_pricing.karma_discount")
    @patch("server.ask_claude", return_value="still water")
    @patch("server._record_oasis_use")
    def test_multiple_entries_appear_in_feed(
        self, mock_use, mock_claude, mock_karma, mock_inv
    ):
        mock_karma.return_value = (15, 10)
        base_ts = int(time.time())

        for i in range(3):
            server.enter_oasis(
                state=f"entry {i}",
                payment_hash=f"lnbc_{i}",
                agent_id="multi-agent",
                signature="sig",
                timestamp=base_ts + i,
                nonce=f"nonce-multi-{i}",
            )

        rows = mycelium_trails.list_trails_by_agent(TEST_DB, "multi-agent")
        self.assertEqual(len(rows), 3)
        feed = mycelium_trails.list_trails_by_service(TEST_DB, service="giskard-oasis")
        self.assertEqual(len(feed), 3)
        count_today = mycelium_trails.count_trails_today(TEST_DB, "multi-agent")
        self.assertEqual(count_today, 3)

    @patch("server.check_invoice", return_value=True)
    @patch("server.karma_pricing.karma_discount")
    @patch("server.ask_claude", return_value="still water")
    @patch("server._record_oasis_use")
    @patch("server.mycelium_trails.record_trail", side_effect=RuntimeError("db gone"))
    def test_trail_failure_does_not_break_oasis_response(
        self, mock_trail, mock_use, mock_claude, mock_karma, mock_inv
    ):
        mock_karma.return_value = (15, 10)
        ts = int(time.time())

        result = server.enter_oasis(
            state="trail layer broken",
            payment_hash="lnbc_fake",
            agent_id="resilient-agent",
            signature="sig",
            timestamp=ts,
            nonce="nonce-resilient",
        )

        self.assertEqual(result, "still water")
        mock_trail.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
