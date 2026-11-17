#!/usr/bin/env python3
"""Local user tracker and transfer monitor for the WireGuard servers.

Run (PowerShell):
    $env:AWS_PROFILE = "wg"          # or pass --profile
    py -3.12 monitor/wgmon.py        # opens http://127.0.0.1:8765 in your browser

Security model:
- Standard library only: nothing to pip install, no CDNs.
- Listens on 127.0.0.1 only and rejects foreign Host/Origin headers (DNS rebinding, CSRF).
  Every /api call needs a per-launch token, handed to the page in the URL fragment.
- AWS calls go through the aws CLI and are limited to read-only Lightsail operations.
- Per-peer stats come from the "wgmon" SSH account, which each server pins to
  "wg-setup stats" (see scripts/wg-setup.sh). That output contains no keys.
- Adding/removing peers uses a separate "wgadmin" key whose forced command only accepts
  "add <name>" / "remove <name>" (rate-limited and logged on the server). New client
  configs are shown once and never stored here.
- Data (SQLite, SSH keys, pinned host keys) lives in %LOCALAPPDATA%\\wg-monitor, outside the repo.
"""
from __future__ import annotations

import argparse
import base64
import calendar
import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WG_SETUP = HERE.parent / "scripts" / "wg-setup.sh"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "wg-monitor"
DB_PATH = DATA / "wgmon.db"
KEY = DATA / "id_ed25519"              # wgmon: read-only stats
ADMIN_KEY = DATA / "id_ed25519_admin"  # wgadmin: add/remove peers only
KNOWN_HOSTS = DATA / "known_hosts"
TUNNEL_IP = "10.8.0.1"                 # each server's wg0 address (WG_NET in wg-setup.sh)

ONLINE_SECS = 180            # handshake in the last 3 minutes
ACTIVE_SECS = 7 * 86400      # connected in the last 7 days
GIB = 1024 ** 3
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
PEER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")  # what wg-setup accepts for new peers
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AWS_READ_ONLY = {"get-instances", "get-instance-metric-data", "get-alarms"}
PROJECT_TAG = ("project", "wireguard-vpn")  # default_tags in versions.tf
STAGES = ("trial", "member", "former")

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  stage      TEXT NOT NULL DEFAULT 'member',
  device     TEXT NOT NULL DEFAULT '',
  contact    TEXT NOT NULL DEFAULT '',
  notes      TEXT NOT NULL DEFAULT '',
  paid       INTEGER NOT NULL DEFAULT 0,
  paid_until TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
-- Links a person to a peer ("# name" in wg0.conf) on a server.
CREATE TABLE IF NOT EXISTS peers (
  id        INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  server    TEXT NOT NULL,
  peer_name TEXT NOT NULL,
  UNIQUE (server, peer_name)
);
-- Last counters seen per server peer, from the latest successful stats fetch.
CREATE TABLE IF NOT EXISTS peer_state (
  server TEXT, peer_name TEXT, ip TEXT, handshake INTEGER, rx INTEGER, tx INTEGER, seen_at INTEGER,
  PRIMARY KEY (server, peer_name)
);
-- WireGuard counters reset on reboot, so usage is accumulated from deltas per UTC month.
CREATE TABLE IF NOT EXISTS peer_usage (
  server TEXT, peer_name TEXT, month TEXT, bytes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (server, peer_name, month)
);
"""


# ---------------------------------------------------------------- helpers

class CmdError(RuntimeError):
    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


def run(cmd: list[str], timeout: int = 30, env: dict | None = None) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode != 0:
        lines = (p.stderr or p.stdout).strip().splitlines()
        raise CmdError(lines[-1] if lines else f"{Path(cmd[0]).name} exited {p.returncode}", p.returncode)
    return p.stdout


def tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} not found on PATH")
    return path


def lock_down(path: Path) -> None:
    """Make a file readable by the current user only."""
    if os.name == "nt":
        run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.environ['USERNAME']}:F"])
    else:
        path.chmod(0o600)


@contextmanager
def db():
    """One transaction: commits on success, rolls back on error, always closes."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def month_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def peer_status(handshake: int | None, now: float) -> str:
    if handshake is None:
        return "unknown"
    if handshake == 0:
        return "never"
    age = now - handshake
    return "online" if age <= ONLINE_SECS else "active" if age <= ACTIVE_SECS else "idle"


