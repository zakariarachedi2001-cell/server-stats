"""Run with:  python -m unittest discover -s tests -v   (needs requirements.txt installed)"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_PATH", os.path.join(os.environ.get("TMPDIR", "/tmp"), "test-visitors.db"))

import numpy as np  # noqa: E402
import server  # noqa: E402


class AddressParsing(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(server.split_host_port("play.example.net"), ("play.example.net", None))
        self.assertEqual(server.split_host_port("play.example.net:25565"), ("play.example.net", "25565"))
        self.assertEqual(server.split_host_port("[::1]:25565"), ("::1", "25565"))

    def test_invalid(self):
        for bad in ("host:0", "host:70000", "host:", ":25565", "a:b:c", "[::1"):
            with self.assertRaises(ValueError, msg=bad):
                server.split_host_port(bad)


class SSRFProtection(unittest.TestCase):
    def test_private_literals_blocked(self):
        for host in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1",
                     "0.0.0.0", "100.64.0.1", "::ffff:127.0.0.1", "localhost"):
            self.assertEqual(server.classify_host(host), "private", host)

    def test_public_literal_allowed(self):
        self.assertEqual(server.classify_host("8.8.8.8"), "public")

    def test_ipv6_bracket_bypass_is_blocked(self):
        client = server.app.test_client()
        r = client.get("/api/status?address=[::1]:25565")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Private", r.get_json()["error"])

    def test_srv_redirect_to_internal_host_blocked(self):
        fake = mock.Mock()
        fake.address.host = "127.0.0.1"
        with mock.patch.object(server, "classify_host", side_effect=["public", "private"]), \
             mock.patch.object(server.JavaServer, "lookup", return_value=fake):
            r = server.app.test_client().get("/api/status?address=example.com")
        self.assertEqual(r.status_code, 400)


class Hardening(unittest.TestCase):
    def test_security_headers(self):
        r = server.app.test_client().get("/")
        self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")

    def test_icon_validation(self):
        self.assertIsNone(server.safe_icon('data:text/html;base64,PHNjcmlwdD4='))
        self.assertIsNone(server.safe_icon('javascript:alert(1)'))
        self.assertEqual(server.safe_icon("data:image/png;base64,AAAA"), "data:image/png;base64,AAAA")

    def test_rate_limiter(self):
        rl = server.RateLimiter()
        self.assertTrue(all(rl.check("k", 3, 60) == 0 for _ in range(3)))
        self.assertGreater(rl.check("k", 3, 60), 0)
        self.assertEqual(rl.check("other", 3, 60), 0)

    def test_endpoint_rate_limit(self):
        client = server.app.test_client()
        codes = [client.get("/api/player?query=").status_code for _ in range(25)]
        self.assertIn(429, codes)

    def test_bad_upload_rejected(self):
        import io
        client = server.app.test_client()
        r = client.post("/api/pixel-art/export", data={"image": (io.BytesIO(b"GIF89a not an image"), "x.png")},
                        content_type="multipart/form-data")
        self.assertIn(r.status_code, (400, 429))


class PixelArt(unittest.TestCase):
    def test_palettes(self):
        for fam, pal in server.PIXEL_PALETTES.items():
            self.assertEqual(len(pal), 16, fam)

    def test_nearest_colour(self):
        pal = server.PIXEL_PALETTES["concrete"]
        px = np.array([[[207, 213, 214], [8, 10, 15]]], dtype=np.uint8)
        self.assertEqual(server.quantize(px, pal).tolist(), [[0, 3]])

    def test_dither_runs_and_stays_in_range(self):
        pal = server.PIXEL_PALETTES["wool"]
        px = np.tile(np.linspace(0, 255, 16, dtype=np.uint8)[:, None, None], (1, 16, 3))
        out = server.quantize(px, pal, dither=True)
        self.assertEqual(out.shape, (16, 16))
        self.assertTrue(out.min() >= 0 and out.max() < 16)

    def test_transparent_goes_white(self):
        from PIL import Image
        img = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
        self.assertEqual(server.to_rgb_on_white(img).getpixel((0, 0)), (255, 255, 255))


if __name__ == "__main__":
    unittest.main()
