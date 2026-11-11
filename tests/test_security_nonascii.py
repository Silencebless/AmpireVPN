"""Аудит: не-ASCII в секрете URL или подписи давал TypeError (hmac.compare_digest) и 500 вместо 401/404."""
import hashlib
import hmac
import unittest

from app.web.security import safe_eq, verify_cryptobot_signature, verify_signature


class SafeEq(unittest.TestCase):
    def test_equal_and_different(self):
        self.assertTrue(safe_eq("secret-123", "secret-123"))
        self.assertFalse(safe_eq("secret-123", "secret-124"))
        self.assertFalse(safe_eq("", "x"))
        self.assertFalse(safe_eq(None, "x"))
        self.assertFalse(safe_eq("x", None))

    def test_non_ascii_does_not_raise(self):
        with self.assertRaises(TypeError):   # то, что падало раньше
            hmac.compare_digest("пароль", "password")
        for a, b in (("пароль", "password"), ("секрет", "секрет"), ("😀", "x"), ("\udcff", "x")):
            safe_eq(a, b)                    # не бросает
        self.assertFalse(safe_eq("пароль", "password"))
        self.assertTrue(safe_eq("секрет", "секрет"))

    def test_bytes_and_str_mix(self):
        self.assertTrue(safe_eq(b"abc", "abc"))


class Signatures(unittest.TestCase):
    def test_remnawave_signature_with_unicode_garbage_is_rejected_not_crashed(self):
        raw = b'{"event":"user.first_connected"}'
        good = hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature(raw, good, "s3cret"))
        self.assertFalse(verify_signature(raw, "подпись", "s3cret"))
        self.assertFalse(verify_signature(raw, "\udcff\udcfe", "s3cret"))
        self.assertFalse(verify_signature(raw, "", "s3cret"))

    def test_cryptobot_signature_with_unicode_garbage(self):
        raw = b'{"update_type":"invoice_paid"}'
        token = "123:ABC"
        good = hmac.new(hashlib.sha256(token.encode()).digest(), raw, hashlib.sha256).hexdigest()
        self.assertTrue(verify_cryptobot_signature(raw, good, token))
        self.assertFalse(verify_cryptobot_signature(raw, "неверная", token))


if __name__ == "__main__":
    unittest.main()
