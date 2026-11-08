import json
import os
import sqlite3
import time
from pathlib import Path
from scheduler.policy import normalize, identity, validate_v2

STATE_FILE = "state-v2.db"

def _sync(path):
    fd = os.open(str(path), os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _stamp(path, body):
    from scheduler.core import atomic_write
    atomic_write(path, json.dumps(body))


def prepare_storage(directory):
    """Crash-safe one-way migration, under the unchanged controller.lock."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    old, archive, current = directory/'state.db', directory/'state-v1-migration.db', directory/STATE_FILE
    ready, marker = directory/'state-v2.ready', directory/'state-v2.migration.json'
    body = None
    if old.exists():
        with sqlite3.connect(old) as db:
            row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
            body = json.loads(row[0]) if row else {}
    tombstone = body is not None and body.get('schema') == 'migrated-use-v2'
    if ready.exists():
        if not current.exists() or not tombstone:
            raise ValueError('新版运行库缺失或旧版状态被替换，拒绝自动重建；请使用一致备份恢复')
        archive.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        _sync(directory)
        return current
    if not marker.exists():
        if tombstone or current.exists() or archive.exists():
            raise ValueError('迁移状态来源不明确，拒绝自动加载旧数据')
        _stamp(marker, {'origin':'legacy' if old.exists() else 'fresh'})
    origin = json.loads(marker.read_text())['origin']
    if origin == 'legacy' and not archive.exists():
        if tombstone or not old.exists():
            raise ValueError('迁移备份缺失，保持关闭')
        source = sqlite3.connect(old)
        temp = directory/'state-migration-copy.tmp'
        dest = sqlite3.connect(temp)
        try: source.backup(dest)
        finally: source.close();dest.close()
        os.chmod(temp,0o600);_sync(temp)
        os.replace(temp,archive);_sync(directory)
    if origin == 'legacy':
        # Make a checkpointed, durable tombstone BEFORE publishing v2 data.
        with sqlite3.connect(old) as db:
            db.execute("INSERT INTO state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (json.dumps({'schema':'migrated-use-v2'}),))
            db.commit()
            if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                raise ValueError('旧状态检查点繁忙，迁移尚未提交')
        _sync(old);_sync(directory)
    if not current.exists():
        temp = directory/'state-v2-publish.tmp'
        temp.unlink(missing_ok=True)
        dest = sqlite3.connect(temp)
        try:
            if origin == 'legacy':
                source = sqlite3.connect(archive)
                try: source.backup(dest)
                finally: source.close()
                state = json.loads(dest.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
            else:
                dest.execute('CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL)')
                dest.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,time REAL,kind TEXT,group_id TEXT,message TEXT)')
                state = {'schema':1,'settings':dict(DEFAULT_SETTINGS),'sources':[],'nodes':{},'groups':[new_group(1),new_group(2)],'revision':0}
            normalize(state);validate_v2(state)
            with dest:
                dest.execute('INSERT INTO state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',(json.dumps(state,ensure_ascii=False),))
        finally: dest.close()
        os.chmod(temp,0o600);_sync(temp);os.replace(temp,current);_sync(directory)
    if origin == 'fresh':
        with sqlite3.connect(old) as db:
            db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL)')
            db.execute('INSERT INTO state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',(json.dumps({'schema':'migrated-use-v2'}),))
        _sync(old);_sync(directory)
    os.chmod(old,0o600)
    _stamp(ready, {'format':2})
    marker.unlink(missing_ok=True);_sync(directory)
    # A successful publication must never replay the legacy archive, even if v2 is lost.
    archive.unlink(missing_ok=True);_sync(directory)
    return current

DEFAULT_SETTINGS = {
    "udp_check_interval": 1800,
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
            "status": "paused", "reason": "等待 TCP 验证", "tcp_ips": [], "udp_ips": [],
            "verified_at": None, "since": None, "failure_since": None, "uid": identity(), "scope": {"mode":"all", "ids":[]}, "retry_step":0, "next_retry_at":0}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = prepare_storage(self.directory)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, kind TEXT, group_id TEXT, message TEXT)")
        row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else {
            "schema": 1, "settings": dict(DEFAULT_SETTINGS), "sources": [], "nodes": {},
            "groups": [new_group(1), new_group(2)], "revision": 0,
        }
        normalize(self.state)
        validate_v2(self.state)
        self.state.setdefault("quality_epoch", identity())
        self.state["settings"] = {**DEFAULT_SETTINGS, **self.state["settings"]}
        # Disk identity is an observation, never a surviving data-plane authorization.
        for group in self.state["groups"]:
            group.update(status="paused", reason="启动后等待重验", failure_since=None)
            last=group.get('last_action')
            if last and last.get('outcome') in ('checking','saved_pending'):
                last.update(outcome='interrupted',message='服务重启，上次节点查找未完成；请查看当前状态后重新开始',finished=time.time())
        self.save()
        os.chmod(self.path, 0o600)

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
        return {"format": 2, "created_at": time.time(), "state": json.loads(row[0]), "events": events}

    def restore(self, state, events):
        state['settings'].setdefault('udp_check_interval', DEFAULT_SETTINGS['udp_check_interval'])
        normalize(state)
        validate_v2(state)
        state["revision"] += 1
        with self.db:
            self.db.execute("INSERT INTO state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                            (json.dumps(state, ensure_ascii=False),))
            self.db.execute("DELETE FROM events")
            self.db.executemany("INSERT INTO events(time,kind,group_id,message) VALUES(?,?,?,?)", events)
        self.state = state

    def close(self):
        self.db.close()
