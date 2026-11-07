"""Best-effort aggregate accounting. No disk I/O or awaits in the forwarding path."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import time
import uuid

TZ = timezone(timedelta(hours=8))
MAX_ROWS = 50_000
MAX_BYTES = 32 * 1024 * 1024
MAX_DIRTY = 4096


class Traffic:
    def __init__(self, directory, clock=time.time):
        self.path = directory / 'traffic.db'
        self.clock = clock
        self.pending = {}
        self.batch = None
        self.lock = asyncio.Lock()
        self.task = None
        self.stopping = asyncio.Event()
        self.error = None
        self.gap = False
        self.started = clock()
        self.saved = None
        self.restored = None
        self.meta_dirty = False
        self.day_start = self.day_end = 0
        self.periods()
        try:
            with self.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS totals(owner TEXT,node TEXT,origin TEXT,period TEXT,up INTEGER,down INTEGER,label TEXT,PRIMARY KEY(owner,node,origin,period))')
                db.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT)')
                meta = dict(db.execute('SELECT key,value FROM metadata'))
                self.started = float(meta.get('started', self.started))
                self.saved = float(meta['saved']) if 'saved' in meta else None
                self.restored = float(meta['restored']) if 'restored' in meta else None
                self.gap = meta.get('clean') == 'false' or meta.get('gap') == 'true'
                self.meta(db, 'started', self.started)
                self.meta(db, 'clean', False)
                self.meta(db, 'gap', self.gap)
            os.chmod(self.path, 0o600)
        except Exception:
            self.fail('流量统计暂不可写，记录可能不完整')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=1)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def meta(db, key, value):
        db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, json.dumps(value)))

    def fail(self, message):
        self.error = message
        self.gap = True

    def periods(self):
        now = self.clock()
        if not self.day_start <= now < self.day_end:
            day = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            self.day_start, self.day_end = day.timestamp(), (day + timedelta(days=1)).timestamp()
            self.day, self.month = day.strftime('d:%Y-%m-%d'), day.strftime('m:%Y-%m')
        return ('all', self.day, self.month)

    def context(self, state, group, node, origin):
        from scheduler.policy import node_ref, source_for
        source = source_for(state, node)
        return (group['uid'] if group else '', node_ref(state, node), origin,
                json.dumps({'group': group['name'] if group else '单独节点测试', 'node': node['name'], 'source': source['name']}, ensure_ascii=False))

    def add(self, context, upload=0, download=0):
        if not upload and not download:
            return
        owner, node, origin, label = context
        periods = self.periods()
        keys = [(owner, node, origin, p) for p in periods]
        inflight = self.batch[1] if self.batch else {}
        if len(self.pending) + len(inflight) + sum(k not in self.pending for k in keys) > MAX_DIRTY:
            self.fail('流量统计内存上限已到，部分用量未记录；代理继续运行')
            return
        for key in keys:
            row = self.pending.setdefault(key, [0, 0, label])
            row[0] += upload
            row[1] += download

    def write(self, batch_id, rows, now, gap, restored):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            applied = db.execute("SELECT value FROM metadata WHERE key='batch'").fetchone()
            if not applied or json.loads(applied[0]) != batch_id:
                db.execute("DELETE FROM totals WHERE period NOT IN ('all',?,?)", (self.day, self.month))
                db.executemany('INSERT INTO totals VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner,node,origin,period) DO UPDATE SET up=up+excluded.up,down=down+excluded.down,label=excluded.label', [(*k,*v) for k,v in rows.items()])
                count, size = db.execute('SELECT count(*),coalesce(sum(length(CAST(label AS BLOB))+length(owner)+length(node)+length(origin)+length(period)+16),0) FROM totals').fetchone()
                if count > MAX_ROWS or size > MAX_BYTES:
                    raise ValueError('流量统计容量上限已到；已有累计保留，新增记录暂未保存')
                self.meta(db, 'batch', batch_id)
                self.meta(db, 'saved', now)
            self.meta(db, 'gap', gap)
            if restored is not None:
                self.meta(db, 'restored', restored)
        return now

    async def flush(self):
        async with self.lock:
            if not self.batch and (self.pending or self.meta_dirty):
                self.batch = (uuid.uuid4().hex, self.pending)
                self.pending = {}
            if not self.batch:
                return
            batch_id, rows = self.batch
            now = self.clock()
            restored = self.restored
            try:
                self.saved = await asyncio.to_thread(self.write, batch_id, rows, now, self.gap, restored)
                self.batch = None
                self.meta_dirty = self.restored != restored
                self.error = None
            except Exception:
                self.fail('流量统计保存失败或达到容量上限；记录可能缺失，代理继续运行')

    async def run(self):
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), 60)
            except asyncio.TimeoutError:
                await self.flush()

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def query(self, owner=None, node=None, offset=0):
        async with self.lock:
            # One snapshot watermark: forwarding keeps adding without waiting for disk/query.
            extra = {k:list(v) for k,v in self.pending.items()}
            batch = self.batch
            periods = self.periods()
            def read():
                with self.connect() as db:
                    meta = dict(db.execute('SELECT key,value FROM metadata'))
                    where = ' WHERE owner=?' if owner is not None else ' WHERE node=?' if node else ''
                    args = (owner,) if owner is not None else (node,) if node else ()
                    rows = list(db.execute('SELECT owner,node,origin,period,up,down,label FROM totals'+where,args))
                # An ambiguous commit may already contain the in-flight batch.
                if batch and json.loads(meta.get('batch','null')) != batch[0]:
                    for k,v in batch[1].items():
                        r = extra.setdefault(k,[0,0,v[2]]);r[0]+=v[0];r[1]+=v[1]
                merged = {tuple(r[:4]):list(r[4:]) for r in rows}
                for k,v in extra.items():
                    if (owner is not None and k[0]!=owner) or (node and k[1]!=node):continue
                    r=merged.setdefault(k,[0,0,v[2]]);r[0]+=v[0];r[1]+=v[1]
                result = [{'owner':k[0],'node_ref':k[1],'origin':k[2],'period':k[3], 'upload':v[0],'download':v[1],'total':v[0]+v[1], **json.loads(v[2])} for k,v in merged.items() if k[3] in periods]
                summary = {p:{'upload':sum(r['upload'] for r in result if r['period']==p),'download':sum(r['download'] for r in result if r['period']==p),'total':sum(r['total'] for r in result if r['period']==p)} for p in periods}
                result.sort(key=lambda r:(r['owner'],r['node_ref'],r['origin'],r['period']))
                return result[offset:offset+100], len(result), summary
            try:
                rows,count,summary = await asyncio.to_thread(read)
            except Exception:
                self.fail('流量统计暂不可读；不代表用量为零')
                rows,count,summary = [],0,{}
            return {'rows':rows,'row_count':count,'offset':offset,'summary':summary,'started':self.started,'saved':self.saved,'restored':self.restored,'gap':self.gap,'error':self.error,'day':periods[1],'month':periods[2],'timezone':'Asia/Shanghai'}

    async def close(self):
        self.stopping.set()
        if self.task:
            await self.task
        await self.flush()
        if self.batch is None and (self.pending or self.meta_dirty):
            await self.flush()
        try:
            def finish():
                with self.connect() as db:
                    self.meta(db,'clean', self.batch is None and not self.pending)
                    self.meta(db,'gap',self.gap)
                    if self.restored is not None:self.meta(db,'restored',self.restored)
            await asyncio.to_thread(finish)
        except Exception:
            self.fail('流量统计关闭保存失败')


@asynccontextmanager
async def metered_probe(upstream, traffic, context):
    """Loopback-only accounting relay for existing probes. Never grant business leases."""
    if traffic is None:
        yield upstream
        return
    from gateway.server import Gateway
    gateway = Gateway('127.0.0.1')
    gateway.traffic = traffic
    gateway.pulse(86400)
    try:
        await gateway.listen('probe', 0)
        gateway.grant('probe', upstream, gateway.clock()+86400, context)
    except Exception:
        traffic.fail('探测计量通道不可用；原检测继续，部分用量未记录')
        await gateway.close()
        yield upstream
        return
    try:
        yield ('127.0.0.1',gateway.servers['probe'].sockets[0].getsockname()[1])
    finally:
        await gateway.close()


def mark_restore(directory):
    """Offline configuration restore never overwrites the local accounting database."""
    path = directory / 'traffic.db'
    if path.exists():
        db = sqlite3.connect(path, timeout=1)
        try:
            with db:
                Traffic.meta(db, 'restored', time.time())
        finally:
            db.close()
