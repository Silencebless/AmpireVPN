import hashlib
import hmac
import json
import unittest

from app.web.security import ip_allowed, verify_cryptobot_signature, verify_signature


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Signature(unittest.TestCase):
    secret = "abc123SECRET"
    payload = {"scope": "user", "event": "user.first_connected", "timestamp": "2026-11-03T10:00:00.000Z",
               "data": {"id": 7, "telegramId": 123, "username": "ampire_123", "note": "привет"}}

    def test_compact_json_like_js_stringify(self):
        compact = json.dumps(self.payload, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertTrue(verify_signature(compact, sign(compact, self.secret), self.secret))

    def test_pretty_body_still_verified_via_recompact(self):
        compact = json.dumps(self.payload, separators=(",", ":"), ensure_ascii=False).encode()
        pretty = json.dumps(self.payload, indent=2, ensure_ascii=False).encode()
        self.assertTrue(verify_signature(pretty, sign(compact, self.secret), self.secret))

    def test_rejects_wrong_secret_tampering_and_empty(self):
        raw = json.dumps(self.payload, separators=(",", ":")).encode()
        good = sign(raw, self.secret)
        self.assertFalse(verify_signature(raw, good, "other"))
        self.assertFalse(verify_signature(raw + b" ", good, self.secret))
        self.assertFalse(verify_signature(raw, "", self.secret))
        self.assertFalse(verify_signature(raw, good, ""))
        self.assertFalse(verify_signature(b"not json", good, self.secret))


class CryptoBotSignature(unittest.TestCase):
    token = "123456789:AAzQcZWQqQAbsfgPnOLr4FHC8Doa4L7KryC"

    def _sig(self, raw: bytes) -> str:
        key = hashlib.sha256(self.token.encode()).digest()
        return hmac.new(key, raw, hashlib.sha256).hexdigest()

    def test_valid_and_invalid(self):
        raw = b'{"update_id":1,"update_type":"invoice_paid","payload":{"invoice_id":42,"status":"paid"}}'
        self.assertTrue(verify_cryptobot_signature(raw, self._sig(raw), self.token))
        self.assertTrue(verify_cryptobot_signature(raw, self._sig(raw).upper(), self.token))
        self.assertFalse(verify_cryptobot_signature(raw + b" ", self._sig(raw), self.token))
        self.assertFalse(verify_cryptobot_signature(raw, self._sig(raw), "other:token"))
        self.assertFalse(verify_cryptobot_signature(raw, "", self.token))
        self.assertFalse(verify_cryptobot_signature(b"", "abc", self.token))


class YooKassaIps(unittest.TestCase):
    def test_allowed(self):
        for ip in ("185.71.76.10", "185.71.77.31", "77.75.153.5", "77.75.156.11", "77.75.156.35", "77.75.154.200", "2a02:5180::1"):
            self.assertTrue(ip_allowed(ip), ip)

    def test_denied(self):
        for ip in ("8.8.8.8", "185.71.78.1", "77.75.156.12", "127.0.0.1", "", "garbage", "2001:db8::1"):
            self.assertFalse(ip_allowed(ip), ip)


if __name__ == "__main__":
    unittest.main()
