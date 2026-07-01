import base64
import json
import unittest

from app.main import build_result_payload


class ResultPayloadTests(unittest.TestCase):
    def test_result_payload_matches_ai_printer_schema_and_logs_details(self):
        raw_mono = base64.b64encode(bytes([0xAA]) * (48 * 384)).decode("ascii")

        with self.assertLogs("app.main", level="INFO") as logs:
            payload = build_result_payload(raw_mono, 384)

        parsed = json.loads(payload)
        self.assertEqual(
            list(parsed.keys()),
            ["type", "raw_mono", "width", "height"],
        )
        self.assertEqual(parsed["type"], "result")
        self.assertEqual(parsed["raw_mono"], raw_mono)
        self.assertEqual(parsed["width"], 384)
        self.assertEqual(parsed["height"], 384)
        self.assertNotIn("\n", payload)
        self.assertTrue(payload.endswith('","width":384,"height":384}'))

        log_output = "\n".join(logs.output)
        self.assertIn("AI Printer result JSON payload length:", log_output)
        self.assertIn("AI Printer result JSON last 200 chars:", log_output)
        self.assertIn(
            'AI Printer result JSON ends with expected suffix: True',
            log_output,
        )

    def test_result_payload_rejects_wrong_raw_bitmap_size(self):
        raw_mono = base64.b64encode(bytes([0x00]) * 47).decode("ascii")

        with self.assertRaisesRegex(ValueError, "raw_mono decoded size"):
            build_result_payload(raw_mono, 1)
