import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("backup_crypto", ROOT / "app" / "backup_crypto.py")
bc = importlib.util.module_from_spec(spec)

try:
    import cryptography  # noqa: F401

    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False


@unittest.skipUnless(HAVE, "нужен пакет cryptography")
class BackupCrypto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec.loader.exec_module(bc)
        bc.ITERATIONS = 1000  # быстрее в тестах

    def test_roundtrip(self):
        data = b"PGDMP\x00" + bytes(range(256)) * 50
        blob = bc.encrypt_bytes(data, "пароль-123")
        self.assertTrue(blob.startswith(bc.MAGIC))
        self.assertNotIn(b"PGDMP", blob)
        self.assertEqual(bc.decrypt_bytes(blob, "пароль-123"), data)

    def test_wrong_password_and_garbage(self):
        blob = bc.encrypt_bytes(b"secret", "pw")
        with self.assertRaises(ValueError):
            bc.decrypt_bytes(blob, "other")
        with self.assertRaises(ValueError):
            bc.decrypt_bytes(b"not a backup", "pw")

    def test_unique_salt(self):
        self.assertNotEqual(bc.encrypt_bytes(b"x", "pw")[7:23], bc.encrypt_bytes(b"x", "pw")[7:23])


if __name__ == "__main__":
    unittest.main()
