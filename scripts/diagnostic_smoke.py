"""Run via Compose exec stdin. Virtual loopback/container fixtures only, no subscriptions."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
import struct

from aiohttp import web
import yaml
from gateway.socks import read_address, pack_address, unpack_datagram
from gateway.server import Gateway
from scheduler.core import Core, parse_subscription
from scheduler.store import Store
from scheduler.controller import Controller
from scheduler.history import History
from scheduler.diagnostics import Diagnostics, diagnostic_core
from scheduler.policy import identity, node_ref
from scheduler.probe import COOKIE


class Stun(asyncio.DatagramProtocol):
    def connection_made(self,t): self.t=t
    def datagram_received(self,data,addr):
        if len(data)!=20:return
        encoded=bytes(a^b for a,b in zip(bytes([8,8,8,8]),struct.pack('!I',COOKIE)))
        body=struct.pack('!HH',0x20,8)+b'\0\1'+struct.pack('!H',12345^(COOKIE>>16))+encoded
        self.t.sendto(struct.pack('!HHI',0x101,len(body),COOKIE)+data[8:20]+body,addr)


class Forward(asyncio.DatagramProtocol):
    def connection_made(self,t):self.t=t;self.peers={}
    def datagram_received(self,data,addr):
        if addr in self.peers:
            self.t.sendto(b'\0\0\0'+pack_address(*addr)+data,self.peers[addr]);return
        try:
            host,port,payload=unpack_datagram(data)
            # Mihomo resolves the target before passing numeric UDP destinations.
            target=(host,port);self.peers[target]=addr;self.t.sendto(payload,target)
        except ValueError:pass


async def main():
    host='proxy-egress-manager'
    loop=asyncio.get_running_loop();connections=set()
    async def socks(r,w):
        task=asyncio.current_task();connections.add(task);other=transport=None
        try:
            h=await r.readexactly(2);await r.readexactly(h[1]);w.write(b'\5\0');await w.drain()
            req=await r.readexactly(3);target,port=await read_address(r)
            if req[1]==3:
                transport,_=await loop.create_datagram_endpoint(Forward,local_addr=('0.0.0.0',0))
                w.write(b'\5\0\0'+pack_address('0.0.0.0',transport.get_extra_info('sockname')[1]));await w.drain();await r.read()
            else:
                remote,other=await asyncio.open_connection(target,port);w.write(b'\5\0\0'+pack_address('127.0.0.1',1));await w.drain()
                async def pipe(reader,writer):
                    while b:=await reader.read(65536):writer.write(b);await writer.drain()
                ps=[asyncio.create_task(pipe(r,other)),asyncio.create_task(pipe(remote,w))]
                try:await asyncio.wait(ps,return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for p in ps:p.cancel()
                    await asyncio.gather(*ps,return_exceptions=True)
        except (OSError,asyncio.IncompleteReadError):pass
        finally:
            if other:other.close()
            if transport:transport.close()
            w.close();connections.discard(task)
    async def check(request):return web.Response(status=204)
    app=web.Application();app.router.add_get('/check',check)
    runner=web.AppRunner(app,access_log=None);await runner.setup();site=web.TCPSite(runner,'0.0.0.0',0);await site.start()
    server=await asyncio.start_server(socks,'0.0.0.0',0)
    stun,_=await loop.create_datagram_endpoint(Stun,local_addr=('0.0.0.0',0))
    try:
        with tempfile.TemporaryDirectory(prefix='pem-diagnostic-smoke-') as d:
            root=Path(d);business=Core(root/'business');business.initialize()
            store=Store(root/'state');history=History(store.directory);history.start()
            diag=diagnostic_core(os.environ['DIAG_CONFIG_DIR'],os.environ['CORE_SECRET'])
            try:
                nodes,payload=parse_subscription(yaml.safe_dump({'proxies':[{'name':'virtual-fixture','type':'socks5','server':host,'port':server.sockets[0].getsockname()[1],'udp':True}]}),1)
                (business.directory/'providers'/'s01.yaml').write_text(payload)
                store.state['sources']=[{'slot':1,'uid':identity(),'name':'fixture','url':'','enabled':True}];store.state['nodes']=nodes
                store.state['settings']['tcp_check_urls']=[f'http://{host}:{site._server.sockets[0].getsockname()[1]}/check']
                store.state['settings']['stun_servers']=[f'127.0.0.1:{stun.get_extra_info("sockname")[1]}']
                c=Controller(store,business,Gateway());tests=Diagnostics(c,diag,history);ref=node_ref(c.state,next(iter(nodes.values())))
                result=await tests.run({'nodes':[ref],'test':'http'});assert result[0].get('ok'), result
                result=await tests.run({'nodes':[ref],'test':'udp'});assert result[0].get('received')==20,result
                assert store.state['groups'][0]['node'] is None
                print('PASS isolated Docker diagnostic service: HTTP + 20/20 UDP samples; no real subscription or business state writes')
            finally:await diag.close();await business.close();await history.close();store.close()
    finally:
        server.close();await server.wait_closed();stun.close();await runner.cleanup()
        for task in list(connections):task.cancel()
        await asyncio.gather(*connections,return_exceptions=True)

asyncio.run(main())
