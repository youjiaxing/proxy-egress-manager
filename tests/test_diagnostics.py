"""Loopback-only tests of diagnostic isolation and UDP accounting."""
import asyncio
import copy
import os
import socket
import struct
import subprocess

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer
from gateway.server import Gateway
from gateway.socks import open_socks, read_address, pack_address, unpack_datagram
from scheduler.core import Core, parse_subscription, atomic_write
from scheduler.controller import Controller
from scheduler.store import Store
from scheduler.policy import identity, node_ref
from scheduler.history import History
from scheduler.diagnostics import Diagnostics, udp_sample
from scheduler.probe import COOKIE
from test_controller import FakeCore, FakeProber


class FakeDiagnosticCore:
    def __init__(self,directory):self.changes=[];self.directory=directory

    async def ready(self):pass
    async def update_source(self,slot,payload):self.changes.append(payload)
    async def select(self,slot,node):return ('127.0.0.1',1)
    async def request(self,*args):pass


@pytest.mark.asyncio
async def test_failed_diagnostics_do_not_mutate_business_mapping_or_state(tmp_path):
    store=Store(tmp_path);core=FakeCore();core.directory=tmp_path/'core';(core.directory/'providers').mkdir(parents=True)
    nodes,payload=parse_subscription('proxies: [{name: US, type: socks5, server: 127.0.0.1, port: 1}]',1)
    store.state['sources']=[{'slot':1,'uid':identity(),'name':'local','url':'','enabled':True}]
    store.state['nodes']=nodes;(core.directory/'providers'/'s01.yaml').write_text(payload)
    c=Controller(store,core,Gateway('127.0.0.1'),FakeProber(core));history=History(tmp_path);history.start()
    d=Diagnostics(c,FakeDiagnosticCore(core.directory),history)
    before=copy.deepcopy(store.state);selected=copy.deepcopy(core.selected)
    ref=node_ref(c.state,next(iter(nodes.values())))
    result=await d.run({'nodes':[ref],'test':'http'})
    assert result[0]['error'] and result[0]['current']
    assert core.selected==selected and store.state==before
    assert not d.inflight
    await history.close();store.close()


@pytest.mark.asyncio
async def test_udp_counts_unique_transactions_not_duplicate_packets(monkeypatch):
    class LocalStun(asyncio.DatagramProtocol):
        def connection_made(self,t):self.t=t;self.count=0
        def datagram_received(self,packet,addr):
            self.count+=1
            host,port,data=unpack_datagram(packet)
            encoded=bytes(a^b for a,b in zip(bytes([8,8,8,8]),struct.pack('!I',COOKIE)))
            attr=b'\0\1'+struct.pack('!H',12345^(COOKIE>>16))+encoded
            body=struct.pack('!HH',0x20,8)+attr
            response=struct.pack('!HHI',0x101,len(body),COOKIE)+data[8:20]+body
            out=b'\0\0\0'+pack_address('127.0.0.1',3478)+response
            self.t.sendto(out,addr);self.t.sendto(out,addr)
    loop=asyncio.get_running_loop()
    transport,protocol=await loop.create_datagram_endpoint(LocalStun,local_addr=('127.0.0.1',0))
    class Control:
        def close(self):pass
    async def fake_open(*args,**kwargs):return None,Control(),transport.get_extra_info('sockname')[:2]
    monkeypatch.setattr('scheduler.diagnostics.open_socks',fake_open)
    try:
        result=await udp_sample(('127.0.0.1',1),'127.0.0.1:3478')
        assert result['sent']==result['received']==20 and protocol.count==20
        assert result['unanswered_pct']==0 and result['mean_ms'] is not None
    finally:transport.close()


