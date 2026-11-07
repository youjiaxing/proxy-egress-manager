import asyncio
import ipaddress
import os
import socket
import struct
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field

import aiohttp
from aiohttp_socks import ProxyConnector
from scheduler.http import read_json
from gateway.socks import open_socks, pack_address, unpack_datagram


COOKIE = 0x2112A442


def stun_request(transaction):
    return struct.pack("!HHI", 1, 0, COOKIE) + transaction


def stun_response(data, transaction):
    if len(data) < 20:
        raise ValueError("truncated STUN")
    kind, length, cookie = struct.unpack("!HHI", data[:8])
    if kind != 0x101 or cookie != COOKIE or data[8:20] != transaction or len(data) != length + 20:
        raise ValueError("invalid STUN response")
    offset = 20
    while offset + 4 <= len(data):
        attr, size = struct.unpack("!HH", data[offset:offset+4])
        body = data[offset+4:offset+4+size]
        offset += 4 + ((size + 3) // 4) * 4
        if attr in (0x20, 1) and len(body) >= 8:
            family = body[1]
            n = 4 if family == 1 else 16 if family == 2 else 0
            if n == 0 or len(body) != n + 4:
                continue
            address = body[4:]
            if attr == 0x20:
                key = struct.pack("!I", COOKIE) + transaction
                address = bytes(a ^ b for a, b in zip(address, key))
            return str(ipaddress.ip_address(address))
    raise ValueError("STUN response has no mapped address")


class DatagramReply(asyncio.DatagramProtocol):
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=32)

    def datagram_received(self, data, addr):
        if not self.queue.full():
            self.queue.put_nowait(data)


async def stun_query(server, timeout, upstream=None):
    host, port = server.rsplit(":", 1)
    port = int(port)
    loop = asyncio.get_running_loop()
    control = transport = None
    transaction = os.urandom(12)
    request = stun_request(transaction)
    try:
        if upstream:
            _, control, relay = await open_socks(*upstream, 3, "0.0.0.0", 0, timeout=timeout)
            if relay[0] in ("0.0.0.0", "::"):
                relay = upstream[0], relay[1]
            address = (await loop.getaddrinfo(*relay, type=socket.SOCK_DGRAM))[0][4][:2]
            payload = b"\x00\x00\x00" + pack_address(host, port) + request
        else:
            address = (await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM))[0][4][:2]
            payload = request
        transport, protocol = await loop.create_datagram_endpoint(DatagramReply, remote_addr=address)
        # Retry a lost STUN datagram, within one bounded identity attempt.
        for _ in range(2):
            transport.sendto(payload)
            try:
                while True:
                    data = await asyncio.wait_for(protocol.queue.get(), timeout / 2)
                    if upstream:
                        data = unpack_datagram(data)[2]
                    return stun_response(data, transaction)
            except (ValueError, asyncio.TimeoutError):
                continue
        raise TimeoutError("STUN identity unavailable")
    finally:
        if transport:
            transport.close()
        if control:
            control.close()


@dataclass
class Observation:
    tcp_ok: bool = False
    udp_ok: bool = False
    tcp_ips: set = field(default_factory=set)
    udp_ips: set = field(default_factory=set)
    countries: dict = field(default_factory=dict)
    failure_confirmed: bool = False
    reason: str = "身份验证未完成"
    identity_complete: bool = True
    observed_at: object = None
    samples: list = field(default_factory=list)
    country_errors: dict = field(default_factory=dict)
    identity_errors: list = field(default_factory=list)

    @property
    def complete(self):
        return bool(self.identity_complete and self.tcp_ok and self.udp_ok and self.tcp_ips and self.udp_ips and
                    all(ip in self.countries for ip in self.tcp_ips | self.udp_ips))

    @property
    def us(self):
        return self.complete and all(self.countries[ip] == "US" for ip in self.tcp_ips | self.udp_ips)

    def public(self):
        return {"tcp_ok": self.tcp_ok, "udp_ok": self.udp_ok, "tcp_ips": sorted(self.tcp_ips),
                "udp_ips": sorted(self.udp_ips), "countries": self.countries,
                "complete": self.complete, "us": self.us, "reason": self.reason, "country_errors": self.country_errors, "identity_errors": self.identity_errors,
                "coverage": "仅配置的 HTTPS/STUN 目标所见出口；其他目的地和地址族未知", "samples": self.samples}


