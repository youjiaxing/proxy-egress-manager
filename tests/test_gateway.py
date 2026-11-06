import asyncio

import pytest
from gateway.server import Gateway
from gateway.socks import open_socks, pack_address, unpack_datagram
from scheduler.probe import DatagramReply


@pytest.mark.asyncio
async def test_tcp_roundtrip_and_revocation_isolated(backend):
    now = [100.0]
    gateway = Gateway("127.0.0.1", clock=lambda: now[0])
    gateway.pulse(60)
    for identity in ("1", "2"):
        await gateway.listen(identity, 0)
        gateway.grant(identity, backend, 280)
    clients = []
    try:
        for identity in ("1", "2"):
            port = gateway.servers[identity].sockets[0].getsockname()[1]
            r, w, _ = await open_socks("127.0.0.1", port, 1, "example.com", 443)
            clients.append((r, w))
            w.write(b"hello")
            await w.drain()
            assert await r.readexactly(5) == b"hello"
        await gateway.revoke("1")
        assert await asyncio.wait_for(clients[0][0].read(), 1) == b""
        clients[1][1].write(b"still")
        await clients[1][1].drain()
        assert await clients[1][0].readexactly(5) == b"still"
    finally:
        for _, writer in clients:
            writer.close()
        await gateway.close()


@pytest.mark.asyncio
async def test_tcp_data_path_enforces_deadline_without_timer(backend):
    now = [0.0]
    gateway = Gateway("127.0.0.1", clock=lambda: now[0])
    await gateway.listen("1", 0)
    gateway.pulse(60)
    gateway.grant("1", backend, 180)
    port = gateway.servers["1"].sockets[0].getsockname()[1]
    r, w, _ = await open_socks("127.0.0.1", port, 1, "example.com", 443)
    now[0] = 61
    w.write(b"must not echo")
    await w.drain()
    assert await asyncio.wait_for(r.read(), 1) == b""
    w.close()
    await gateway.close()


@pytest.mark.asyncio
async def test_udp_reachable_bidirectional_no_timer_expiry_and_control_close(backend):
    now = [0.0]
    gateway = Gateway("127.0.0.1", clock=lambda: now[0])
    await gateway.listen("1", 0)
    gateway.pulse(60)
    gateway.grant("1", backend, 180)
    port = gateway.servers["1"].sockets[0].getsockname()[1]
    _, control, relay = await open_socks("127.0.0.1", port, 3, "0.0.0.0", 0)
    assert relay[0] == "127.0.0.1"
    transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(DatagramReply, remote_addr=relay)
    packet = b"\x00\x00\x00" + pack_address("example.com", 53) + b"udp-payload"
    try:
        transport.sendto(packet)
        response = await asyncio.wait_for(protocol.queue.get(), 1)
        assert unpack_datagram(response) == ("example.com", 53, b"udp-payload")
        now[0] = 61
        transport.sendto(packet)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(protocol.queue.get(), 0.1)
        gateway.pulse(60)
        control.close()
        await asyncio.sleep(0.03)
        transport.sendto(packet)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(protocol.queue.get(), 0.1)
    finally:
        transport.close()
        control.close()
        await gateway.close()


@pytest.mark.asyncio
async def test_udp_drops_fragments_and_foreign_client_port(backend):
    gateway = Gateway("127.0.0.1")
    await gateway.listen("1", 0)
    gateway.pulse(60)
    gateway.grant("1", backend, gateway.clock()+180)
    port = gateway.servers["1"].sockets[0].getsockname()[1]
    _, control, relay = await open_socks("127.0.0.1", port, 3, "0.0.0.0", 0)
    t1, p1 = await asyncio.get_running_loop().create_datagram_endpoint(DatagramReply, remote_addr=relay)
    t2, p2 = await asyncio.get_running_loop().create_datagram_endpoint(DatagramReply, remote_addr=relay)
    packet = b"\x00\x00\x00"+pack_address("example.com", 53)+b"ok"
    try:
        t1.sendto(packet)
        assert await asyncio.wait_for(p1.queue.get(), 1) == packet
        t2.sendto(packet)
        t1.sendto(b"\x00\x00\x01"+packet[3:])
        for p in (p1, p2):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(p.queue.get(), .05)
    finally:
        t1.close(); t2.close(); control.close()
        await gateway.close()


@pytest.mark.asyncio
async def test_new_lease_is_refused_until_old_cancellation_finishes():
    from gateway.server import Lease
    gateway=Gateway('127.0.0.1')
    gateway.leases['1']=Lease()
    gateway.pulse(60)
    gateway.grant('1',('127.0.0.1',9),gateway.clock()+180)
    started,cleaning,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
    async def old_session():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
    old=asyncio.create_task(old_session())
    gateway.leases['1'].tasks.add(old)
    await started.wait()
    revoke=asyncio.create_task(gateway.revoke('1'))
    await cleaning.wait()
    assert gateway.grant('1',('127.0.0.1',9),gateway.clock()+180) is None
    assert not gateway.allowed('1',gateway.leases['1'].generation)
    release.set(); await revoke
    gateway.leases['1'].tasks.discard(old)
    assert gateway.grant('1',('127.0.0.1',9),gateway.clock()+180) is not None
