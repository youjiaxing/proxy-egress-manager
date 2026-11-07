"""Bounded, best-effort observation history, outside the control-state database."""
import asyncio
import json
import os
import sqlite3
import time

MAX_ROWS = 50_000
MAX_BYTES = 32 * 1024 * 1024
DAYS = 7


class History:
    def __init__(self, directory):
        self.path = directory / 'quality.db'
        self.queue = asyncio.Queue(maxsize=1024)
        self.task = None
        self.error = None
        self.latest = {}
        try:
            with sqlite3.connect(self.path) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, time REAL NOT NULL, owner TEXT NOT NULL, node TEXT, origin TEXT NOT NULL, body TEXT NOT NULL)')
                db.execute('CREATE INDEX IF NOT EXISTS owner_time ON samples(owner,time)')
                db.execute('CREATE INDEX IF NOT EXISTS node_time ON samples(node,time)')
            os.chmod(self.path, 0o600)
            with sqlite3.connect(self.path) as db:
                for t,n,b in db.execute("SELECT time,node,body FROM samples WHERE origin='manual' ORDER BY id DESC LIMIT 1000"):
                    if n not in self.latest:
                        self.latest[n] = dict(json.loads(b),time=t)
        except Exception:
            self.error = '质量历史暂不可写，不影响代理验证'

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    def record(self, owner, node, origin, data):
        try:
            if origin == "manual" and node:
                self.latest[node] = dict(data,time=time.time())
            body = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
            if len(body.encode()) > 16384:
                raise ValueError('oversized sample')
            self.queue.put_nowait((time.time(), owner, node, origin, body))
        except Exception:
            self.error = '质量历史记录出现缺失，请勿将空档视为正常'

    def write(self, batch):
        with sqlite3.connect(self.path, timeout=1) as db:
            db.executemany('INSERT INTO samples(time,owner,node,origin,body) VALUES(?,?,?,?,?)', batch)
            db.execute('DELETE FROM samples WHERE time<?', (time.time()-DAYS*86400,))
            db.execute('DELETE FROM samples WHERE id <= COALESCE((SELECT id FROM samples ORDER BY id DESC LIMIT 1 OFFSET ?),0)', (MAX_ROWS,))
            total = db.execute('SELECT COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM samples').fetchone()[0]
            while total > MAX_BYTES:
                rows = db.execute('SELECT id,length(CAST(body AS BLOB)) FROM samples ORDER BY id LIMIT 256').fetchall()
                db.execute('DELETE FROM samples WHERE id<=?', (rows[-1][0],))
                total -= sum(r[1] for r in rows)
            db.execute('PRAGMA wal_checkpoint(PASSIVE)') if not db.in_transaction else None
        # WAL/indices are additional to the documented record-content budget.
        with sqlite3.connect(self.path, timeout=1) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    async def run(self):
        while True:
            first = await self.queue.get()
            batch = [first]
            while not self.queue.empty() and len(batch) < 64:
                batch.append(self.queue.get_nowait())
            try:
                await asyncio.to_thread(self.write, batch)
            except Exception:
                self.error = '质量历史写入失败，本时段可能缺失'
            finally:
                for _ in batch:
                    self.queue.task_done()

    def query(self, owner, hours, node=None):
        cutoff = time.time()-hours*3600
        where, args = ('node=?', [node]) if node else ('owner=?', [owner])
        with sqlite3.connect('file:'+str(self.path)+'?mode=ro', uri=True, timeout=1) as db:
            rows = db.execute(f'SELECT time,origin,body FROM samples WHERE {where} AND time>=? ORDER BY id DESC LIMIT 50000', (*args, cutoff)).fetchall()
            first = db.execute('SELECT MIN(time) FROM samples').fetchone()[0]
            restores = db.execute("SELECT time,body FROM samples WHERE origin='restore' AND time>=? ORDER BY id DESC LIMIT 20",(cutoff,)).fetchall()
        records = [{'time':t,'origin':o,**json.loads(b)} for t,o,b in rows]
        # Each target/method/identity is a distinct series. Never connect across missing/failed samples.
        series = {}
        for r in reversed(records):
            for sample in r.get('samples', []):
                key = json.dumps([r.get('node_ref'),r.get('config'),r['origin'],sample['target'],sample['method']], separators=(',', ':'))
                group = series.setdefault(key, {'config':r.get('config'), 'target':sample['target'], 'method':sample['method'], 'node_ref':r.get('node_ref'), 'origin':r['origin'], 'attempts':0,'successes':0,'total_ms':0,'deltas':[],'previous':None,'last_time':None,'previous_segment':None,'points':[]})
                group['attempts'] += 1
                ms = sample.get('ms') if sample.get('ok') else None
                if ms is not None:
                    group['successes'] += 1
                    group['total_ms'] += ms
                    if group['previous'] is not None and r.get('segment')==group['previous_segment'] and r['time']-group['last_time'] <= 90:
                        group['deltas'].append(abs(ms-group['previous']))
                boundary = r.get('segment')!=group['previous_segment']
                group['previous'], group['last_time'], group['previous_segment'] = ms, r['time'],r.get('segment')
                group['points'].append({'time':r['time'],'ms':ms,'break_before':boundary})
        for g in series.values():
            g['mean_ms'] = round(g.pop('total_ms')/g['successes'],1) if g['successes'] else None
            ds = g.pop('deltas'); g['variation_ms'] = round(sum(ds)/len(ds),1) if ds else None
            g.pop('previous');g.pop('last_time');g.pop('previous_segment');g['points'] = g['points'][-300:]
        manual, udp = {}, {}
        for r in records:
            if r['origin']=='manual':
                manual.setdefault((r.get('node_ref'),r.get('test')),r)
                if r.get('test')=='udp' and r.get('sent'):
                    udp.setdefault(r.get('node_ref'),r)
        state_records = [r for r in records if r['origin']=='state']
        return {'manual_records':list(manual.values()),'udp_records':list(udp.values()),'state_records':state_records[:5000], 'state_truncated':len(state_records)>5000, 'records_truncated':len(records)>500, 'restores':[{'time':t,**json.loads(b)} for t,b in restores], 'records':records[:500], 'series':list(series.values()), 'coverage_start':first, 'hours':hours, 'error':self.error, 'limits':{'days':DAYS,'rows':MAX_ROWS,'content_bytes':MAX_BYTES}}

    def export_page(self, after):
        with sqlite3.connect('file:'+str(self.path)+'?mode=ro', uri=True, timeout=1) as db:
            return db.execute('SELECT id,time,owner,node,origin,body FROM samples WHERE id>? ORDER BY id LIMIT 100', (after,)).fetchall()

    async def close(self):
        if self.task:
            try:
                await asyncio.wait_for(self.queue.join(), 3)
            except asyncio.TimeoutError:
                pass
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
