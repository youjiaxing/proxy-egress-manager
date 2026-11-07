import asyncio
import copy
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from gateway.server import Gateway
from gateway.socks import open_socks, pack_address, unpack_datagram
from scheduler.probe import DatagramReply
from scheduler.traffic import Traffic, metered_probe

CTX=('group-uid','source-uid/node-fingerprint','business',json.dumps({'group':'组','node':'节点','source':'来源'}))


@pytest.mark.asyncio
async def test_tcp_udp_and_probe_payload_counts(backend,tmp_path):
    t=Traffic(tmp_path)
    g=Gateway('127.0.0.1');g.traffic=t;g.pulse(60)
    await g.listen('1',0);g.grant('1',backend,g.clock()+60,CTX)
    port=g.servers['1'].sockets[0].getsockname()[1]
    try:
        r,w,_=await open_socks('127.0.0.1',port,1,'example.com',443)
        w.write(b'hello');await w.drain();assert await r.readexactly(5)==b'hello';w.close()
        _,control,relay=await open_socks('127.0.0.1',port,3,'0.0.0.0',0)
        tr,p=await asyncio.get_running_loop().create_datagram_endpoint(DatagramReply,remote_addr=relay)
        tr.sendto(b'\0\0\0'+pack_address('example.com',53)+b'udp')
        assert unpack_datagram(await asyncio.wait_for(p.queue.get(),1))[2]==b'udp'
        tr.close();control.close()
        probe=(*CTX[:2],'manual',CTX[3])
        async with metered_probe(backend,t,probe) as route:
            r,w,_=await open_socks(*route,1,'example.com',443)
            w.write(b'probe');await w.drain();assert await r.readexactly(5)==b'probe';w.close()
        q=await t.query()
        business=next(x for x in q['rows'] if x['origin']=='business' and x['period']=='all')
        manual=next(x for x in q['rows'] if x['origin']=='manual' and x['period']=='all')
        assert (business['upload'],business['download'])==(8,8)
        assert (manual['upload'],manual['download'])==(5,5)
        await t.flush();assert (await t.query())['rows']==q['rows']
    finally:await g.close();await t.close()


@pytest.mark.asyncio
async def test_periods_restart_and_frozen_identities(tmp_path):
    now=[datetime(2026,11,30,15,59,59,tzinfo=timezone.utc).timestamp()]
    t=Traffic(tmp_path,clock=lambda:now[0]);t.add(CTX,10,20)
    await t.flush();now[0]+=2
    new=('new-group','new-source/same-node','business',CTX[3]);t.add(new,3,4)
    q=await t.query();assert q['day']=='d:2026-12-01' and q['month']=='m:2026-12'
    assert sum(r['upload'] for r in q['rows'] if r['period']=='all')==13
    assert sum(r['upload'] for r in q['rows'] if r['period']==q['day'])==3
    await t.close();reopened=Traffic(tmp_path,clock=lambda:now[0])
    assert not reopened.gap
    assert next(r for r in (await reopened.query(CTX[0]))['rows'] if r['period']=='all')['upload']==10
    await reopened.close()


@pytest.mark.asyncio
async def test_ambiguous_commit_retry_and_read_do_not_double_count(tmp_path,monkeypatch):
    t=Traffic(tmp_path);t.add(CTX,100,50);write=t.write
    def commit_then_fail(*args):write(*args);raise OSError('ambiguous acknowledgement')
    monkeypatch.setattr(t,'write',commit_then_fail)
    await t.flush();assert t.batch and t.gap
    t.add(CTX,20,30)
    q=await t.query();row=next(r for r in q['rows'] if r['period']=='all');assert (row['upload'],row['download'])==(120,80)
    monkeypatch.setattr(t,'write',write)
    await t.flush();await t.flush()
    q=await t.query();row=next(r for r in q['rows'] if r['period']=='all');assert row['total']==200 and q['gap']
    await t.close()


@pytest.mark.asyncio
async def test_bounded_failure_keeps_old_totals_and_no_idle_writes(tmp_path,monkeypatch):
    t=Traffic(tmp_path);t.add(CTX,1,2);await t.flush()
    monkeypatch.setattr('scheduler.traffic.MAX_ROWS',3)
    other=('other',*CTX[1:]);t.add(other,5,6);await t.flush();assert t.error and t.batch
    with sqlite3.connect(t.path) as db:assert db.execute("select up from totals where owner=? and period='all'",(CTX[0],)).fetchone()[0]==1
    monkeypatch.setattr('scheduler.traffic.MAX_DIRTY',3)
    t.add(('third',*CTX[1:]),9,9);assert not t.pending
    await t.close()
    t=Traffic(tmp_path);before=t.path.stat().st_mtime_ns;await t.flush();assert t.path.stat().st_mtime_ns==before
    await t.close()


@pytest.mark.asyncio
async def test_concurrent_query_has_no_double_count_or_lost_pending(tmp_path,monkeypatch):
    import threading
    t=Traffic(tmp_path);t.add(CTX,10,0)
    entered=threading.Event();release=threading.Event();write=t.write
    def blocked(*args):entered.set();release.wait(2);return write(*args)
    monkeypatch.setattr(t,'write',blocked)
    flush=asyncio.create_task(t.flush());await asyncio.to_thread(entered.wait,1)
    t.add(CTX,7,0);query=asyncio.create_task(t.query());release.set();await flush
    assert next(r for r in (await query)['rows'] if r['period']=='all')['upload']==17
    await t.close()


