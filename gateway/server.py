import asyncio
import socket
import time
from dataclasses import dataclass, field
from gateway.socks import open_socks, pack_address, read_address, unpack_datagram


@dataclass
class Lease:
    generation: int = 0
    deadline: float = 0
    revocations: int = 0
    upstream: object = None
    tasks: set = field(default_factory=set)
    transports: set = field(default_factory=set)


class Gateway:
    def __init__(self, host="0.0.0.0", advertise_host=None, clock=time.monotonic):
        self.host, self.advertise_host, self.clock = host, advertise_host, clock
        self.leases, self.servers = {}, {}
        self.scheduler_deadline = 0

    def allowed(self, group, generation):
        lease = self.leases.get(group)
        return bool(lease and not lease.revocations and lease.generation == generation and lease.upstream and
                    self.clock() < min(lease.deadline, self.scheduler_deadline))

    def pulse(self, grace):
        self.scheduler_deadline = self.clock() + grace

    def grant(self, group, upstream, identity_deadline):
        lease = self.leases.setdefault(group, Lease())
        if lease.revocations:
            return None
        lease.upstream, lease.deadline = upstream, identity_deadline
        return lease.generation

    async def revoke(self, group):
        lease = self.leases.setdefault(group, Lease())
        lease.generation += 1
        lease.revocations += 1
        lease.deadline, lease.upstream = 0, None
        tasks = list(lease.tasks)
        try:
            for transport in list(lease.transports):
                transport.close()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            lease.transports.clear()
        finally:
            lease.revocations -= 1

    async def listen(self, group, port):
        if group not in self.servers:
            self.leases.setdefault(group, Lease())
            self.servers[group] = await asyncio.start_server(
                lambda r, w: self.handle(group, r, w), self.host, port)

    async def remove(self, group):
        await self.revoke(group)
        server = self.servers.pop(group, None)
        if server:
            server.close()
            await server.wait_closed()

    async def close(self):
        for group in list(self.servers):
            await self.remove(group)

    async def handle(self, group, reader, writer):
        lease = self.leases[group]
        task = asyncio.current_task()
        lease.tasks.add(task)
        lease.transports.add(writer)
        generation = lease.generation
        backend_writer = None
        udp_transports = []
        try:
            if not self.allowed(group, generation):
                return
            greeting = await asyncio.wait_for(reader.readexactly(2), 8)
            if greeting[0] != 5 or greeting[1] == 0:
                return
            methods = await asyncio.wait_for(reader.readexactly(greeting[1]), 8)
            writer.write(b"\x05\x00" if 0 in methods else b"\x05\xff")
            await writer.drain()
            if 0 not in methods:
                return
            request = await asyncio.wait_for(reader.readexactly(3), 8)
            if request[0] != 5 or request[2] != 0:
                return
            target = await asyncio.wait_for(read_address(reader), 8)
            command = request[1]
            if command not in (1, 3) or not self.allowed(group, generation):
                writer.write(b"\x05\x07\x00" + pack_address("0.0.0.0", 0))
                await writer.drain()
                return
            backend_target = target if command == 1 else ("0.0.0.0", 0)
            backend_reader, backend_writer, bound = await open_socks(*lease.upstream, command, *backend_target)
            lease.transports.add(backend_writer)
            if not self.allowed(group, generation):
                return
            if command == 1:
                writer.write(b"\x05\x00\x00" + pack_address(*bound))
                await writer.drain()
                async def copy(src, dst):
                    while True:
                        data = await src.read(65536)
                        if not data or not self.allowed(group, generation):
                            return
                        # No await between authorization check and the user-space write.
                        dst.write(data)
                        await dst.drain()
                pumps = [asyncio.create_task(copy(reader, backend_writer)),
                         asyncio.create_task(copy(backend_reader, writer))]
                try:
                    await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for pump in pumps:
                        pump.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
            else:
                loop = asyncio.get_running_loop()
                # Upstream relay is private; expose only a newly bound client-facing relay.
                relay_host = bound[0] if bound[0] not in ("0.0.0.0", "::") else lease.upstream[0]
                infos = await loop.getaddrinfo(relay_host, bound[1], type=socket.SOCK_DGRAM)
                relay = infos[0][4][:2]
                peer_ip = writer.get_extra_info("peername")[0]
                front = ClientRelay(self, group, generation, peer_ip, target[1])
                back = BackendRelay(self, group, generation, front)
                bt, _ = await loop.create_datagram_endpoint(lambda: back, remote_addr=relay)
                front.backend = bt
                ft, _ = await loop.create_datagram_endpoint(lambda: front, local_addr=(self.host, 0))
                udp_transports = [bt, ft]
                lease.transports.update(udp_transports)
                advertise = self.advertise_host or writer.get_extra_info("sockname")[0]
                writer.write(b"\x05\x00\x00" + pack_address(advertise, ft.get_extra_info("sockname")[1]))
                await writer.drain()
                # Closing either TCP control channel destroys the UDP association.
                controls = [asyncio.create_task(reader.read(1)), asyncio.create_task(backend_reader.read(1))]
                try:
                    await asyncio.wait(controls, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for control in controls:
                        control.cancel()
                    await asyncio.gather(*controls, return_exceptions=True)
        except (OSError, ValueError, ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            for transport in udp_transports:
                transport.close()
                lease.transports.discard(transport)
            if backend_writer:
                backend_writer.close()
                lease.transports.discard(backend_writer)
            writer.close()
            lease.transports.discard(writer)
            lease.tasks.discard(task)


class ClientRelay(asyncio.DatagramProtocol):
    def __init__(self, gateway, group, generation, peer_ip, requested_port):
        self.gateway, self.group, self.generation = gateway, group, generation
        self.peer_ip, self.requested_port = peer_ip, requested_port
        self.client, self.transport, self.backend = None, None, None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if not self.gateway.allowed(self.group, self.generation) or addr[0] != self.peer_ip:
            return
        if self.requested_port and addr[1] != self.requested_port:
            return
        if self.client is not None and addr != self.client:
            return
        try:
            unpack_datagram(data)
        except (ValueError, UnicodeError):
            return
        self.client = addr
        self.backend.sendto(data)


class BackendRelay(asyncio.DatagramProtocol):
    def __init__(self, gateway, group, generation, front):
        self.gateway, self.group, self.generation, self.front = gateway, group, generation, front

    def datagram_received(self, data, addr):
        if not self.gateway.allowed(self.group, self.generation) or not self.front.client:
            return
        try:
            unpack_datagram(data)
        except (ValueError, UnicodeError):
            return
        self.front.transport.sendto(data, self.front.client)
