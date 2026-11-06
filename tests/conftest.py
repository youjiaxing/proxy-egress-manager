import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest_asyncio
from gateway.socks import read_address, pack_address, unpack_datagram


class EchoUDP(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        # Keep SOCKS framing so the front relay exercises its validation path.
        try:
            unpack_datagram(data)
        except ValueError:
            return
        self.transport.sendto(data, address)


@pytest_asyncio.fixture
async def backend():
    connections, udp = set(), set()
    async def handle(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        transport = None
        try:
            header = await reader.readexactly(2)
            await reader.readexactly(header[1])
            writer.write(b"\x05\x00")
            await writer.drain()
            request = await reader.readexactly(3)
            target = await read_address(reader)
            if request[1] == 1:
                writer.write(b"\x05\x00\x00" + pack_address("127.0.0.1", 9999))
                await writer.drain()
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            else:
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(EchoUDP, local_addr=("127.0.0.1", 0))
                udp.add(transport)
                writer.write(b"\x05\x00\x00" + pack_address(*transport.get_extra_info("sockname")))
                await writer.drain()
                await reader.read()
        except (asyncio.IncompleteReadError, OSError):
            pass
        finally:
            if transport:
                transport.close()
                udp.discard(transport)
            writer.close()
            connections.discard(task)
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[:2]
    server.close()
    await server.wait_closed()
    for task in list(connections):
        task.cancel()
    await asyncio.gather(*connections, return_exceptions=True)
    for transport in udp:
        transport.close()
