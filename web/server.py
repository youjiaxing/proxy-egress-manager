import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import os
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from scheduler.core import parse_subscription
from scheduler.validation import validate_settings
from scheduler.http import read_limited
from scheduler.maintenance import begin as begin_maintenance, finish as finish_maintenance


STATIC = Path(__file__).parent
SESSION_KEY = web.RequestKey("session", dict)


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000, dklen=32)
    return salt, digest


class Management:
    def __init__(self, controller, password, public_url, secure=True):
        self.controller, self.public_url, self.secure = controller, public_url.rstrip("/"), secure
        self.password = password_hash(password) if password else None
        self.sessions = OrderedDict()
        self.attempts = OrderedDict()
        self.login_inflight = 0
        self.jobs = OrderedDict()
        self.job_tasks = set()
        self.app = web.Application(middlewares=[self.security], client_max_size=32 * 1024 * 1024)
        self.app.add_routes([
            web.get("/", self.index), web.get("/app.js", self.script), web.get("/style.css", self.style),
            web.get("/healthz", self.health), web.post("/api/login", self.login),
            web.post("/api/logout", self.logout), web.get("/api/session", self.session),
            web.get("/api/state", self.state), web.get("/api/events", self.events),
            web.get("/api/sources/{slot}", self.source_details),
            web.post("/api/action", self.action), web.get("/api/jobs/{job}", self.job),
            web.post("/api/backup", self.backup), web.post("/api/restore", self.restore),
            web.get("/api/diagnostics", self.diagnostics),
        ])
        self.app.on_cleanup.append(self.cleanup)

    @web.middleware
    async def security(self, request, handler):
        try:
            if request.path.startswith("/api/"):
                if request.method != "GET":
                    if request.headers.get("Origin") != self.public_url:
                        raise web.HTTPForbidden(text="请求来源不匹配 PUBLIC_URL")
                    if request.content_type != "application/json":
                        raise web.HTTPUnsupportedMediaType(text="只接受 JSON")
                if request.path != "/api/login":
                    session = self.get_session(request)
                    if not session:
                        raise web.HTTPUnauthorized(text="请登录")
                    request[SESSION_KEY] = session
                    if request.method != "GET" and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf"]):
                        raise web.HTTPForbidden(text="CSRF 验证失败")
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        except (ValueError, KeyError, StopIteration, TypeError, json.JSONDecodeError):
            response = web.json_response({"error": "请求字段或备份格式无效"}, status=400)
        except Exception:
            response = web.json_response({"error": "操作失败，请查看脱敏事件记录"}, status=500)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if self.secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if isinstance(response, web.HTTPException):
            raise response
        return response

    def get_session(self, request):
        token = request.cookies.get("pem_session", "")
        session = self.sessions.get(token)
        if session and time.monotonic() < session["expires"]:
            return session
        self.sessions.pop(token, None)
        return None

    async def login(self, request):
        if not self.password:
            raise web.HTTPServiceUnavailable(text="部署时尚未配置管理员凭据")
        # Proxies do not get to spoof rate-limit identities via X-Forwarded-For.
        key = request.remote or "unknown"
        now = time.monotonic()
        count, since = self.attempts.get(key, (0, now))
        if now - since > 300:
            count, since = 0, now
        if self.login_inflight >= 4:
            raise web.HTTPTooManyRequests(text="登录校验繁忙，请稍后重试")
        self.login_inflight += 1
        try:
            data = json.loads(await asyncio.wait_for(read_limited(request, 2048), 5))
            supplied = str(data.get("password", ""))
            if len(supplied) > 1024:
                raise web.HTTPUnauthorized(text="凭据错误")
            _, digest = await asyncio.to_thread(password_hash, supplied, self.password[0])
        finally:
            self.login_inflight -= 1
        if not hmac.compare_digest(digest, self.password[1]):
            self.attempts[key] = count + 1, since
            if len(self.attempts) > 1000:
                self.attempts.popitem(last=False)
            if count >= 10:
                await asyncio.sleep(0.5)
                raise web.HTTPTooManyRequests(text="失败请求已节流；正确凭据仍可登录")
            raise web.HTTPUnauthorized(text="凭据错误")
        self.attempts.pop(key, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[token] = {"csrf": csrf, "expires": now + 8 * 3600}
        while len(self.sessions) > 100:
            self.sessions.popitem(last=False)
        response = web.json_response({"csrf": csrf})
        response.set_cookie("pem_session", token, httponly=True, secure=self.secure, samesite="Strict", max_age=8*3600, path="/")
        return response

    async def logout(self, request):
        self.sessions.pop(request.cookies.get("pem_session"), None)
        response = web.json_response({"ok": True})
        response.del_cookie("pem_session", path="/")
        return response

    async def session(self, request):
        return web.json_response({"csrf": request[SESSION_KEY]["csrf"]})

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html")

    async def script(self, request):
        return web.FileResponse(STATIC / "app.js")

    async def style(self, request):
        return web.FileResponse(STATIC / "style.css")

    async def health(self, request):
        healthy = not self.controller.maintenance and self.controller.clock() < self.controller.gateway.scheduler_deadline
        return web.json_response({"ready": healthy}, status=200 if healthy else 503)

    async def state(self, request):
        return web.json_response(self.controller.public())

    async def source_details(self, request):
        # Secrets are returned only on explicit authenticated editing, never in the dashboard state.
        try:
            slot = int(request.match_info["slot"])
        except ValueError:
            raise web.HTTPNotFound()
        # No await between lookup and serialization: the event loop gives a coherent
        # snapshot without waiting behind long-running subscription/node probes.
        source = next((s for s in self.controller.state["sources"] if s["slot"] == slot), None)
        if source is None:
            raise web.HTTPNotFound()
        return web.json_response({key: source[key] for key in ("slot", "name", "url")})

    async def events(self, request):
        return web.json_response(self.controller.store.events())

    async def action(self, request):
        data = await request.json()
        kind = data.pop("kind")
        return self.submit(self.controller.action(kind, data))

    def submit(self, coroutine):
        active = sum(j["status"] == "running" for j in self.jobs.values())
        if active >= 8:
            coroutine.close()
            raise web.HTTPTooManyRequests(text="等待管理操作完成")
        identity = secrets.token_hex(12)
        self.jobs[identity] = {"status": "running", "message": "操作已排队"}
        async def execute():
            try:
                await coroutine
                self.jobs[identity] = {"status": "done", "message": "操作已完成"}
            except asyncio.CancelledError:
                self.jobs[identity] = {"status": "error", "message": "服务停止，操作已取消"}
                raise
            except Exception as exc:
                # Only curated ValueError messages may be returned, never network exception URLs.
                message = str(exc) if isinstance(exc, ValueError) else "操作失败，保持关闭并查看事件记录"
                self.jobs[identity] = {"status": "error", "message": message[:300]}
                self.controller.event("error", "管理操作失败（秘密已隐藏）")
            while len(self.jobs) > 100:
                oldest = next((k for k, v in self.jobs.items() if v["status"] != "running"), None)
                if oldest is None:
                    break
                self.jobs.pop(oldest)
        task = asyncio.create_task(execute())
        self.job_tasks.add(task)
        task.add_done_callback(self.job_tasks.discard)
        return web.json_response({"job": identity}, status=202)

    async def job(self, request):
        return web.json_response(self.jobs[request.match_info["job"]])

    async def backup(self, request):
        async with self.controller.lock:
            if self.controller.maintenance:
                raise web.HTTPConflict(text="维护恢复未完成，不能导出不一致备份")
            body = self.controller.store.backup()
            body["providers"] = {str(s["slot"]): (self.controller.core.directory / "providers" / ("s%02d.yaml" % s["slot"])).read_text()
                                 for s in self.controller.state["sources"]}
        response = web.json_response(body)
        response.headers["Content-Disposition"] = 'attachment; filename="pem-backup.json"'
        return response

    async def restore(self, request):
        backup = await request.json()
        validate_backup(backup, self.controller.core.capacity, self.controller.core.source_capacity)
        async def restore_job():
            async with self.controller.lock:
                self.controller.maintenance = True
                begin_maintenance(self.controller.store.directory)
                await self.controller.pause_all("恢复备份：停止全部转发并清理旧会话")
                for slot, payload in backup["providers"].items():
                    await self.controller.core.update_source(int(slot), payload)
                restored = backup["state"]
                for group in restored["groups"]:
                    group.update(status="paused", reason="备份恢复后等待重验", failure_since=None)
                old_ids = {g["id"] for g in self.controller.state["groups"]}
                new_ids = {g["id"] for g in restored["groups"]}
                for identity in old_ids - new_ids:
                    await self.controller.gateway.remove(identity)
                for group in restored["groups"]:
                    await self.controller.gateway.listen(group["id"], group["port"])
                self.controller.identities.clear()
                self.controller.store.restore(restored, backup.get("events", []))
                self.controller.event("restore", "已恢复备份；旧授权不恢复，等待独立验证")
                finish_maintenance(self.controller.store.directory)
                self.controller.maintenance = False
                await self.controller.tick()
        return self.submit(restore_job())

    async def diagnostics(self, request):
        state = self.controller.public()
        for source in state["sources"]:
            source["name"] = "source-%s" % source["slot"]
        for node in state["nodes"].values():
            node["name"] = node["id"]
        for group in state["groups"]:
            group["name"] = "group-%s" % group["id"]
        for key in ("tcp_identity_urls", "tcp_check_urls", "stun_servers", "country_url"):
            state["settings"][key] = "[redacted]"
        response = web.json_response({"state": state, "notice": "仍含出口IP与运行时间，请检查后再分享"})
        response.headers["Content-Disposition"] = 'attachment; filename="pem-diagnostics.json"'
        return response

    async def cleanup(self, app):
        for task in self.job_tasks:
            task.cancel()
        await asyncio.gather(*self.job_tasks, return_exceptions=True)


def validate_backup(backup, capacity, source_capacity):
    if backup.get("format") != 1 or backup.get("state", {}).get("schema") != 1:
        raise ValueError("不兼容的备份版本")
    events = backup.get("events", [])
    if not isinstance(events, list) or len(events) > 5000:
        raise ValueError("备份事件集合无效")
    for event in events:
        if (not isinstance(event, (list, tuple)) or len(event) != 4 or
                not isinstance(event[0], (int, float)) or not math.isfinite(event[0]) or
                not isinstance(event[1], str) or (event[2] is not None and not isinstance(event[2], str)) or
                not isinstance(event[3], str) or len(event[3]) > 1000):
            raise ValueError("备份事件字段无效")
    state, providers = backup["state"], backup["providers"]
    if not isinstance(providers, dict) or not isinstance(state.get("nodes"), dict):
        raise ValueError("备份结构无效")
    if not isinstance(state["groups"], list) or not isinstance(state["sources"], list):
        raise ValueError("备份结构无效")
    slots = []
    for source in state["sources"]:
        slot = source["slot"]
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= source_capacity or slot in slots:
            raise ValueError("备份订阅槽位无效")
        if not isinstance(source.get("name"), str) or not isinstance(source.get("enabled"), bool):
            raise ValueError("备份订阅字段无效")
        if not isinstance(source.get("url"), str) or (source["url"] and not source["url"].startswith("https://")):
            raise ValueError("备份订阅 URL 无效")
        slots.append(slot)
        nodes, _ = parse_subscription(providers[str(slot)], slot)
        if not all(k in state["nodes"] for k in nodes):
            raise ValueError("备份节点与供应配置不一致")
    if not isinstance(state.get("revision"), int) or state["revision"] < 0:
        raise ValueError("备份版本计数无效")
    ids = []
    for node in state["nodes"].values():
        if not isinstance(node.get("name"), str) or not isinstance(node.get("disabled"), bool) or not isinstance(node.get("priority"), int) or node.get("source") not in slots:
            raise ValueError("备份节点字段无效")
    for group in state["groups"]:
        required = ("id", "name", "port", "node", "paused", "pinned", "pin_until", "status", "reason", "tcp_ips", "udp_ips", "verified_at", "since", "failure_since")
        if any(key not in group for key in required) or not isinstance(group["name"], str):
            raise ValueError("备份分组字段不完整")
        slot = int(group["id"])
        if not 1 <= slot <= capacity or group["id"] in ids or group["port"] != 1080 + slot:
            raise ValueError("备份分组槽位无效")
        ids.append(group["id"])
        for key in ("pin_until", "verified_at", "since", "failure_since"):
            value = group[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0):
                raise ValueError("备份时间字段无效")
        for key in ("tcp_ips", "udp_ips"):
            if not isinstance(group[key], list) or len(group[key]) > 16:
                raise ValueError("备份出口集合无效")
        for ip in group["tcp_ips"] + group["udp_ips"]:
            ipaddress.ip_address(ip)
    if set(providers) != {str(slot) for slot in slots}:
        raise ValueError("备份含未知 provider")
    expected = set()
    for slot in slots:
        parsed, _ = parse_subscription(providers[str(slot)], slot)
        expected.update(parsed)
    if set(state["nodes"]) != expected:
        raise ValueError("备份节点集合不匹配")
    for group in state["groups"]:
        if group["node"] is not None and not isinstance(group["node"], str):
            raise ValueError("备份节点身份无效")
        if not isinstance(group["id"], str) or str(int(group["id"])) != group["id"]:
            raise ValueError("备份组身份无效")
        for key in ("paused", "pinned"):
            if not isinstance(group[key], bool):
                raise ValueError("备份状态无效")
    validate_settings(state["settings"])