@pytest.mark.asyncio
async def test_lease_switch_freezes_old_node_and_revokes(tmp_path,backend):
    t=Traffic(tmp_path);g=Gateway('127.0.0.1');g.traffic=t;g.pulse(60)
    await g.listen('1',0);g.grant('1',backend,g.clock()+60,CTX)
    port=g.servers['1'].sockets[0].getsockname()[1]
    r,w,_=await open_socks('127.0.0.1',port,1,'example.com',443)
    w.write(b'old');await w.drain();assert await r.readexactly(3)==b'old'
    await g.revoke('1');assert await r.read()==b'';w.close()
    second=(CTX[0],'new-source/new-node',CTX[2],CTX[3]);g.grant('1',backend,g.clock()+60,second)
    r,w,_=await open_socks('127.0.0.1',port,1,'example.com',443)
    w.write(b'newer');await w.drain();assert await r.readexactly(5)==b'newer';w.close()
    rows=(await t.query())['rows']
    assert next(r for r in rows if r['node_ref']==CTX[1] and r['period']=='all')['total']==6
    assert next(r for r in rows if r['node_ref']==second[1] and r['period']=='all')['total']==10
    await g.close();await t.close()


@pytest.mark.asyncio
async def test_restore_preserves_accounting_and_api_reads_without_probes(tmp_path):
    from aiohttp import CookieJar
    from aiohttp.test_utils import TestClient,TestServer
    from scheduler.store import Store
    from scheduler.controller import Controller
    from web.server import Management
    from test_controller import FakeCore,FakeProber
    t=Traffic(tmp_path);t.add(CTX,11,22);await t.flush()
    store=Store(tmp_path);core=FakeCore();core.directory=tmp_path/'core'
    c=Controller(store,core,Gateway('127.0.0.1'),FakeProber(core));c.traffic=t
    for g in c.state['groups']:g['paused']=True
    async def listen(group,port):
        from gateway.server import Lease
        c.gateway.leases.setdefault(group,Lease())
    c.gateway.listen=listen
    for g in c.state['groups']:await listen(g['id'],g['port'])
    manager=Management(c,'local-fixture-password-long','http://localhost',secure=False)
    client=TestClient(TestServer(manager.app),cookie_jar=CookieJar(unsafe=True));await client.start_server()
    try:
        assert (await client.get('/api/traffic')).status==401
        res=await client.post('/api/login',json={'password':'local-fixture-password-long'},headers={'Origin':'http://localhost'})
        csrf=(await res.json())['csrf'];headers={'Origin':'http://localhost','X-CSRF-Token':csrf}
        q=await (await client.get('/api/traffic?group='+CTX[0])).json()
        assert q['summary']['all']['total']==33
        assert (await client.get('/api/traffic?offset=-1')).status==400
        backup=store.backup();backup['providers']={}
        assert 'traffic' not in backup
        response=await client.post('/api/restore',json=backup,headers=headers);job=(await response.json())['job']
        await asyncio.gather(*list(manager.job_tasks));assert manager.jobs[job]['status']=='done'
        assert (await t.query())['summary']['all']['total']==33 and t.restored
        await t.flush()
    finally:await client.close();await c.gateway.close();await t.close();store.close()
    from scheduler.traffic import mark_restore
    mark_restore(tmp_path)
    reopened=Traffic(tmp_path);assert reopened.restored and (await reopened.query())['summary']['all']['total']==33
    await reopened.close()


@pytest.mark.asyncio
async def test_pagination_retains_complete_summary(tmp_path):
    t=Traffic(tmp_path)
    for i in range(50):t.add((str(i),*CTX[1:]),2,3)
    q=await t.query();assert len(q['rows'])==100 and q['row_count']==150 and q['summary']['all']['total']==250
    assert len((await t.query(offset=100))['rows'])==50
    await t.close()


@pytest.mark.asyncio
async def test_shutdown_waits_for_writer_and_saves_new_pending(tmp_path,monkeypatch):
    import threading
    t=Traffic(tmp_path);t.add(CTX,10,0)
    entered=threading.Event();release=threading.Event();write=t.write
    def blocked(*args):entered.set();release.wait(2);return write(*args)
    monkeypatch.setattr(t,'write',blocked)
    flush=asyncio.create_task(t.flush());await asyncio.to_thread(entered.wait,1)
    t.add(CTX,7,0);t.restored=123;t.meta_dirty=True
    close=asyncio.create_task(t.close());release.set();await flush;await close
    reopened=Traffic(tmp_path)
    q=await reopened.query();assert not q['gap'] and q['restored']==123 and q['summary']['all']['upload']==17
    await reopened.close()


@pytest.mark.asyncio
async def test_meter_setup_failure_preserves_original_probe_route(tmp_path,monkeypatch):
    t=Traffic(tmp_path)
    async def fail(*args):raise OSError('fixture bind failure')
    monkeypatch.setattr(Gateway,'listen',fail)
    async with metered_probe(('original-core',20000),t,CTX) as route:
        assert route==('original-core',20000)
    assert t.gap and '原检测继续' in t.error
    await t.close()


@pytest.mark.asyncio
async def test_persistence_failure_does_not_interrupt_live_business(tmp_path,backend,monkeypatch):
    t=Traffic(tmp_path);g=Gateway('127.0.0.1');g.traffic=t;g.pulse(60)
    await g.listen('1',0);g.grant('1',backend,g.clock()+60,CTX)
    r,w,_=await open_socks('127.0.0.1',g.servers['1'].sockets[0].getsockname()[1],1,'example.com',443)
    try:
        w.write(b'before');await w.drain();assert await r.readexactly(6)==b'before'
        def fail(*args):raise OSError('fixture disk full')
        monkeypatch.setattr(t,'write',fail);await t.flush()
        assert t.gap and t.error and g.allowed('1',g.leases['1'].generation)
        w.write(b'after');await w.drain();assert await r.readexactly(5)==b'after'
    finally:w.close();await g.close();await t.close()
