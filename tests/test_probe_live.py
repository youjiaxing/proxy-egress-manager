"""Exercise HTTP identity, metadata and STUN through an actual SOCKS server."""
import asyncio
import json
import socket
import struct

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from gateway.socks import read_address, pack_address, unpack_datagram
from scheduler.probe import Prober, COOKIE
from scheduler.store import DEFAULT_SETTINGS


class Stun(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport=transport
    def datagram_received(self,data,address):
        if len(data)!=20:
            return
        transaction=data[8:20]
        ip=bytes(x^y for x,y in zip(bytes([8,8,4,4]),struct.pack('!I',COOKIE)))
        attribute=b'\x00\x01'+struct.pack('!H',12345^(COOKIE>>16))+ip
        body=struct.pack('!HH',0x20,len(attribute))+attribute
        self.transport.sendto(struct.pack('!HHI',0x101,len(body),COOKIE)+transaction+body,address)


class UDPForward(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport=None
        self.client=None
        self.targets={}
    def connection_made(self,transport):
        self.transport=transport
    def datagram_received(self,data,address):
        if address in self.targets:
            self.transport.sendto(b'\x00\x00\x00'+pack_address(*address)+data,self.targets[address])
        else:
            try:
                host,port,payload=unpack_datagram(data)
                target=(host,port)
                self.targets[target]=address
                self.transport.sendto(payload,target)
            except ValueError:
                pass


@pytest.mark.asyncio
async def test_real_tcp_and_udp_observations_are_separate_and_all_tcp_targets_required(monkeypatch):
    async def check(request): return web.Response(status=204)
    identity_value={'ip':'8.8.8.8'}
    async def identity(request): return web.json_response(identity_value)
    async def country(request): return web.json_response({'country_code':'US'})
    app=web.Application()
    app.router.add_get('/check',check)
    app.router.add_get('/identity',identity)
    app.router.add_get('/country/{ip}',country)
    http=TestServer(app)
    await http.start_server()
    tasks=set()
    async def socks(reader,writer):
        task=asyncio.current_task(); tasks.add(task)
        transport=other=None
        try:
            header=await reader.readexactly(2)
            await reader.readexactly(header[1])
            writer.write(b'\x05\x00'); await writer.drain()
            request=await reader.readexactly(3)
            host,port=await read_address(reader)
            if request[1]==3:
                transport,_=await asyncio.get_running_loop().create_datagram_endpoint(UDPForward,local_addr=('127.0.0.1',0))
                writer.write(b'\x05\x00\x00'+pack_address(*transport.get_extra_info('sockname')))
                await writer.drain(); await reader.read()
            else:
                remote,other=await asyncio.open_connection(host,port)
                writer.write(b'\x05\x00\x00'+pack_address('127.0.0.1',0)); await writer.drain()
                async def pipe(r,w):
                    while True:
                        data=await r.read(65536)
                        if not data: return
                        w.write(data); await w.drain()
                pumps=[asyncio.create_task(pipe(reader,other)),asyncio.create_task(pipe(remote,writer))]
                try:
                    await asyncio.wait(pumps,return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for p in pumps:p.cancel()
                    await asyncio.gather(*pumps,return_exceptions=True)
        except (OSError,asyncio.IncompleteReadError): pass
        finally:
            if transport: transport.close()
            if other: other.close()
            writer.close(); tasks.discard(task)
    server=await asyncio.start_server(socks,'127.0.0.1',0)
    stun,_=await asyncio.get_running_loop().create_datagram_endpoint(Stun,local_addr=('127.0.0.1',0))
    upstream=server.sockets[0].getsockname()[:2]
    settings={**DEFAULT_SETTINGS,'probe_timeout':.2,
              'tcp_check_urls':[str(http.make_url('/check'))],
              'tcp_identity_urls':[str(http.make_url('/identity'))],
              'stun_servers':['127.0.0.1:%s'%stun.get_extra_info('sockname')[1]],
              'country_url':str(http.make_url('/country/'))+'{ip}'}
    try:
        result=await Prober().observe(upstream,settings)
        assert result.us
        assert result.tcp_ips=={'8.8.8.8'}
        assert result.udp_ips=={'8.8.4.4'}
        assert result.tcp_ok and result.udp_ok
        from scheduler import probe
        real_stun = probe.stun_query
        async def forbidden_udp(*args):
            raise AssertionError('TCP-only round must not call STUN')
        monkeypatch.setattr(probe, 'stun_query', forbidden_udp)
        result = await Prober().observe(upstream, {**settings, 'stun_servers': []})
        assert result.us and not result.udp_checked and not result.udp_ips
        assert not any(s['method']=='stun-with-retry' for s in result.samples)
        monkeypatch.setattr(probe, 'stun_query', real_stun)
        # Optional UDP failure must neither fail TCP admission nor trigger failure controls.
        from scheduler import probe
        real_stun = probe.stun_query
        async def no_udp(server, timeout, upstream=None):
            assert upstream is not None, 'UDP must not participate in failure attribution'
            raise TimeoutError('UDP unavailable')
        monkeypatch.setattr(probe, 'stun_query', no_udp)
        result = await Prober().observe(upstream, settings)
        assert result.us and result.complete and result.tcp_ok and not result.udp_ok
        assert not result.failure_confirmed
        assert result.identity_errors == settings['stun_servers']
        # A hung optional measurement must not erase a completed TCP identity.
        async def hung_udp(*args):
            await asyncio.Event().wait()
        monkeypatch.setattr(probe, 'stun_query', hung_udp)
        result = await asyncio.wait_for(Prober().observe(upstream, {**settings, 'health_grace': 1}), 1.5)
        assert result.us and not result.udp_ok
        result = await asyncio.wait_for(Prober().observe(upstream, {**settings, 'identity_ttl': 1}), .9)
        assert result.us and not result.udp_ok
        monkeypatch.setattr(probe, 'stun_query', no_udp)
        settings['tcp_identity_urls'].append(str(http.make_url('/missing')))
        result = await Prober().observe(upstream, settings)
        assert not result.us
        assert settings['stun_servers'][0] in result.identity_errors
        assert settings['stun_servers'][0] not in result.reason
        settings['tcp_identity_urls'].pop()
        monkeypatch.setattr(probe, 'stun_query', real_stun)
        # A required identity target fails; the other protocol cannot stand in for it.
        settings['tcp_identity_urls'].append(str(http.make_url('/missing')))
        result=await Prober().observe(upstream,settings)
        assert not result.complete and not result.us
        assert result.tcp_ok and result.udp_ok
        assert not result.failure_confirmed
        assert result.identity_errors == [str(http.make_url('/missing'))]
        assert '部分出口验证目标' in result.reason
        settings['tcp_identity_urls']=settings['tcp_identity_urls'][:1]
        identity_value['ip']='127.0.0.1'
        result=await Prober().observe(upstream,settings)
        assert result.countries['127.0.0.1']=='INVALID'
        assert not result.us
    finally:
        stun.close(); server.close(); await server.wait_closed()
        for task in list(tasks): task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await http.close()