@pytest.mark.asyncio
async def test_real_separate_core_test_does_not_switch_or_close_business(tmp_path):
    binary=os.environ.get('MIHOMO_BIN')
    if not binary:pytest.skip('MIHOMO_BIN not provided')
    for port in (20000,20001,20100,20101,29592,29593):
        with socket.socket() as s:s.bind(('127.0.0.1',port))
    tasks=set()
    async def socks(reader,writer):
        task=asyncio.current_task();tasks.add(task);remote_writer=None
        try:
            head=await reader.readexactly(2);await reader.readexactly(head[1]);writer.write(b'\5\0');await writer.drain()
            await reader.readexactly(3);host,port=await read_address(reader)
            remote,remote_writer=await asyncio.open_connection(host,port)
            writer.write(b'\5\0\0'+pack_address('127.0.0.1',1));await writer.drain()
            async def pipe(r,w):
                while chunk:=await r.read(65536):w.write(chunk);await w.drain()
            pumps=[asyncio.create_task(pipe(reader,remote_writer)),asyncio.create_task(pipe(remote,writer))]
            try:await asyncio.wait(pumps,return_when=asyncio.FIRST_COMPLETED)
            finally:
                for p in pumps:p.cancel()
                await asyncio.gather(*pumps,return_exceptions=True)
        except (OSError,asyncio.IncompleteReadError):pass
        finally:
            writer.close()
            if remote_writer:remote_writer.close()
            tasks.discard(task)
    async def echo(r,w):
        try:
            while chunk:=await r.read(100):w.write(chunk);await w.drain()
        finally:w.close()
    server=await asyncio.start_server(socks,'127.0.0.1',0)
    echo_server=await asyncio.start_server(echo,'127.0.0.1',0)
    async def check(request):return web.Response(status=204)
    app=web.Application();app.router.add_get('/check',check);http=TestServer(app);await http.start_server()
    cores=[];processes=[];writers=[];history=None;store=None
    try:
        for i in range(2):
            directory=tmp_path/str(i);directory.mkdir()
            core=Core(directory,base_url=f'http://127.0.0.1:{29592+i}',host='127.0.0.1',secret='local-fixture-only-secret-32-characters',capacity=1,source_capacity=1)
            core.initialize();cfg=yaml.safe_load((directory/'config.yaml').read_text());cfg['external-controller']=f'127.0.0.1:{29592+i}'
            for listener in cfg['listeners']:
                listener['listen']='127.0.0.1';listener['port']+=100*i
            atomic_write(directory/'config.yaml',yaml.safe_dump(cfg));cores.append(core)
            processes.append(subprocess.Popen([binary,'-d',str(directory)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
            for _ in range(100):
                try:await core.ready();break
                except Exception:await asyncio.sleep(.03)
            else:raise AssertionError('local core not ready')
        business,diag=cores
        original_select=diag.select
        async def diag_select(slot,node):await original_select(slot,node);return ('127.0.0.1',20100+slot)
        diag.select=diag_select
        store=Store(tmp_path/'state');nodes,payload=parse_subscription(yaml.safe_dump({'proxies':[{'name':'local','type':'socks5','server':'127.0.0.1','port':server.sockets[0].getsockname()[1]}]}),1)
        store.state['nodes']=nodes;store.state['sources']=[{'slot':1,'uid':identity(),'enabled':True,'name':'local','url':''}]
        store.state['settings']['tcp_check_urls']=[str(http.make_url('/check'))]
        n=next(iter(nodes));await business.update_source(1,payload);await business.select(1,n)
        store.state['groups'][0]['node']=n
        c=Controller(store,business,Gateway('127.0.0.1'));history=History(store.directory);history.start()
        d=Diagnostics(c,diag,history)
        r,w,_=await open_socks('127.0.0.1',20001,1,'127.0.0.1',echo_server.sockets[0].getsockname()[1]);writers.append(w)
        ref=node_ref(c.state,nodes[n]);before=copy.deepcopy(c.state)
        results=await d.run({'nodes':[ref],'test':'http','group':'1'})
        assert results[0]['ok'] and results[0]['samples'][0]['ms']>0
        assert c.state==before and await business.mapping_valid(1,n)
        w.write(b'after-diagnostic');await w.drain();assert await asyncio.wait_for(r.readexactly(16),2)==b'after-diagnostic'
    finally:
        for w in writers:w.close()
        if history:await history.close()
        if store:store.close()
        for core in cores:await core.close()
        for p in processes:
            p.terminate();await asyncio.to_thread(p.wait,5)
        server.close();echo_server.close();await server.wait_closed();await echo_server.wait_closed();await http.close()
        for task in list(tasks):task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_failure_never_restores_provider_credentials(tmp_path):
    core=FakeDiagnosticCore(tmp_path)
    (tmp_path/'providers').mkdir()
    (tmp_path/'providers'/'s01.yaml').write_text('private credentials')
    async def fail(*args):raise OSError('fixture reload failure')
    core.request=fail
    diagnostics=Diagnostics(None,core,None)
    with pytest.raises(ValueError,match='清理失败'):
        await diagnostics.clear()
    data=(tmp_path/'providers'/'s01.yaml').read_text()
    assert 'private credentials' not in data and 'empty-01' in data


@pytest.mark.asyncio
async def test_user_cancellation_reports_cancel_and_clears_provider(tmp_path):
    import json
    from types import SimpleNamespace
    from web.server import Management
    store=Store(tmp_path);core=FakeCore();core.directory=tmp_path/'core';(core.directory/'providers').mkdir(parents=True)
    nodes,payload=parse_subscription('proxies: [{name: local, type: socks5, server: 127.0.0.1, port: 1}]',1)
    store.state['sources']=[{'slot':1,'uid':identity(),'name':'local','url':'','enabled':True}]
    store.state['nodes']=nodes;(core.directory/'providers'/'s01.yaml').write_text(payload)
    controller=Controller(store,core,Gateway('127.0.0.1'),FakeProber(core))
    diagnostic_core=FakeDiagnosticCore(core.directory);started=asyncio.Event()
    async def waiting():started.set();await asyncio.Event().wait()
    diagnostic_core.ready=waiting
    h=History(tmp_path);h.start()
    diagnostics=Diagnostics(controller,diagnostic_core,h)
    manager=Management(controller,'local-fixture-password-long','http://localhost',secure=False)
    ref=node_ref(controller.state,next(iter(nodes.values())))
    response=manager.submit(diagnostics.run({'nodes':[ref],'test':'http'}))
    job=json.loads(response.text)['job'];manager.test_jobs={job}
    await started.wait()
    task=manager.job_by_id[job]
    await manager.cancel_job(SimpleNamespace(match_info={'job':job}))
    await asyncio.gather(task,return_exceptions=True)
    assert '用户已取消' in manager.jobs[job]['message'] and not diagnostics.inflight
    assert 'empty-01' in (core.directory/'providers'/'s01.yaml').read_text()
    await h.close();store.close()
