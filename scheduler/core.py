import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
import yaml
from scheduler.http import read_limited


MAX_SOURCE_BYTES = 4 * 1024 * 1024


class SubscriptionLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("订阅不接受 YAML alias，请展开为普通节点定义")
        return super().compose_node(parent, index)


def decode64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def parse_uri(line):
    u = urlparse(line)
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    name = unquote(u.fragment) or "%s-%s" % (u.scheme, u.hostname)
    if u.scheme == "vmess":
        v = json.loads(decode64(line[8:]))
        node = {"name": v.get("ps", name), "type": "vmess", "server": v["add"],
                "port": int(v["port"]), "uuid": v["id"], "alterId": int(v.get("aid", 0)),
                "cipher": v.get("scy", "auto"), "udp": True}
        if v.get("tls") == "tls":
            node.update(tls=True, servername=v.get("sni") or v.get("host") or v["add"])
        if v.get("net") == "ws":
            node.update(network="ws", **{"ws-opts": {"path": v.get("path", "/"), "headers": {"Host": v.get("host", "")}}})
        return node
    if u.scheme == "ss":
        content = line[5:].split("#", 1)[0].split("?", 1)[0]
        if "@" not in content:
            content = decode64(content)
        user, address = content.rsplit("@", 1)
        if ":" not in user:
            user = decode64(user)
        cipher, password = user.split(":", 1)
        addr = urlparse("//" + address)
        if q.get("plugin"):
            raise ValueError("SS plugin URI 请使用含完整插件字段的 Mihomo YAML")
        return {"name": name, "type": "ss", "server": addr.hostname, "port": addr.port,
                "cipher": cipher, "password": password, "udp": True}
    kinds = {"socks5": "socks5", "trojan": "trojan", "vless": "vless", "hysteria2": "hysteria2", "hy2": "hysteria2", "tuic": "tuic"}
    if u.scheme not in kinds:
        raise ValueError("不支持的 URI 类型；可使用 Mihomo YAML proxies 格式")
    node = {"name": name, "type": kinds[u.scheme], "server": u.hostname, "port": u.port, "udp": True}
    if u.scheme == "socks5":
        if u.username:
            node.update(username=unquote(u.username), password=unquote(u.password or ""))
    elif u.scheme == "vless":
        node.update(uuid=u.username, tls=q.get("security") in ("tls", "reality"))
        if q.get("flow"):
            node["flow"] = q["flow"]
        if q.get("pbk"):
            node["reality-opts"] = {"public-key": q["pbk"], "short-id": q.get("sid", "")}
    elif u.scheme == "tuic":
        node.update(uuid=u.username, password=unquote(u.password or ""))
    else:
        node["password"] = unquote(u.username or "")
    if q.get("sni") or q.get("peer"):
        node["servername" if u.scheme in ("vless", "trojan") else "sni"] = q.get("sni") or q["peer"]
    if q.get("type") in ("ws", "grpc"):
        node["network"] = q["type"]
        if q["type"] == "ws":
            node["ws-opts"] = {"path": q.get("path", "/"), "headers": {"Host": q.get("host", "")}}
        else:
            node["grpc-opts"] = {"grpc-service-name": q.get("serviceName", "")}
    if q.get("fp"):
        node["client-fingerprint"] = q["fp"]
    return node