STATUS_RANK = {"unknown": 0, "never": 1, "idle": 2, "active": 3, "online": 4}


# ---------------------------------------------------------------- AWS + SSH

class Aws:
    def __init__(self, profile: str):
        self.env = {**os.environ, "AWS_PROFILE": profile}

    def call(self, region: str, op: str, *args: str) -> dict:
        if op not in AWS_READ_ONLY:
            raise ValueError(f"{op} is not an allowed read-only call")
        try:
            out = run([tool("aws"), "lightsail", op, "--region", region, *args, "--output", "json"],
                      timeout=60, env=self.env)
        except RuntimeError as e:
            if "expired" in str(e).lower() or "aws login" in str(e):
                raise RuntimeError("AWS session expired: run  aws login --profile wg") from e
            raise
        return json.loads(out)


def ensure_keys() -> None:
    for key, comment in ((KEY, "wg-monitor"), (ADMIN_KEY, "wg-admin")):
        if not key.exists():
            run([tool("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key)])
        lock_down(key)


def _ssh(target: str, alias: str, user: str, key: Path, remote: str | None,
         timeout: int, connect_timeout: int) -> str:
    return run([
        tool("ssh"), "-T", "-i", str(key),
        "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS.as_posix()}",
        # Host keys are always pinned under the server's public IP, even over the tunnel.
        "-o", f"HostKeyAlias={alias}",
        "-o", "StrictHostKeyChecking=accept-new",  # pin on first use; a changed key then fails
        "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", f"ConnectTimeout={connect_timeout}",
        f"{user}@{target}", *([remote] if remote else []),
    ], timeout=timeout)


def ssh(ip: str, user: str, key: Path, remote: str | None = None, timeout: int = 25) -> str:
    """Over the WireGuard tunnel first, so port 22 never has to be open to your home IP.

    Every server is 10.8.0.1 inside its own tunnel. HostKeyAlias checks that address
    against the key pinned for this server's public IP, so if you're connected to a
    different server the check fails and nothing runs there. The public IP is the
    fallback (for example the VPN is off, or the key isn't pinned yet).
    """
    ipaddress.ip_address(ip)  # never pass anything else to ssh
    if not host_fingerprint(ip):
        try:
            pin_over_tunnel(ip)
        except (RuntimeError, subprocess.TimeoutExpired):
            pass  # not on the VPN: the public IP below pins it instead, if it's reachable
    tunnel_error = None
    if host_fingerprint(ip):
        try:
            return _ssh(TUNNEL_IP, ip, user, key, remote, timeout, connect_timeout=4)
        except CmdError as e:
            if e.code != 255:  # ssh connected and the command itself failed: don't run it twice
                raise
            tunnel_error = e
        except subprocess.TimeoutExpired as e:
            tunnel_error = e
    try:
        return _ssh(ip, ip, user, key, remote, timeout, connect_timeout=8)
    except (CmdError, subprocess.TimeoutExpired) as e:
        if isinstance(e, CmdError) and e.code != 255:
            raise
        detail = tunnel_error if isinstance(tunnel_error, CmdError) else e
        raise RuntimeError(f"can't reach {ip} over the VPN ({TUNNEL_IP}) or its public IP ({detail}). "
                           "Connect WireGuard on this PC to this server, then press Refresh") from e


def pin_over_tunnel(ip: str) -> None:
    """First contact over the VPN: pin the key 10.8.0.1 offers under this server's public IP.

    Refuses if that key is already pinned for another server, which means the tunnel
    you're on goes there and not to this one.
    """
    out = run([tool("ssh-keyscan"), "-T", "4", "-t", "ed25519", TUNNEL_IP], timeout=15)
    found = [ln.split() for ln in out.splitlines() if ln and not ln.startswith("#")]
    found = [f for f in found if len(f) == 3 and f[1] == "ssh-ed25519"]
    if not found:
        raise RuntimeError(f"no host key from {TUNNEL_IP}")
    _, kind, blob = found[0]
    pinned = KNOWN_HOSTS.read_text().splitlines() if KNOWN_HOSTS.exists() else []
    for ln in pinned:
        f = ln.split()
        if len(f) >= 3 and f[2] == blob and f[0] != ip:
            raise RuntimeError(f"the VPN on this PC goes to the server at {f[0]}, not {ip}")
    with KNOWN_HOSTS.open("a", encoding="ascii") as fh:
        fh.write(f"{ip} {kind} {blob}\n")


def fetch_stats(ip: str) -> str:
    return ssh(ip, "wgmon", KEY)


def admin(ip: str, action: str, name: str) -> str:
    """Runs "add <name>" or "remove <name>" through the server's wgadmin gate."""
    if action not in ("add", "remove") or not PEER_NAME_RE.match(name):
        raise BadRequest("peer names: a-z 0-9 . _ - (max 32, starting with a letter or digit)")
    return ssh(ip, "wgadmin", ADMIN_KEY, f"{action} {name}", timeout=40)


def parse_added(text: str) -> dict:
    """Parse api-add output: ip, conf (base64) and qr (base64 SVG) lines."""
    f = dict(line.split("\t", 1) for line in text.splitlines() if "\t" in line)
    conf = base64.b64decode(f["conf"], validate=True).decode()
    svg = base64.b64decode(f["qr"], validate=True)
    if "[Interface]" not in conf or b"<svg" not in svg[:300]:
        raise ValueError("unexpected add output from server")
    ipaddress.ip_address(f["ip"])
    return {"ip": f["ip"], "conf": conf, "qr_svg_b64": f["qr"]}


def host_fingerprint(ip: str) -> str | None:
    if not KNOWN_HOSTS.exists():
        return None
    try:
        out = run([tool("ssh-keygen"), "-l", "-F", ip, "-f", str(KNOWN_HOSTS)])
    except RuntimeError:
        return None
    m = re.search(r"SHA256:\S+", out)
    return m.group(0) if m else None


def parse_stats(text: str) -> tuple[int, list[dict]]:
    """Parse "wg-setup stats": a "now<TAB>epoch" line, then name, ip, handshake, rx, tx."""
    now, peers = None, []
    for line in text.splitlines():
        f = line.split("\t")
        if f[0] == "now" and len(f) == 2:
            now = int(f[1])
        elif len(f) == 5:
            name, ip = f[0], f[1]
            ipaddress.ip_address(ip)
            if not NAME_RE.match(name):
                name = f"unnamed-{ip}"  # peer added without a "# name" comment
            peers.append({"name": name, "ip": ip, "handshake": int(f[2]), "rx": int(f[3]), "tx": int(f[4])})
    if now is None:
        raise ValueError("stats output had no 'now' line")
    return now, peers


def record_stats(con: sqlite3.Connection, server: str, now: int, peers: list[dict]) -> None:
    month = month_key(now)
    prev = {r["peer_name"]: r for r in con.execute("SELECT * FROM peer_state WHERE server = ?", (server,))}

    def grew(new: int, old: int) -> int:
        return new - old if new >= old else new  # counter reset (reboot): count from zero

    for p in peers:
        old = prev.get(p["name"])
        delta = grew(p["rx"], old["rx"]) + grew(p["tx"], old["tx"]) if old else p["rx"] + p["tx"]
        con.execute("""INSERT INTO peer_usage (server, peer_name, month, bytes) VALUES (?, ?, ?, ?)
                       ON CONFLICT DO UPDATE SET bytes = bytes + excluded.bytes""",
                    (server, p["name"], month, delta))
        con.execute("INSERT OR REPLACE INTO peer_state VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (server, p["name"], p["ip"], p["handshake"], p["rx"], p["tx"], now))
    names = [p["name"] for p in peers]
    con.execute(f"DELETE FROM peer_state WHERE server = ? AND peer_name NOT IN ({','.join('?' * len(names))})",
                (server, *names))


# ---------------------------------------------------------------- monitor

class Monitor:
    def __init__(self, aws: Aws, regions: list[str]):
        self.aws, self.regions = aws, regions
        self.lock = threading.Lock()
        self.servers: dict[str, dict] = {}
        self.errors: list[str] = []
        self.refreshed_at: float | None = None
        self.refreshing = False
        self.admin_lock = threading.Lock()  # one add/remove at a time

    def loop(self, interval: int) -> None:
        while True:
            try:
                self.refresh()
            except Exception as e:  # keep the background loop alive
                self.errors = [f"refresh failed: {e}"]
            time.sleep(interval)

    def refresh(self) -> None:
        with self.lock:
            self.refreshing = True
            try:
                self._refresh()
            finally:
                self.refreshing = False

    def _refresh(self) -> None:
        servers, errors = {}, []
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for region in self.regions:
            try:
                instances = self.aws.call(region, "get-instances")["instances"]
            except Exception as e:
                errors.append(f"{region}: {e}")
                continue
            for inst in instances:
                tags = {t["key"]: t.get("value", "") for t in inst.get("tags", [])}
                if tags.get(PROJECT_TAG[0]) != PROJECT_TAG[1]:
                    continue
                servers[inst["name"]] = self._server(region, inst, now, month_start)
        if not errors:  # every Region answered, so servers missing now are really gone
            with db() as con:
                con.execute(f"DELETE FROM peer_state WHERE server NOT IN ({','.join('?' * len(servers))})",
                            tuple(servers))
        self.servers, self.errors, self.refreshed_at = servers, errors, time.time()

    def _fetch_peers(self, s: dict) -> None:
        try:
            server_now, peers = parse_stats(fetch_stats(s["ip"]))
            with db() as con:
                record_stats(con, s["name"], server_now, peers)
            s.update(stats_ok=True, stats_error=None, server_now=server_now, peer_count=len(peers))
        except Exception as e:
            s.update(stats_ok=False, stats_error=str(e))

    def _server(self, region: str, inst: dict, now: datetime, month_start: datetime) -> dict:
        name, ip = inst["name"], inst.get("publicIpAddress")
        s = {
            "name": name, "region": region, "ip": ip, "static": inst.get("isStaticIp", False),
            "state": inst["state"]["name"], "bundle": inst.get("bundleId"),
            "allowance_gb": inst.get("networking", {}).get("monthlyTransfer", {}).get("gbPerMonthAllocated", 0),
            "daily": [], "used_gb": 0.0, "projected_gb": None, "alarm": "none",
            "stats_ok": False, "stats_error": None, "server_now": None, "fingerprint": None,
            "errors": [],
        }
        try:
            per_metric = {}
            for metric in ("NetworkIn", "NetworkOut"):
                r = self.aws.call(region, "get-instance-metric-data", "--instance-name", name,
                                  "--metric-name", metric, "--period", "86400", "--unit", "Bytes",
                                  "--statistics", "Sum", "--start-time", month_start.isoformat(),
                                  "--end-time", now.isoformat())
                per_metric[metric] = {datetime.fromisoformat(d["timestamp"]).astimezone(timezone.utc).day: d["sum"]
                                      for d in r["metricData"]}
            s["daily"] = [{"day": d, "in_gb": per_metric["NetworkIn"].get(d, 0) / GIB,
                           "out_gb": per_metric["NetworkOut"].get(d, 0) / GIB} for d in range(1, now.day + 1)]
            s["used_gb"] = sum(d["in_gb"] + d["out_gb"] for d in s["daily"])
            elapsed_days = (now - month_start).total_seconds() / 86400
            if elapsed_days > 0.25:
                days_in_month = calendar.monthrange(now.year, now.month)[1]
                s["projected_gb"] = s["used_gb"] / elapsed_days * days_in_month
        except Exception as e:
            s["errors"].append(f"metrics: {e}")
        try:
            alarms = self.aws.call(region, "get-alarms", "--monitored-resource-name", name)["alarms"]
            s["alarm"] = alarms[0]["state"] if alarms else "none"
        except Exception as e:
            s["errors"].append(f"alarms: {e}")
        if s["state"] == "running" and ip:
            self._fetch_peers(s)
            s["fingerprint"] = host_fingerprint(ip)
        return s

    # -------------------------------------------------------- add / remove

    def _reachable(self, server: str) -> dict:
        s = self.servers.get(server)
        if not s or s["state"] != "running" or not s["ip"]:
            raise BadRequest(f"server {server} isn't running or hasn't been loaded yet; press Refresh")
        return s

    def add_user(self, body: dict) -> dict:
        """Creates the peer on the server, then the record. Returns the one-time config."""
        name, server = body.get("name"), body.get("server")
        if not (isinstance(name, str) and PEER_NAME_RE.match(name)):
            raise BadRequest("name: a-z 0-9 . _ - (max 32, starting with a letter or digit)")
        fields = clean_person({k: v for k, v in body.items() if k not in ("server", "stage")}, partial=False)
        fields["stage"] = "member" if fields.get("paid") else "trial"
        s = self._reachable(server)
        with self.admin_lock:
            with db() as con:  # fail before touching the server if the name is taken
                if con.execute("SELECT 1 FROM people WHERE name = ?", (name,)).fetchone() or \
                   con.execute("SELECT 1 FROM peers WHERE server = ? AND peer_name = ?", (server, name)).fetchone():
                    raise BadRequest(f"{name} is already in the tracker")
            out = admin(s["ip"], "add", name)
            try:
                created = parse_added(out)
            except Exception as e:  # the peer exists now; it will show as untracked after Refresh
                raise RuntimeError(f"{name} was added on the server but its config couldn't be read ({e}). "
                                   f"Get it with the browser SSH: sudo cat /root/wg-clients/{name}.conf") from e
            try:
                with db() as con:
                    cur = con.execute(
                        f"INSERT INTO people ({', '.join(fields)}, created_at) VALUES ({', '.join('?' * len(fields))}, ?)",
                        (*fields.values(), datetime.now(timezone.utc).isoformat(timespec="seconds")))
                    con.execute("INSERT INTO peers (person_id, server, peer_name) VALUES (?, ?, ?)",
                                (cur.lastrowid, server, name))
            except Exception:
                admin(s["ip"], "remove", name)  # don't leave an untracked peer behind
                raise
            self._fetch_peers(s)
        return {"name": name, "server": server, **created}

    def remove_user(self, pid: int, confirm: str) -> list[str]:
        """Revokes every linked peer on its server, then deletes the record."""
        with db() as con:
            person = con.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
            if not person:
                raise LookupError("no such user")
            links = [dict(r) for r in con.execute("SELECT * FROM peers WHERE person_id = ?", (pid,))]
        if confirm != person["name"]:
            raise BadRequest("type the user's name exactly to confirm")
        revoked = []
        with self.admin_lock:
            for link in links:
                gone = link["server"] not in self.servers and self.refreshed_at and not self.errors
                if gone:
                    continue  # the server itself no longer exists
                s = self._reachable(link["server"])
                try:
                    admin(s["ip"], "remove", link["peer_name"])
                except RuntimeError as e:
                    if "No peer" not in str(e):  # already removed on the server is fine
                        raise
                revoked.append(f"{link['server']}/{link['peer_name']}")
            with db() as con:
                con.execute("DELETE FROM people WHERE id = ?", (pid,))
            for s in {link["server"] for link in links} & set(self.servers):
                self._fetch_peers(self.servers[s])
        return revoked

    # -------------------------------------------------------- read model

    def snapshot(self) -> dict:
        today = date.today().isoformat()
        month = month_key(time.time())
        with db() as con:
            people = [dict(r) for r in con.execute("SELECT * FROM people ORDER BY name COLLATE NOCASE")]
            links = [dict(r) for r in con.execute("SELECT * FROM peers")]
            states = {(r["server"], r["peer_name"]): dict(r) for r in con.execute("SELECT * FROM peer_state")}
            usage = {(r["server"], r["peer_name"]): r["bytes"]
                     for r in con.execute("SELECT * FROM peer_usage WHERE month = ?", (month,))}

        def server_now(server: str) -> float:
            return (self.servers.get(server) or {}).get("server_now") or time.time()

        all_regions_ok = bool(self.refreshed_at) and not self.errors

        def peer_view(server: str, peer_name: str) -> dict:
            st = states.get((server, peer_name))
            fetched = (self.servers.get(server) or {}).get("stats_ok", False) or \
                (server not in self.servers and all_regions_ok)  # the server no longer exists
            return {
                "server": server, "peer_name": peer_name,
                "ip": st["ip"] if st else None,
                "last_seen": st["handshake"] if st and st["handshake"] else None,
                "status": peer_status(st["handshake"] if st else None, server_now(server)),
                "month_bytes": usage.get((server, peer_name), 0),
                "on_server": (st is not None) if fetched else None,
            }

        by_person: dict[int, list] = {}
        for link in links:
            by_person.setdefault(link["person_id"], []).append({"id": link["id"], **peer_view(link["server"], link["peer_name"])})
        for p in people:
            p["paid"] = bool(p["paid"])
            p["peers"] = sorted(by_person.get(p["id"], []), key=lambda x: x["server"])
            p["status"] = max((x["status"] for x in p["peers"]), key=STATUS_RANK.get, default="unknown")
            p["last_seen"] = max((x["last_seen"] or 0 for x in p["peers"]), default=0) or None
            p["month_bytes"] = sum(x["month_bytes"] for x in p["peers"])
            p["overdue"] = bool(p["paid_until"]) and p["paid_until"] < today

        linked = {(link["server"], link["peer_name"]) for link in links}
        unknown = [peer_view(s, n) for (s, n) in sorted(states) if (s, n) not in linked]
        missing = [{"person": p["name"], **x} for p in people for x in p["peers"] if x["on_server"] is False]

        return {
            "servers": sorted(self.servers.values(), key=lambda s: s["name"]),
            "people": people, "unknown_peers": unknown, "missing_peers": missing,
            "errors": self.errors, "refreshed_at": self.refreshed_at, "refreshing": self.refreshing,
            "rules": {"online_secs": ONLINE_SECS, "active_secs": ACTIVE_SECS},
        }

    def setup_info(self) -> dict:
        pub = KEY.with_name(KEY.name + ".pub").read_text().strip()
        admin_pub = ADMIN_KEY.with_name(ADMIN_KEY.name + ".pub").read_text().strip()
        script = WG_SETUP.read_bytes().replace(b"\r\n", b"\n")
        sha = hashlib.sha256(script).hexdigest()
        blob = base64.b64encode(gzip.compress(script, mtime=0)).decode()
        # Installs the current wg-setup (checksum-verified), then (re)creates the wgmon
        # and wgadmin accounts for this app's keys. One line for the browser SSH.
        update = (f"f=$(mktemp) && echo '{blob}' | base64 -d | gunzip > \"$f\" && "
                  f"echo \"{sha}  $f\" | sha256sum -c --quiet - && "
                  f"sudo install -m 700 \"$f\" /usr/local/sbin/wg-setup && rm -f \"$f\" && "
                  f"sudo wg-setup monitor '{pub}' && sudo wg-setup admin '{admin_pub}'")
        return {
            "pubkey": pub, "admin_pubkey": admin_pub, "script_sha256": sha,
            "update_cmd": update,
            "servers": [{k: s.get(k) for k in ("name", "ip", "fingerprint", "stats_ok", "stats_error")}
                        for s in sorted(self.servers.values(), key=lambda s: s["name"])],
        }


# ---------------------------------------------------------------- validation

class BadRequest(Exception):
    pass


def clean_person(data: dict, partial: bool) -> dict:
    out = {}
    rules = {
        "name": lambda v: isinstance(v, str) and NAME_RE.match(v),
        "stage": lambda v: v in STAGES,
        "device": lambda v: isinstance(v, str) and len(v) <= 40,
        "contact": lambda v: isinstance(v, str) and len(v) <= 120,
        "notes": lambda v: isinstance(v, str) and len(v) <= 2000,
        "paid": lambda v: isinstance(v, bool),
        "paid_until": lambda v: v == "" or (isinstance(v, str) and DATE_RE.match(v)),
    }
    for key, ok in rules.items():
        if key in data:
            if not ok(data[key]):
                raise BadRequest(f"invalid {key}")
            out[key] = int(data[key]) if key == "paid" else data[key].strip() if isinstance(data[key], str) else data[key]
    if not partial and "name" not in out:
        raise BadRequest("name is required (letters, digits, . _ -, max 32)")
    return out


def clean_peer(data: dict) -> tuple[str, str]:
    server, peer = data.get("server"), data.get("peer_name")
    if not (isinstance(server, str) and NAME_RE.match(server) and isinstance(peer, str) and NAME_RE.match(peer)):
        raise BadRequest("server and peer_name must be letters, digits, . _ - (max 32)")
    return server, peer


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "wgmon"
    sys_version = ""

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        srv = self.server
        hosts = {f"127.0.0.1:{srv.port}", f"localhost:{srv.port}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in hosts or (origin and origin not in {f"http://{h}" for h in hosts}):
            return self._send(403, {"error": "forbidden"})
        path = self.path.split("?", 1)[0]

        if method == "GET" and path in STATIC:
            fname, ctype = STATIC[path]
            return self._send(200, (HERE / "static" / fname).read_bytes(), ctype)
        if not path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not hmac.compare_digest(self.headers.get("X-Token", ""), srv.token):
            return self._send(401, {"error": "missing or wrong token: open the URL printed in the console"})

        try:
            body = self._body() if method in ("POST", "PATCH") else {}
            return self._send(200, self._api(method, path, body, srv.app))
        except BadRequest as e:
            return self._send(400, {"error": str(e)})
        except sqlite3.IntegrityError as e:
            msg = "that name is already used" if "people.name" in str(e) else \
                  "that server peer is already linked to someone" if "peers." in str(e) else str(e)
            return self._send(409, {"error": msg})
        except LookupError as e:
            return self._send(404, {"error": str(e)})
        except ValueError:  # malformed JSON or numbers
            return self._send(400, {"error": "malformed request"})
        except RuntimeError as e:  # ssh/aws failed; the message is the tool's last stderr line
            return self._send(502, {"error": str(e)})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            raise BadRequest("body too large")
        if length == 0:
            return {}
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise BadRequest("expected application/json")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise BadRequest("expected a JSON object")
        return data

    def _api(self, method: str, path: str, body: dict, app: Monitor) -> dict:
        m = re.fullmatch(r"/api/people/(\d+)(/peers|/remove)?", path)
        pid, sub = (int(m.group(1)), m.group(2)) if m else (None, None)

        if method == "POST" and sub == "/remove":
            revoked = app.remove_user(pid, body.get("confirm"))
            return {"state": app.snapshot(), "revoked": revoked}

        if method == "GET" and path == "/api/state":
            return app.snapshot()
        if method == "GET" and path == "/api/setup":
            return app.setup_info()
        if method == "POST" and path == "/api/refresh":
            app.refresh()
            return app.snapshot()

        if method == "POST" and path == "/api/users/add":
            created = app.add_user(body)
            return {"state": app.snapshot(), "created": created}

        if method == "POST" and path == "/api/people":  # track a peer that already exists
            fields = clean_person(body, partial=False)
            peers = [clean_peer(p) for p in body.get("peers", [])]
            fields.setdefault("stage", "member")
            with db() as con:
                cur = con.execute(
                    f"INSERT INTO people ({', '.join(fields)}, created_at) VALUES ({', '.join('?' * len(fields))}, ?)",
                    (*fields.values(), datetime.now(timezone.utc).isoformat(timespec="seconds")))
                con.executemany("INSERT INTO peers (person_id, server, peer_name) VALUES (?, ?, ?)",
                                [(cur.lastrowid, s, n) for s, n in peers])
            return app.snapshot()

        if pid is not None:
            with db() as con:
                if not con.execute("SELECT 1 FROM people WHERE id = ?", (pid,)).fetchone():
                    raise LookupError("no such user")
                if method == "PATCH" and sub is None:
                    fields = clean_person(body, partial=True)
                    if fields.get("paid") and "stage" not in fields:  # paying ends the trial
                        con.execute("UPDATE people SET stage = 'member' WHERE id = ? AND stage = 'trial'", (pid,))
                    if fields:
                        con.execute(f"UPDATE people SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                                    (*fields.values(), pid))
                elif method == "POST" and sub == "/peers":
                    con.execute("INSERT INTO peers (person_id, server, peer_name) VALUES (?, ?, ?)",
                                (pid, *clean_peer(body)))
                else:
                    raise LookupError("not found")
            return app.snapshot()

        m = re.fullmatch(r"/api/peers/(\d+)", path)
        if method == "DELETE" and m:
            with db() as con:
                con.execute("DELETE FROM peers WHERE id = ?", (int(m.group(1)),))
            return app.snapshot()
        raise LookupError("not found")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "wg"))
    ap.add_argument("--regions", nargs="+", default=["ap-southeast-5"])
    ap.add_argument("--refresh-minutes", type=int, default=15)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    ensure_keys()
    with db() as con:
        con.executescript(SCHEMA)
        con.execute("UPDATE people SET stage = 'trial' WHERE stage = 'tester'")  # pre-trial naming
    lock_down(DB_PATH)

    app = Monitor(Aws(args.profile), args.regions)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.app, httpd.port, httpd.token = app, args.port, secrets.token_urlsafe(24)
    threading.Thread(target=app.loop, args=(args.refresh_minutes * 60,), daemon=True).start()

    url = f"http://127.0.0.1:{args.port}/#token={httpd.token}"
    print(f"wg-monitor on {url}\nData: {DATA}\nCtrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
