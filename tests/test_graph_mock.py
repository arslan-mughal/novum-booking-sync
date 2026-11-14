import json
import os
import tempfile
import unittest
from pathlib import Path

from bsync import graph as G
from tests import mock_graph as M


class Base(unittest.TestCase):
    def setUp(self):
        self.cloud = M.FakeCloud()
        self.srv, self.url = M.start(self.cloud)
        os.environ["GRAPH_BASE"] = self.url + "/v1.0"
        os.environ["LOGIN_BASE"] = self.url
        self.tmp = Path(tempfile.mkdtemp())
        self.sleeps = []

    def tearDown(self):
        self.srv.shutdown()
        os.environ.pop("GRAPH_BASE", None)
        os.environ.pop("LOGIN_BASE", None)

    def od(self, auth=None, drive="me"):
        return G.OneDrive(auth or G.ClientCredentialsAuth("t", "cid", "sek"), drive, sleep=self.sleeps.append)


class AuthTests(Base):
    def test_client_credentials_and_bad_secret(self):
        self.assertTrue(G.ClientCredentialsAuth("t", "c", "sek").token().startswith("AT-app"))
        with self.assertRaises(G.AuthError) as cm:
            G.ClientCredentialsAuth("t", "c", "wrong").token()
        self.assertIn("AADSTS7000215", str(cm.exception))

    def test_refresh_token_rotation_is_persisted_and_reused(self):
        tf = self.tmp / "tok.json"
        a = G.RefreshTokenAuth("t", "c", "RT-0", str(tf))
        a.token()
        self.assertEqual(json.loads(tf.read_text())["refresh_token"], "RT-1")
        b = G.RefreshTokenAuth("t", "c", "RT-0", str(tf))       # a later run: file wins over the env value
        self.assertEqual(b.rt, "RT-1")
        b.token()
        self.assertEqual(json.loads(tf.read_text())["refresh_token"], "RT-2")

    def test_falls_back_to_env_token_when_saved_one_is_dead(self):
        tf = self.tmp / "tok.json"
        tf.write_text(json.dumps({"refresh_token": "RT-dead"}))
        a = G.RefreshTokenAuth("t", "c", "RT-0", str(tf))
        self.assertTrue(a.token())
        self.assertEqual(a.rt, "RT-1")

    def test_all_tokens_dead_gives_actionable_error(self):
        with self.assertRaises(G.AuthError) as cm:
            G.RefreshTokenAuth("t", "c", "RT-nope").token()
        self.assertIn("login_device.py", str(cm.exception))

    def test_env_selection(self):
        e = {"MS_CLIENT_ID": "c", "MS_TENANT_ID": "t", "MS_CLIENT_SECRET": "sek"}
        self.assertIsInstance(G.auth_from_env(e), G.ClientCredentialsAuth)
        self.assertIsInstance(G.auth_from_env({**e, "MS_REFRESH_TOKEN": "RT-0"}), G.RefreshTokenAuth)
        with self.assertRaises(G.AuthError):
            G.auth_from_env({"MS_CLIENT_ID": "c"})
        self.assertEqual(G.drive_spec_from_env({}), "me")
        self.assertEqual(G.drive_spec_from_env({"ONEDRIVE_USER": "a@b.c"}), "user:a@b.c")
        self.assertEqual(G.drive_spec_from_env({"ONEDRIVE_USER": "a@b.c", "ONEDRIVE_DRIVE_ID": "X1"}), "id:X1")


class DriveTests(Base):
    def test_upload_create_then_exists_and_exotic_names(self):
        od = self.od()
        p = "NOVUM/novum-hotels-images/Holiday Inn – the niu #1/Standard Zimmer - 12 m²/Bad-Dusche 100%/01_123.jpg"
        self.assertEqual(od.upload_bytes(p, b"abc"), "created")
        self.assertEqual(self.cloud.files[p]["data"], b"abc")
        self.assertEqual(od.upload_bytes(p, b"zzz"), "exists")
        self.assertEqual(self.cloud.files[p]["data"], b"abc", "must not overwrite an existing file")
        self.assertEqual(od.upload_bytes(p, b"zzz", replace=True), "created")
        self.assertEqual(self.cloud.files[p]["data"], b"zzz")

    def test_parent_folders_are_created_when_service_does_not_do_it(self):
        self.cloud.auto_create_parents = False
        od = self.od()
        self.assertEqual(od.upload_bytes("A/B/C/f.jpg", b"1"), "created")
        self.assertIn("A/B/C", self.cloud.folders)
        self.assertEqual(od.upload_bytes("A/B/C/g.jpg", b"1"), "created")

    def test_retry_after_throttling_and_token_expiry(self):
        od = self.od()
        self.cloud.throttle = 2
        self.assertEqual(od.upload_bytes("T/x.jpg", b"1"), "created")
        self.assertEqual(len(self.sleeps), 2)
        self.cloud.valid_access.clear()                       # server forgets our token -> 401 -> refresh -> retry
        self.assertEqual(od.upload_bytes("T/y.jpg", b"1"), "created")

    def test_large_upload_uses_session_and_roundtrips(self):
        od = self.od()
        data = os.urandom(G.SIMPLE_UPLOAD_LIMIT + 3 * G.CHUNK + 123)
        od.upload_bytes("Big/file.bin", data)
        self.assertEqual(self.cloud.files["Big/file.bin"]["data"], data)

    def test_replace_with_if_match(self):
        od = self.od()
        self.cloud.put("W/book.xlsx", b"v1")
        item = od.get_item("W/book.xlsx")
        new = od.replace_file("W/book.xlsx", self._write(b"v2"), if_match=item["eTag"])
        self.assertEqual(self.cloud.files["W/book.xlsx"]["data"], b"v2")
        self.assertEqual(new["name"], "book.xlsx")
        self.cloud.bump_etag("W/book.xlsx")                    # someone edited it meanwhile
        with self.assertRaises(G.PreconditionFailed):
            od.replace_file("W/book.xlsx", self._write(b"v3"), if_match=item["eTag"])
        self.assertEqual(self.cloud.files["W/book.xlsx"]["data"], b"v2")
        self.cloud.locked.add("W/book.xlsx")
        with self.assertRaises(G.Locked):
            od.replace_file("W/book.xlsx", self._write(b"v4"))

    def test_listing_paging_download_and_missing(self):
        od = self.od()
        for n in "abcde":
            self.cloud.put(f"L/{n}.jpg", n.encode())
        self.assertEqual(od.list_names("L"), [f"{n}.jpg" for n in "abcde"])
        self.assertEqual(od.list_names("nope"), [])
        self.assertIsNone(od.get_item("L/zzz.jpg"))
        od.download("L/c.jpg", self.tmp / "d" / "c.jpg")
        self.assertEqual((self.tmp / "d" / "c.jpg").read_bytes(), b"c")
        self.assertIsNone(od.read_bytes("L/zzz.jpg"))

    def test_drive_prefixes(self):
        self.assertEqual(self.od(drive="user:a@b.com").base, "/users/a@b.com/drive")
        self.assertEqual(self.od(drive="id:b!xyz").base, "/drives/b%21xyz")
        self.assertTrue(self.od(drive="user:a@b.com").upload_bytes("U/x.txt", b"1"))

    def _write(self, data):
        p = self.tmp / "up.bin"
        p.write_bytes(data)
        return p


if __name__ == "__main__":
    unittest.main()
