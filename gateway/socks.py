import asyncio
import ipaddress
import struct


async def read_address(reader):
    atyp = (await reader.readexactly(1))[0]
    if atyp == 1:
        host = str(ipaddress.ip_address(await reader.readexactly(4)))
    elif atyp == 4:
        host = str(ipaddress.ip_address(await reader.readexactly(16)))
    elif atyp == 3:
        host = (await reader.readexactly((await reader.readexactly(1))[0])).decode("idna")
    else:
        raise ValueError("unsupported SOCKS address")
    return host, struct.unpack("!H", await reader.readexactly(2))[0]


def pack_address(host, port):
    try:
        ip = ipaddress.ip_address(host)
        return bytes([1 if ip.version == 4 else 4]) + ip.packed + struct.pack("!H", port)
    except ValueError:
        raw = host.encode("idna")
        if len(raw) > 255:
            raise ValueError("SOCKS host too long")
        return b"\x03" + bytes([len(raw)]) + raw + struct.pack("!H", port)


def unpack_datagram(data):
    if len(data) < 4 or data[:3] != b"\x00\x00\x00":
        raise ValueError("fragmented/invalid SOCKS UDP datagram")
    atyp = data[3]
    if atyp in (1, 4):
        n = 4 if atyp == 1 else 16
        if len(data) < 6 + n:
            raise ValueError("truncated SOCKS UDP datagram")
        host, offset = str(ipaddress.ip_address(data[4:4+n])), 4+n
    elif atyp == 3 and len(data) >= 5:
        n = data[4]
        if len(data) < 7 + n:
            raise ValueError("truncated SOCKS UDP datagram")
        host, offset = data[5:5+n].decode("idna"), 5+n
    else:
        raise ValueError("invalid SOCKS UDP address")
    return host, struct.unpack("!H", data[offset:offset+2])[0], data[offset+2:]


async def open_socks(host, port, command, target_host, target_port, timeout=8):
    async def connect():
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            if await reader.readexactly(2) != b"\x05\x00":
                raise ConnectionError("backend authentication rejected")
            writer.write(b"\x05" + bytes([command]) + b"\x00" + pack_address(target_host, target_port))
            await writer.drain()
            reply = await reader.readexactly(3)
            if reply != b"\x05\x00\x00":
                raise ConnectionError("backend request rejected")
            bound = await read_address(reader)
            return reader, writer, bound
        except BaseException:
            writer.close()
            raise
    return await asyncio.wait_for(connect(), timeout)
