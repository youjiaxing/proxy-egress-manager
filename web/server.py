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
from scheduler.policy import normalize, validate_v2, identity
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
        self.diagnostic = None
        self.job_by_id = {}
        self.cancelled_by_user = set()
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
            web.get('/api/traffic', self.traffic), web.get('/api/quality', self.quality), web.get('/api/quality/export', self.quality_export),
            web.post('/api/groups/{uid}/selection/cancel', self.cancel_selection),
            web.get('/api/pool', self.pool),
            web.post('/api/tests', self.tests), web.post('/api/jobs/{job}/cancel', self.cancel_job),
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
        cancellable = kind in ('group_switch','group_retry','group_scope','group_evaluate')
        cancel = asyncio.Event() if cancellable else None
        holder = []
        def progress(message):
            if holder:
                self.jobs[holder[0]]['message'] = message
        response = self.submit(self.controller.action(kind, data, cancel, progress))
        if cancellable:
            job=json.loads(response.text)['job']
            holder.append(job)
            if not hasattr(self,'search_jobs'): self.search_jobs = {}
            self.search_jobs[job]=cancel
            response = web.json_response({'job':job,'cancellable':True},status=202)
        return response

    def submit(self, coroutine):
        active = sum(j["status"] == "running" for j in self.jobs.values())
        if active >= 8:
            coroutine.close()
            raise web.HTTPTooManyRequests(text="等待管理操作完成")
        identity = secrets.token_hex(12)
        self.jobs[identity] = {"status": "running", "message": "操作已排队"}
        async def execute():
            try:
                result = await coroutine
                self.jobs[identity] = {"status": "done", "message": "操作已完成", "result":result}
            except asyncio.CancelledError:
                self.jobs[identity] = {"status": "error", "message": "用户已取消测试；未执行部分不计入结果" if identity in self.cancelled_by_user else "服务停止，操作已取消"}
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
                getattr(self, "test_jobs", set()).discard(oldest)
                getattr(self, "search_jobs", {}).pop(oldest,None)
        task = asyncio.create_task(execute())
        self.job_by_id[identity] = task
        self.job_tasks.add(task)
        task.add_done_callback(lambda t: self.job_by_id.pop(identity,None))
        task.add_done_callback(self.job_tasks.discard)
        return web.json_response({"job": identity}, status=202)

    async def job(self, request):
        job = self.jobs.get(request.match_info['job'])
        if job is None: raise web.HTTPNotFound(text='任务记录已不存在')
        return web.json_response(job)

    async def tests(self, request):
        if self.diagnostic is None:
            raise web.HTTPServiceUnavailable(text='独立诊断服务未配置')
        data = await request.json()
        identity_holder = []
        def progress(message):
            if identity_holder:
                self.jobs[identity_holder[0]]['message'] = message
        response = self.submit(self.diagnostic.run(data, progress))
        identity_holder.append(json.loads(response.text)['job'])
        if not hasattr(self, 'test_jobs'): self.test_jobs = set()
        self.test_jobs.add(json.loads(response.text)['job'])
        return response

    async def cancel_job(self, request):
        task = self.job_by_id.get(request.match_info['job'])
        if task is None: raise web.HTTPNotFound()
        event = getattr(self,'search_jobs',{}).get(request.match_info['job'])
        if event is not None:
            event.set()
            return web.json_response({'ok':True,'message':'已请求停止，将在当前检查结束后停止查找'})
        # Only diagnostic jobs expose a cancellation control in the UI. Do not interrupt state transactions.
        if not request.match_info['job'] in getattr(self, 'test_jobs', set()):
            raise web.HTTPForbidden(text='此操作不能取消')
        self.cancelled_by_user.add(request.match_info['job'])
        if not task.cancelling(): task.cancel()
        return web.json_response({'ok':True})

    async def cancel_selection(self, request):
        event=self.controller.selection_cancellations.get(request.match_info['uid'])
        if event is None:
            raise web.HTTPNotFound(text='本组当前没有正在进行的节点查找')
        event.set()
        return web.json_response({'ok':True})

    async def pool(self, request):
        """Read existing per-node history on demand, never send a probe."""
        group=self.controller.group(request.query.get('group'))
        uid=group['uid'];history=self.controller.history
        from scheduler.policy import node_ref, source_for
        import hashlib
        settings=self.controller.state['settings']
        config=hashlib.sha256(json.dumps([self.controller.history_epoch,'tcp-us-independent-v1']+[settings[k] for k in ('tcp_check_urls','tcp_identity_urls','stun_servers','country_url')],sort_keys=True).encode()).hexdigest()[:16]
        refs=[node_ref(self.controller.state,n) for n in self.controller.state['nodes'].values()
              if not n['disabled'] and source_for(self.controller.state,n)['enabled']][:100]
        def read():
            summaries={}
            for ref in refs:
                rows=history.query('',24,ref)['series'] if history else []
                rows=[s for s in rows if s.get('config')==config and s['method']=='http-get-headers' and s['origin'] in ('running','candidate')]
                rows.sort(key=lambda s:(s['origin']!='running',-(s['points'][-1]['time'] if s['points'] else 0)))
                summaries[ref]={k:rows[0][k] for k in ('target','origin','attempts','successes','mean_ms','variation_ms')} if rows else None
            return summaries
        try:
            summaries=await asyncio.to_thread(read)
            return web.json_response({'group_uid':uid,'summaries':summaries})
        except Exception:
            return web.json_response({'group_uid':uid,'summaries':{},'error':'历史暂不可读'})

    async def traffic(self, request):
        meter = self.controller.traffic
        if meter is None: raise web.HTTPServiceUnavailable(text='流量统计尚未配置')
        group = request.query.get('group')
        node = request.query.get('node')
        if group and node: raise ValueError('请选择一个统计视角')
        offset = int(request.query.get('offset','0'))
        if not 0 <= offset <= 50000: raise ValueError('分页无效')
        return web.json_response(await meter.query(group, node, offset))

    async def quality(self, request):
        history = self.controller.history
        if history is None: return web.json_response({'records':[], 'series':[], 'coverage_start':None, 'error':'暂无质量历史'})
        hours = int(request.query.get('hours','24'))
        if hours not in (24,168): raise ValueError('时间范围无效')
        node = request.query.get('node')
        group = request.query.get('group')
        if node:
            from scheduler.policy import node_ref
            if not any(node_ref(self.controller.state,n)==node for n in self.controller.state['nodes'].values()):
                raise web.HTTPNotFound()
            owner = ''
        elif group:
            owner = self.controller.group(group)['uid']
        else: raise ValueError('请选择分组或节点')
        try:
            result = await asyncio.to_thread(history.query, owner, hours, node)
        except Exception:
            result = {'records':[], 'series':[], 'coverage_start':None, 'error':'质量历史暂不可读，检测状态不受影响'}
        return web.json_response(result)

    async def quality_export(self, request):
        history = self.controller.history
        if history is None: raise web.HTTPNotFound()
        response = web.StreamResponse(headers={'Content-Type':'application/x-ndjson','Content-Disposition':'attachment; filename="pem-quality.ndjson"'})
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        await response.prepare(request)
        after = 0
        while True:
            rows = await asyncio.to_thread(history.export_page, after)
            if not rows: break
            for row in rows:
                after,t,owner,node,origin,body = row
                await response.write((json.dumps({'time':t,'owner':owner,'node':node,'origin':origin,'data':json.loads(body)},ensure_ascii=False)+'\n').encode())
        await response.write_eof()
        return response

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
        normalize(backup["state"])
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
                for removed_id in old_ids - new_ids:
                    await self.controller.gateway.remove(removed_id)
                for group in restored["groups"]:
                    await self.controller.gateway.listen(group["id"], group["port"])
                self.controller.identities.clear()
                self.controller.udp_schedule.clear()
                restored["quality_epoch"] = identity()
                self.controller.store.restore(restored, backup.get("events", []))
                if self.controller.traffic:
                    self.controller.traffic.restored = time.time()
                    self.controller.traffic.meta_dirty = True
                self.controller.history_epoch = restored["quality_epoch"]
                if self.controller.history:
                    self.controller.history.record("system",None,"restore",{"reason":"配置已恢复；保留现有质量历史，后续样本重新分段"})
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
    if (backup.get("format"), backup.get("state", {}).get("schema")) not in ((1, 1), (2, 2)):
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
    if backup["format"] == 2:
        validate_v2(state)
