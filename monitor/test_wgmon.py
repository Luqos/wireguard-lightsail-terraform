"""Tests for wgmon.py (standard library only): python -m unittest discover -s monitor"""
import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import wgmon


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        wgmon.DB_PATH = Path(self.tmp.name) / "test.db"
        with wgmon.db() as con:
            con.executescript(wgmon.SCHEMA)

    def tearDown(self):
        self.tmp.cleanup()


class ParseStats(unittest.TestCase):
    def test_parses_peers_and_names_unnamed(self):
        now, peers = wgmon.parse_stats("now\t100\naiman\t10.8.0.2\t90\t1000\t2000\n?\t10.8.0.3\t0\t0\t0\n")
        self.assertEqual(now, 100)
        self.assertEqual(peers[0], {"name": "aiman", "ip": "10.8.0.2", "handshake": 90, "rx": 1000, "tx": 2000})
        self.assertEqual(peers[1]["name"], "unnamed-10.8.0.3")

    def test_rejects_missing_now_and_bad_ip(self):
        with self.assertRaises(ValueError):
            wgmon.parse_stats("aiman\t10.8.0.2\t0\t0\t0\n")
        with self.assertRaises(ValueError):
            wgmon.parse_stats("now\t1\naiman\tnot-an-ip\t0\t0\t0\n")


class Status(unittest.TestCase):
    def test_thresholds(self):
        now = 1_000_000
        self.assertEqual(wgmon.peer_status(None, now), "unknown")
        self.assertEqual(wgmon.peer_status(0, now), "never")
        self.assertEqual(wgmon.peer_status(now - 60, now), "online")
        self.assertEqual(wgmon.peer_status(now - 3 * 86400, now), "active")
        self.assertEqual(wgmon.peer_status(now - 8 * 86400, now), "idle")


class Usage(TempDb):
    def usage(self):
        with wgmon.db() as con:
            return {r["peer_name"]: r["bytes"] for r in con.execute("SELECT * FROM peer_usage")}

    def test_deltas_and_counter_reset(self):
        t = 1_793_000_000  # same UTC month throughout
        peer = lambda rx, tx: [{"name": "a", "ip": "10.8.0.2", "handshake": t, "rx": rx, "tx": tx}]
        with wgmon.db() as con:
            wgmon.record_stats(con, "wg-my", t, peer(100, 200))        # first sight: 300
            wgmon.record_stats(con, "wg-my", t + 60, peer(150, 250))   # +100
            wgmon.record_stats(con, "wg-my", t + 120, peer(10, 20))    # reboot: +30
        self.assertEqual(self.usage(), {"a": 430})

    def test_removed_peer_leaves_state(self):
        t = 1_793_000_000
        with wgmon.db() as con:
            wgmon.record_stats(con, "wg-my", t, [{"name": "a", "ip": "10.8.0.2", "handshake": 0, "rx": 0, "tx": 0}])
            wgmon.record_stats(con, "wg-my", t + 60, [])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM peer_state").fetchone()[0], 0)


class Validation(unittest.TestCase):
    def test_person(self):
        self.assertEqual(wgmon.clean_person({"name": "aiman", "paid": True}, partial=False), {"name": "aiman", "paid": 1})
        for bad in ({"name": "a b"}, {"name": "x" * 33}, {"name": "ok", "stage": "admin"},
                    {"name": "ok", "paid_until": "next week"}, {"name": "ok", "paid": "yes"}):
            with self.assertRaises(wgmon.BadRequest):
                wgmon.clean_person(bad, partial=False)
        with self.assertRaises(wgmon.BadRequest):
            wgmon.clean_person({}, partial=False)

    def test_peer(self):
        self.assertEqual(wgmon.clean_peer({"server": "wg-my", "peer_name": "aiman"}), ("wg-my", "aiman"))
        with self.assertRaises(wgmon.BadRequest):
            wgmon.clean_peer({"server": "wg-my", "peer_name": "../etc"})


class FakeAws:
    def call(self, region, op, *args):
        raise RuntimeError("offline")


