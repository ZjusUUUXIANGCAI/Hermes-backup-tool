import os
import struct
import tempfile
import unittest
from pathlib import Path

from _helper import hb


def roundtrip(mod, ver, password="pw-123456"):
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "data.bin"
    src.write_bytes(os.urandom(300 * 1024))
    enc = tmp / "x.hbz"
    dec = tmp / "back.bin"
    if ver == 2:
        mod.encrypt_file_v2(src, enc, password)
        mod.decrypt_file_v2(enc, dec, password)
    else:
        mod.encrypt_file_v1(src, enc, password)
        mod.decrypt_file_v1(enc, dec, password)
    return src, enc, dec, tmp


class CryptoV1Test(unittest.TestCase):
    def test_roundtrip(self):
        src, enc, dec, tmp = roundtrip(hb, 1)
        self.assertEqual(src.read_bytes(), dec.read_bytes())
        self.assertEqual(hb.encrypted_version(enc), 1)

    def test_wrong_password_rejected(self):
        src, enc, dec, tmp = roundtrip(hb, 1)
        with self.assertRaises(SystemExit):
            hb.decrypt_file_v1(enc, tmp / "bad.bin", "wrong-password")

    def test_tampered_tag_rejected(self):
        src, enc, dec, tmp = roundtrip(hb, 1)
        raw = bytearray(enc.read_bytes())
        raw[-1] ^= 0xFF
        enc.write_bytes(raw)
        with self.assertRaises(SystemExit):
            hb.decrypt_file_v1(enc, tmp / "bad.bin", "pw-123456")


@unittest.skipUnless(hb.HAVE_AES, "cryptography not installed")
class CryptoV2Test(unittest.TestCase):
    def test_roundtrip(self):
        src, enc, dec, tmp = roundtrip(hb, 2)
        self.assertEqual(src.read_bytes(), dec.read_bytes())
        self.assertEqual(hb.encrypted_version(enc), 2)

    def test_wrong_password_rejected(self):
        src, enc, dec, tmp = roundtrip(hb, 2)
        with self.assertRaises(SystemExit):
            hb.decrypt_file_v2(enc, tmp / "bad.bin", "wrong-password")

    def test_tampered_ciphertext_rejected(self):
        src, enc, dec, tmp = roundtrip(hb, 2)
        raw = bytearray(enc.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        enc.write_bytes(raw)
        with self.assertRaises(SystemExit):
            hb.decrypt_file_v2(enc, tmp / "bad.bin", "pw-123456")

    def test_oversized_header_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        bad = tmp / "huge.hbz"
        bad.write_bytes(hb.MAGIC2 + struct.pack(">I", 10_000_000) + b"{}" + b"0" * 32)
        with self.assertRaises(SystemExit):
            hb.decrypt_file_v2(bad, tmp / "out.bin", "pw")


class KdfBoundsTest(unittest.TestCase):
    def test_iteration_bounds(self):
        with self.assertRaises(SystemExit):
            hb.derive_keys("pw", b"0" * 16, 1000)
        with self.assertRaises(SystemExit):
            hb.derive_keys("pw", b"0" * 16, 10_000_000)

    def test_password_length_bound(self):
        with self.assertRaises(SystemExit):
            hb.derive_keys("x" * (hb.MAX_PASSWORD + 1), b"0" * 16, hb.DEFAULT_ITERS)


if __name__ == "__main__":
    unittest.main()