def parse_subscription(text, slot):
    try:
        value = yaml.load(text, Loader=SubscriptionLoader)
    except yaml.YAMLError:
        value = None
    if isinstance(value, dict) and isinstance(value.get("proxies"), list):
        raw = value["proxies"]
    else:
        content = text.strip()
        if "://" not in content:
            try:
                content = decode64(content)
            except Exception as exc:
                raise ValueError("订阅不是 Mihomo YAML 或支持的 URI 列表") from exc
        raw = [parse_uri(line.strip()) for line in content.splitlines() if line.strip()]
    if not raw or len(raw) > 5000:
        raise ValueError("订阅节点数须为 1–5000")
    nodes, payload = {}, []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("节点缺少 name")
        cfg = dict(item)
        if cfg.get("type") in ("direct", "reject", "dns") or not isinstance(cfg.get("server"), str):
            raise ValueError("只允许真实远程代理节点")
        if any(key in cfg for key in ("dialer-proxy", "interface-name", "routing-mark", "ca", "ca-str")):
            raise ValueError("节点含不允许的本地资源或跨节点引用")
        if not isinstance(cfg.get("port"), int) or not 1 <= cfg["port"] <= 65535:
            raise ValueError("节点端口无效")
        original = cfg.pop("name")
        fingerprint = hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        identity = "s%02d-%s" % (slot, fingerprint)
        cfg["name"] = identity
        payload.append(cfg)
        nodes[identity] = {"id": identity, "name": original[:200], "source": slot,
                           "disabled": False, "priority": 100, "hint_us": bool(re.search(r"美国|美國|🇺🇸|\bUS\b|\bUSA\b|United States", original, re.I)),
                           "fingerprint": fingerprint, "last_probe": None}
    return nodes, yaml.safe_dump({"proxies": payload}, allow_unicode=True)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        dirfd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Core:
    def __init__(self, directory, base_url="http://mihomo:9090", secret="", host="mihomo", capacity=32, source_capacity=16):
        self.directory, self.base_url, self.secret, self.host = Path(directory), base_url, secret, host
        if not 1 <= capacity <= 256 or not 1 <= source_capacity <= 128:
            raise ValueError("容量范围：分组 1–256、订阅 1–128")
        self.capacity, self.source_capacity = capacity, source_capacity
        self.session = None

    async def request(self, method, path, body=None):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        async with self.session.request(method, self.base_url + path, json=body,
                                        headers={"Authorization": "Bearer " + self.secret}) as response:
            if response.status >= 400:
                raise RuntimeError("Mihomo API 操作失败 (%s)" % response.status)
            return await response.json() if response.content_type == "application/json" else None

    def initialize(self):
        providers = {}
        for slot in range(1, self.source_capacity + 1):
            path = self.directory / "providers" / ("s%02d.yaml" % slot)
            if not path.exists():
                atomic_write(path, yaml.safe_dump({"proxies": [{"name": "empty-%02d" % slot,
                    "type": "socks5", "server": "127.0.0.1", "port": 9}]}))
            providers["s%02d" % slot] = {"type": "file", "path": "./providers/s%02d.yaml" % slot}
        groups, listeners = [], []
        for slot in range(0, self.capacity + 1):
            name = "probe" if slot == 0 else "group-%s" % slot
            groups.append({"name": name, "type": "select", "proxies": ["REJECT"], "use": list(providers)})
            listeners.append({"name": name, "type": "socks", "listen": "0.0.0.0", "port": 20000 + slot,
                              "udp": True, "proxy": name})
        config = {"allow-lan": True, "mode": "rule", "log-level": "silent", "ipv6": True,
                  "external-controller": "0.0.0.0:9090", "secret": self.secret,
                  "profile": {"store-selected": False}, "proxy-providers": providers,
                  "proxy-groups": groups, "listeners": listeners, "rules": ["MATCH,REJECT"],
                  "dns": {"enable": False}}
        atomic_write(self.directory / "config.yaml", yaml.safe_dump(config, sort_keys=False))

    async def ready(self):
        await self.request("GET", "/version")

    async def update_source(self, slot, payload):
        path = self.directory / "providers" / ("s%02d.yaml" % slot)
        old = path.read_text()
        atomic_write(path, payload)
        try:
            await self.request("PUT", "/providers/proxies/s%02d" % slot)
        except BaseException:
            atomic_write(path, old)
            # No group may resume until a subsequent successful revalidation.
            try:
                await self.request("PUT", "/providers/proxies/s%02d" % slot)
            except Exception:
                pass
            raise

    async def select(self, slot, node):
        name = "probe" if slot == 0 else "group-%s" % slot
        await self.request("PUT", "/proxies/" + name, {"name": node})
        result = await self.request("GET", "/proxies/" + name)
        if result.get("now") != node:
            raise RuntimeError("Mihomo 节点选择未生效")
        return self.host, 20000 + slot

    async def mapping_valid(self, slot, node):
        result = await self.request("GET", "/proxies/group-%s" % slot)
        return result.get("now") == node

    async def close(self):
        if self.session:
            await self.session.close()