class Http(TempDb):
    def setUp(self):
        super().setUp()
        self.httpd = wgmon.ThreadingHTTPServer(("127.0.0.1", 0), wgmon.Handler)
        self.port = self.httpd.server_address[1]
        self.httpd.app, self.httpd.port, self.httpd.token = wgmon.Monitor(FakeAws(), []), self.port, "t0ken"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, token="t0ken", headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Token": token, "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read() or b"{}") if path.startswith("/api") else resp.read()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_guards(self):
        self.assertEqual(self.req("GET", "/api/state", token="wrong")[0], 401)
        self.assertEqual(self.req("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/state", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/state")[0], 200)
        bare = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/refresh", method="POST", data=b"",
                                      headers={"X-Token": "t0ken"})  # what the Refresh button sends
        with urllib.request.urlopen(bare) as resp:
            self.assertEqual(resp.status, 200)

    def test_track_existing_and_paid_ends_trial(self):
        code, st = self.req("POST", "/api/people", {"name": "qobe", "stage": "trial",
                                                    "peers": [{"server": "wg-my", "peer_name": "qobe"}]})
        self.assertEqual(code, 200)
        p = st["people"][0]
        self.assertEqual(self.req("POST", "/api/people", {"name": "QOBE"})[0], 409)  # names are case-insensitive
        p = self.req("PATCH", f"/api/people/{p['id']}", {"paid": True})[1]["people"][0]
        self.assertEqual(p["stage"], "member")
        self.assertEqual(self.req("DELETE", f"/api/people/{p['id']}")[0], 404)  # removal must go through /remove


class AdminFlow(Http):
    """Add/remove through a fake wgadmin gate."""

    def setUp(self):
        super().setUp()
        self.calls = []
        conf = base64.b64encode(b"[Interface]\nPrivateKey = X=\n").decode()
        svg = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()

        def fake_admin(ip, action, name):
            self.calls.append((action, name))
            if action == "remove" and name == "gone":
                raise RuntimeError("No peer 'gone'.")
            return f"ip\t10.8.0.9\nconf\t{conf}\nqr\t{svg}\n" if action == "add" else f"removed\t{name}\n"

        self.orig = wgmon.admin, wgmon.fetch_stats
        wgmon.admin = fake_admin
        wgmon.fetch_stats = lambda ip: "now\t100\n"
        app = self.httpd.app
        app.servers = {"wg-my": {"name": "wg-my", "state": "running", "ip": "203.0.113.10"}}
        app.refreshed_at = 1.0

    def tearDown(self):
        wgmon.admin, wgmon.fetch_stats = self.orig
        super().tearDown()

    def test_add_shows_config_once_and_starts_as_trial(self):
        code, r = self.req("POST", "/api/users/add", {"name": "aiman", "server": "wg-my", "contact": "@aiman"})
        self.assertEqual(code, 200)
        self.assertEqual(self.calls, [("add", "aiman")])
        self.assertIn("[Interface]", r["created"]["conf"])
        p = r["state"]["people"][0]
        self.assertEqual((p["stage"], p["peers"][0]["peer_name"]), ("trial", "aiman"))
        # nothing about the config is kept
        with wgmon.db() as con:
            dump = "\n".join(con.iterdump())
        self.assertNotIn("PrivateKey", dump)
        # taken names never reach the server
        self.assertEqual(self.req("POST", "/api/users/add", {"name": "aiman", "server": "wg-my"})[0], 400)
        self.assertEqual(self.calls, [("add", "aiman")])

    def test_add_rejects_bad_names_and_unknown_servers(self):
        for body in ({"name": "Aiman", "server": "wg-my"}, {"name": "../x", "server": "wg-my"},
                     {"name": "ok", "server": "wg-test"}):
            self.assertEqual(self.req("POST", "/api/users/add", body)[0], 400)
        self.assertEqual(self.calls, [])

    def test_remove_needs_exact_name_and_revokes(self):
        pid = self.req("POST", "/api/users/add", {"name": "aiman", "server": "wg-my"})[1]["state"]["people"][0]["id"]
        self.assertEqual(self.req("POST", f"/api/people/{pid}/remove", {"confirm": "Aiman"})[0], 400)
        self.assertNotIn(("remove", "aiman"), self.calls)
        code, r = self.req("POST", f"/api/people/{pid}/remove", {"confirm": "aiman"})
        self.assertEqual((code, r["revoked"], r["state"]["people"]), (200, ["wg-my/aiman"], []))

    def test_remove_tolerates_peer_already_gone_and_deleted_servers(self):
        st = self.req("POST", "/api/people", {"name": "gone", "peers": [
            {"server": "wg-my", "peer_name": "gone"}, {"server": "wg-test", "peer_name": "gone"}]})[1]
        p = st["people"][0]
        self.assertEqual({x["server"]: x["on_server"] for x in p["peers"]}["wg-test"], False)  # server deleted
        code, r = self.req("POST", f"/api/people/{p['id']}/remove", {"confirm": "gone"})
        self.assertEqual((code, r["state"]["people"]), (200, []))
        self.assertEqual(self.calls, [("remove", "gone")])  # wg-test is skipped, not contacted


class ParseAdded(unittest.TestCase):
    def test_rejects_unexpected_output(self):
        good = {"ip": "10.8.0.9", "conf": base64.b64encode(b"[Interface]\n").decode(),
                "qr": base64.b64encode(b"<svg/>").decode()}
        line = lambda d: "".join(f"{k}\t{v}\n" for k, v in d.items())
        self.assertEqual(wgmon.parse_added(line(good))["ip"], "10.8.0.9")
        for bad in ({**good, "qr": base64.b64encode(b"<script>").decode()}, {**good, "ip": "evil"},
                    {**good, "conf": "not base64!"}):
            with self.assertRaises((ValueError, KeyError)):
                wgmon.parse_added(line(bad))


if __name__ == "__main__":
    unittest.main()


class TunnelFirst(unittest.TestCase):
    """ssh() tries 10.8.0.1 first, falls back to the public IP, and never runs a command twice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = wgmon.KNOWN_HOSTS, wgmon.run, wgmon.tool
        wgmon.KNOWN_HOSTS = Path(self.tmp.name) / "known_hosts"
        wgmon.tool = lambda name: name
        self.targets, self.results = [], {}

        def fake_run(cmd, timeout=30, env=None):
            if cmd[0] == "ssh-keygen":  # host_fingerprint: is this IP pinned?
                ip = cmd[cmd.index("-F") + 1]
                if any(ln.startswith(ip + " ") for ln in self._pins()):
                    return f"{ip} ED25519 SHA256:abc\n"
                raise wgmon.CmdError("not found", 1)
            if cmd[0] == "ssh-keyscan":
                return self.results.get("keyscan", "")
            target = cmd[-1].split("@")[1] if "@" in cmd[-1] else cmd[-2].split("@")[1]
            self.targets.append(target)
            r = self.results.get(target, wgmon.CmdError("timed out", 255))
            if isinstance(r, Exception):
                raise r
            return r

        wgmon.run = fake_run

    def _pins(self):
        return wgmon.KNOWN_HOSTS.read_text().splitlines() if wgmon.KNOWN_HOSTS.exists() else []

    def tearDown(self):
        wgmon.KNOWN_HOSTS, wgmon.run, wgmon.tool = self.orig
        self.tmp.cleanup()

    def pin(self, ip, blob="KEYA"):
        with wgmon.KNOWN_HOSTS.open("a") as fh:
            fh.write(f"{ip} ssh-ed25519 {blob}\n")

    def test_pinned_uses_tunnel(self):
        self.pin("203.0.113.10")
        self.results["10.8.0.1"] = "ok"
        self.assertEqual(wgmon.fetch_stats("203.0.113.10"), "ok")
        self.assertEqual(self.targets, ["10.8.0.1"])

    def test_falls_back_to_public_ip(self):
        self.pin("203.0.113.10")
        self.results["203.0.113.10"] = "ok"
        self.assertEqual(wgmon.fetch_stats("203.0.113.10"), "ok")
        self.assertEqual(self.targets, ["10.8.0.1", "203.0.113.10"])

    def test_both_down_says_connect_wireguard(self):
        self.pin("203.0.113.10")
        with self.assertRaisesRegex(RuntimeError, "Connect WireGuard"):
            wgmon.fetch_stats("203.0.113.10")

    def test_remote_failure_is_not_retried(self):
        self.pin("203.0.113.10")
        self.results["10.8.0.1"] = wgmon.CmdError("Rate limit", 1)
        with self.assertRaisesRegex(RuntimeError, "Rate limit"):
            wgmon.admin("203.0.113.10", "add", "aiman")
        self.assertEqual(self.targets, ["10.8.0.1"])

    def test_first_contact_pins_over_tunnel(self):
        self.results["keyscan"] = "# 10.8.0.1:22 SSH-2.0\n10.8.0.1 ssh-ed25519 KEYB\n"
        self.results["10.8.0.1"] = "ok"
        self.assertEqual(wgmon.fetch_stats("203.0.113.10"), "ok")
        self.assertIn("203.0.113.10 ssh-ed25519 KEYB", self._pins())

    def test_refuses_to_pin_another_servers_key(self):
        self.pin("13.239.200.208", "KEYB")  # the tunnel actually goes to this server
        self.results["keyscan"] = "10.8.0.1 ssh-ed25519 KEYB\n"
        with self.assertRaisesRegex(RuntimeError, "Connect WireGuard"):
            wgmon.fetch_stats("203.0.113.10")
        self.assertEqual(self.targets, ["203.0.113.10"])  # tunnel never used for an unpinned server
        self.assertFalse(any(p.startswith("203.0.113.10") for p in self._pins()))
