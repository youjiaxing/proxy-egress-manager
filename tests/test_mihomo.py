"""Real upstream-core integration. Set MIHOMO_BIN to the pinned executable."""
import asyncio
import os
import socket
import subprocess

import pytest
import yaml
from gateway.server import Gateway
from gateway.socks import open_socks, pack_address, unpack_datagram
from scheduler.core import Core, atomic_write, parse_subscription
from scheduler.probe import DatagramReply


@pytest.mark.asyncio
async def test_real_core_tcp_udp_and_provider_update_preserves_other_session(tmp_path, backend):
    binary = os.environ.get('MIHOMO_BIN')
    if not binary:
        pytest.skip('MIHOMO_BIN not provided; real-core integration is separately gated')
    # Refuse to occupy ports belonging to an existing local service.
    for port in (20000,20001,20002,20003,29090):
        with socket.socket() as check:
            check.bind(('127.0.0.1',port))
    core=Core(tmp_path,base_url='http://127.0.0.1:29090',host='127.0.0.1',secret='test-secret-32-characters-long!!',capacity=3,source_capacity=3)
    core.initialize()
    config=yaml.safe_load((tmp_path/'config.yaml').read_text())
    config['external-controller']='127.0.0.1:29090'
    for listener in config['listeners']:
        listener['listen']='127.0.0.1'
    atomic_write(tmp_path/'config.yaml',yaml.safe_dump(config))
    log=(tmp_path/'core.log').open('w')
    process=subprocess.Popen([binary,'-d',str(tmp_path)],stdout=log,stderr=log)
    gateway=Gateway('127.0.0.1')
    writers=[]
    try:
        for _ in range(100):
            try:
                await core.ready()
                break
            except Exception:
                if process.poll() is not None:
                    pytest.fail((tmp_path/'core.log').read_text())
                await asyncio.sleep(.05)
        else:
            pytest.fail('core not ready: '+(tmp_path/'core.log').read_text())
        text=yaml.safe_dump({'proxies':[{'name':'美国 local fixture','type':'socks5','server':backend[0],'port':backend[1],'udp':True}]})
        nodes,payload=parse_subscription(text,1)
        node=next(iter(nodes))
        await core.update_source(1,payload)
        upstream=await core.select(1,node)
        gateway.pulse(60)
        await gateway.listen('1',0)
        gateway.grant('1',upstream,gateway.clock()+180)
        port=gateway.servers['1'].sockets[0].getsockname()[1]
        reader,writer,_=await open_socks('127.0.0.1',port,1,'example.com',443)
        writers.append(writer)
        writer.write(b'first')
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(5),3)==b'first'
        _,udp_control,relay=await open_socks('127.0.0.1',port,3,'0.0.0.0',0)
        writers.append(udp_control)
        transport,protocol=await asyncio.get_running_loop().create_datagram_endpoint(DatagramReply,remote_addr=relay)
        try:
            # Use a numeric target: Mihomo may resolve domain UDP targets first.
            packet=b'\x00\x00\x00'+pack_address('8.8.8.8',53)+b'udp-roundtrip'
            transport.sendto(packet)
            result=await asyncio.wait_for(protocol.queue.get(),3)
            assert unpack_datagram(result)[2]==b'udp-roundtrip'
        finally:
            transport.close()
        _,third_payload=parse_subscription(text.replace('local fixture','third'),3)
        await core.update_source(3,third_payload)
        assert await core.mapping_valid(1,node)
        writer.write(b'after-third-source')
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(18),3)==b'after-third-source'
        # Invalid provider update rolls disk back; unrelated established TCP survives.
        with pytest.raises(RuntimeError):
            await core.update_source(3,'proxies: []\n')
        writer.write(b'unchanged')
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(9),3)==b'unchanged'
    finally:
        for writer in writers:
            writer.close()
        await gateway.close()
        await core.close()
        process.terminate()
        try:
            await asyncio.to_thread(process.wait,5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
        log.close()