class Prober:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.country_cache = {}
        self.country_cooldown = {}

    async def country(self, session, template, ip):
        """Cache IP metadata, never the observed TCP/UDP identity or its lease."""
        key = (template, ip)
        now = self.clock()
        cached = self.country_cache.get(key)
        if cached and now < cached[0]:
            return cached[1:]
        if now < self.country_cooldown.get(template, 0):
            return None, "国家查询服务限流，等待退避后重试"
        code, error, ttl = None, "国家查询失败，暂时无法确认出口国家", 60
        try:
            async with session.get(template.replace('{ip}', ip), allow_redirects=False) as response:
                if response.status == 429:
                    try: retry = float(response.headers.get('Retry-After', 900))
                    except ValueError: retry = 900
                    self.country_cooldown[template] = now + max(60, min(3600, retry))
                    error = "国家查询服务限流，等待退避后重试"
                elif response.status == 200:
                    body = await read_json(response)
                    value = body.get('country_code') or body.get('countryCode')
                    if body.get('success') is not False and not body.get('error') and isinstance(value, str) and len(value) == 2 and value.isascii() and value.isalpha():
                        code, error, ttl = value.upper(), None, 6 * 3600
        except Exception:
            pass
        # Bound memory, including when subscriptions expose many distinct IPs.
        if len(self.country_cache) >= 512:
            self.country_cache.pop(next(iter(self.country_cache)))
        if len(self.country_cooldown) > 16:
            self.country_cooldown.pop(next(iter(self.country_cooldown)))
        self.country_cache[key] = (self.clock() + ttl, code, error)
        return code, error

    async def observe(self, upstream, settings):
        timeout = settings["probe_timeout"]
        result = Observation()
        connector = ProxyConnector.from_url("socks5://%s:%s" % upstream, rdns=True)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as proxy:
            async def tcp_check(url):
                start = time.monotonic()
                ok, ms = False, None
                try:
                    async with proxy.get(url, allow_redirects=False) as response:
                        ms = round((time.monotonic()-start)*1000, 2)
                        await response.content.read(4096)
                        ok = 200 <= response.status < 400
                        return ok
                except Exception:
                    return False
                finally:
                    target = urlsplit(url)
                    result.samples.append({"target":target.scheme+"://"+target.netloc+target.path,"method":"http-get-headers","ok":ok,"ms":ms if ok else None})
            async def tcp_identity(url):
                try:
                    async with proxy.get(url, allow_redirects=False) as response:
                        if response.status == 200:
                            body = await read_json(response)
                            return str(ipaddress.ip_address(body["ip"]))
                except Exception:
                    pass
                target = urlsplit(url)
                result.identity_errors.append(target.scheme+'://'+target.netloc+target.path)
                return None
            async def udp_identity(server):
                start = time.monotonic()
                value = None
                try:
                    value = await asyncio.wait_for(stun_query(server, timeout, upstream), timeout + 1)
                    return value
                except Exception:
                    return None
                finally:
                    if value is None:
                        result.identity_errors.append(server)
                    result.samples.append({"target":server,"method":"stun-with-retry","ok":bool(value),"ms":round((time.monotonic()-start)*1000,2) if value else None})
            checks, tcp_ips, udp_ips = await asyncio.gather(
                asyncio.gather(*(tcp_check(u) for u in settings["tcp_check_urls"])),
                asyncio.gather(*(tcp_identity(u) for u in settings["tcp_identity_urls"])),
                asyncio.gather(*(udp_identity(u) for u in settings["stun_servers"])))
            result.identity_complete = all(tcp_ips) and all(udp_ips)
            result.tcp_ips = {ip for ip in tcp_ips if ip}
            result.udp_ips = {ip for ip in udp_ips if ip}
            result.tcp_ok = any(checks) or bool(result.tcp_ips)
            result.udp_ok = bool(result.udp_ips)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as direct:
            async def tcp_control(url):
                try:
                    async with direct.get(url, allow_redirects=False) as response:
                        return 200 <= response.status < 400
                except Exception:
                    return False
            async def udp_control(server):
                try:
                    await asyncio.wait_for(stun_query(server, timeout), timeout + 1)
                    return True
                except Exception:
                    return False
            async def country(ip):
                try:
                    if not ipaddress.ip_address(ip).is_global:
                        result.countries[ip] = "INVALID"
                        return
                    code, error = await self.country(direct, settings['country_url'], ip)
                    if code:
                        result.countries[ip] = code
                    else:
                        result.country_errors[ip] = error
                except Exception:
                    pass
            controls = []
            if not result.tcp_ok:
                controls.extend(tcp_control(u) for u in settings["tcp_check_urls"])
            if not result.udp_ok:
                controls.extend(udp_control(u) for u in settings["stun_servers"])
            outcomes, _ = await asyncio.gather(asyncio.gather(*controls),
                asyncio.gather(*(country(ip) for ip in result.tcp_ips | result.udp_ips)))
            result.failure_confirmed = any(outcomes)
        if result.complete:
            result.reason = "双协议出口已验证" if result.us else "观测到非美国出口"
        elif result.country_errors and result.identity_complete and result.tcp_ok and result.udp_ok:
            result.reason = "；".join(sorted(set(result.country_errors.values()))) + "；TCP/UDP 连通不代表出口验证通过"
        elif result.identity_errors and result.tcp_ok and result.udp_ok:
            result.reason = "部分出口验证目标未返回有效结果：" + "、".join(sorted(result.identity_errors))
        elif result.failure_confirmed:
            result.reason = "必需协议持续连通故障（独立对照目标可达）"
        else:
            result.reason = "出口身份不完整或探测目标不可用；不据此盲切"
        return result
