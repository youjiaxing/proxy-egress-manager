import json
import os
import sqlite3
import time
from pathlib import Path

DEFAULT_SETTINGS = {
    "check_interval": 30, "failure_seconds": 180, "health_grace": 60,
    "identity_ttl": 180, "probe_timeout": 8, "priority_country": "US",
    "tcp_identity_urls": ["https://api.ipify.org?format=json"],
    "tcp_check_urls": ["https://www.gstatic.com/generate_204", "https://cp.cloudflare.com/generate_204"],
    "stun_servers": ["stun.cloudflare.com:3478", "stun.l.google.com:19302"],
    "country_url": "https://ipwho.is/{ip}", "alert_url": "", "alert_enabled": False,
}


def new_group(slot, name=None):
    return {"id": str(slot), "name": name or "出口组 %s" % slot, "port": 1080 + slot,
            "node": None, "paused": False, "pinned": False, "pin_until": None,
            "status": "paused", "reason": "等待双协议验证", "tcp_ips": [], "udp_ips": [],
            "verified_at": None, "since": None, "failure_since": None}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.directory / "state.db"))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, kind TEXT, group_id TEXT, message TEXT)")
        row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else {
            "schema": 1, "settings": dict(DEFAULT_SETTINGS), "sources": [], "nodes": {},
            "groups": [new_group(1), new_group(2)], "revision": 0,
        }
        self.state["settings"] = {**DEFAULT_SETTINGS, **self.state["settings"]}
        # Disk identity is an observation, never a surviving data-plane authorization.
        for group in self.state["groups"]:
            group.update(status="paused", reason="启动后等待重验", failure_since=None)
        self.save()
        os.chmod(str(self.directory / "state.db"), 0o600)

    def save(self):
        self.state["revision"] += 1
        with self.db:
            self.db.execute("INSERT INTO state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                            (json.dumps(self.state, ensure_ascii=False),))

    def event(self, kind, message, group_id=None):
        with self.db:
            self.db.execute("INSERT INTO events(time,kind,group_id,message) VALUES(?,?,?,?)",
                            (time.time(), kind, group_id, message))
            self.db.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-5000 FROM events)")

    def events(self, limit=200):
        rows = self.db.execute("SELECT id,time,kind,group_id,message FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(zip(("id", "time", "kind", "group_id", "message"), row)) for row in rows]

    def backup(self):
        # All writers run on the same event loop; SQLite backup gives one committed snapshot.
        dest = sqlite3.connect(":memory:")
        self.db.backup(dest)
        row = dest.execute("SELECT body FROM state WHERE id=1").fetchone()
        events = list(dest.execute("SELECT time,kind,group_id,message FROM events"))
        dest.close()
        return {"format": 1, "created_at": time.time(), "state": json.loads(row[0]), "events": events}

    def restore(self, state, events):
        state["revision"] += 1
        with self.db:
            self.db.execute("INSERT INTO state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                            (json.dumps(state, ensure_ascii=False),))
            self.db.execute("DELETE FROM events")
            self.db.executemany("INSERT INTO events(time,kind,group_id,message) VALUES(?,?,?,?)", events)
        self.state = state

    def close(self):
        self.db.close()
