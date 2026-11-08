import asyncio
import copy
import json
import hashlib
import time
from urllib.parse import urlparse

import aiohttp
from scheduler.core import MAX_SOURCE_BYTES, parse_subscription
from scheduler.probe import Observation, Prober
from scheduler.store import new_group
from scheduler.policy import normalize, identity, node_ref, eligible, in_scope, validate_scope
from scheduler.validation import validate_settings
from scheduler.http import read_limited
from scheduler.maintenance import marker_path, begin as begin_maintenance, finish as finish_maintenance


class Controller:
    def __init__(self, store, core, gateway, prober=None, clock=time.monotonic, wall=time.time):
        self.store, self.core, self.gateway = store, core, gateway
        normalize(self.store.state)
        self.prober, self.clock, self.wall = prober or Prober(), clock, wall
        self.lock = asyncio.Lock()
        self.identities = {}  # Monotonic deadlines are never restored from disk.
        self.tasks = []
        self.running = False
        self.candidate_cursor = {}
        self.udp_schedule = {}  # One bounded entry per group UID; never restored as fresh telemetry.
        self.selection_progress = {}
        self.selection_cancellations = {}
        self.core_error = None
        self.maintenance = marker_path(store.directory).exists()
        self.history = None
        self.traffic = None
        self.history_epoch = self.state.get('quality_epoch') or identity()
        self.session_epoch = identity()
        self.alert_queue = asyncio.Queue(maxsize=100)

    @property
    def state(self):
        return self.store.state

    def group(self, identity):
        return next(g for g in self.state["groups"] if g["id"] == str(identity))

    def occupied(self, exclude=None):
        return set().union(*(set(g["tcp_ips"]) for g in self.state["groups"] if g["id"] != exclude)) if any(g["id"] != exclude for g in self.state["groups"]) else set()

    def event(self, kind, message, group=None):
        self.store.event(kind, message, group)
        if kind in ("paused", "switch", "error") and not self.alert_queue.full():
            self.alert_queue.put_nowait({"time": self.wall(), "kind": kind, "group": group, "message": message})

    async def pause(self, group, reason):
        changed = group["status"] != "paused" or group["reason"] != reason
        # The generation is invalidated synchronously before any cancellation awaits.
        await self.gateway.revoke(group["id"])
        group.update(status="paused", reason=reason)
        if changed:
            self.record_quality(group, group.get("node"), "state", {"status":"unavailable","reason":reason})
            self.event("paused", reason, group["id"])
            self.store.save()

    async def pause_all(self, reason):
        for group in self.state["groups"]:
            await self.pause(group, reason)

    async def halt_source_edit(self, reason):
        self.maintenance = True
        # Revoke every lease before any audit/database write can fail on a full disk.
        await asyncio.gather(*(self.gateway.revoke(g["id"]) for g in self.state["groups"]))
        for group in self.state["groups"]:
            group.update(status="paused", reason=reason)
        self.store.save()

    def record_quality(self, group, node, origin, data):
        if self.history:
            obj = self.state['nodes'].get(node)
            ref = node_ref(self.state, obj) if obj else None
            settings = self.state['settings']
            config = hashlib.sha256(json.dumps([self.history_epoch,'tcp-us-independent-v1']+[settings[k] for k in ('tcp_check_urls','tcp_identity_urls','stun_servers','country_url')],sort_keys=True).encode()).hexdigest()[:16]
            self.history.record(group['uid'], ref, origin, dict(data, node_ref=ref, config=config, segment=self.session_epoch))

    async def start(self):
        for group in self.state["groups"]:
            await self.gateway.listen(group["id"], group["port"])
        self.running = True
        self.tasks = [asyncio.create_task(self.run()), asyncio.create_task(self.expire()), asyncio.create_task(self.alerts())]

    async def stop(self):
        self.running = False
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.pause_all("服务已停止")
        await self.gateway.close()
        await self.core.close()

    async def expire(self):
        while True:
            now = self.clock()
            for group in list(self.state["groups"]):
                if group["status"] == "active" and (now >= self.gateway.scheduler_deadline or now >= self.identities.get(group["id"], 0)):
                    # Do not acquire the controller lock: hung probes or jobs cannot defer revocation.
                    await self.pause(group, "调度授权到期" if now >= self.gateway.scheduler_deadline else "出口身份到期")
            await asyncio.sleep(0.25)

    async def alerts(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            while True:
                event = await self.alert_queue.get()
                settings = self.state["settings"]
                if not settings.get("alert_enabled") or not settings.get("alert_url"):
                    continue
                try:
                    async with session.post(settings["alert_url"], json=event, allow_redirects=False) as response:
                        if not 200 <= response.status < 300:
                            raise ValueError("webhook rejected")
                except Exception:
                    # Do not include the secret URL or remote response in logs/events.
                    self.store.event("alert_error", "告警投递失败（有界一次投递，不保证送达）")

    async def run(self):
        while True:
            try:
                async with self.lock:
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.core_error = "控制循环异常；等待重试，转发授权不会无限续期"
                self.event("error", self.core_error)
            await asyncio.sleep(self.state["settings"]["check_interval"])

    async def progress(self):
        await self.core.ready()
        self.core_error = None
        self.gateway.pulse(self.state["settings"]["health_grace"])

    async def observe(self, group, node, candidate=False):
        await self.progress()
        if candidate:
            upstream = await self.core.select(0, node)
        else:
            if not await self.core.mapping_valid(int(group["id"]), node):
                await self.pause(group, "后端映射变化，重新选择并验证")
                await self.core.select(int(group["id"]), node)
            upstream = self.core.host, 20000 + int(group["id"])
        observed_at = self.clock()
        settings = dict(self.state['settings'])
        udp_key = (node_ref(self.state, self.state['nodes'][node]), tuple(settings['stun_servers']),
                   settings['country_url'], settings['udp_check_interval'])
        previous_udp = self.udp_schedule.get(group['uid'])
        udp_due = not candidate and (previous_udp is None or previous_udp[0] != udp_key or observed_at >= previous_udp[1])
        if udp_due:
            self.udp_schedule[group['uid']] = (udp_key, observed_at + settings['udp_check_interval'])
        else:
            settings['stun_servers'] = []
        try:
            budget = max(1, min(45, self.state["settings"]["health_grace"] * 0.75))
            from scheduler.traffic import metered_probe
            context = self.traffic.context(self.state,group,self.state['nodes'][node],'candidate' if candidate else 'running') if self.traffic else None
            async def measured():
                async with metered_probe(upstream,self.traffic,context) as route:
                    return await self.prober.observe(route,settings)
            result = await asyncio.wait_for(measured(), budget)
        except asyncio.TimeoutError:
            result = Observation(reason="探测总超时，身份未刷新", udp_checked=False)
        # Test doubles and timeout results must follow the same explicit measurement schedule.
        if not udp_due:
            result.udp_checked = False
            result.udp_ok, result.udp_ips = False, set()
        result.observed_at = observed_at
        if not candidate:
            observed = result.tcp_ips
            previous = set(group["tcp_ips"])
            if (observed - previous or observed & self.occupied(group["id"]) or
                    any(result.countries.get(ip, "US") != "US" for ip in result.tcp_ips)):
                await self.pause(group, "观测出口变化/异常，清理会话并等待完整提交")
        data = result.public()
        data["time"] = self.wall()
        self.record_quality(group, node, "candidate" if candidate else "running", data)
        if node in self.state["nodes"]:
            self.state["nodes"][node]["last_probe"] = data
        return result

    async def accept(self, group, node, observation, revalidate=False, cancel=None):
        addresses = observation.tcp_ips
        if not eligible(self.state, group, self.state["nodes"][node]) or not observation.us or addresses & self.occupied(group["id"]):
            return False
        previous = set(group["tcp_ips"])
        changed = node != group["node"] or addresses != previous
        if changed:
            await self.pause(group, "出口/节点更新：清理旧会话后提交占用")
        upstream = await self.core.select(int(group["id"]), node)
        # Candidate mapping must itself be verified before receiving a business lease.
        if node != group["node"] or revalidate:
            verified = await self.observe(group, node)
            if not verified.us or verified.tcp_ips & self.occupied(group["id"]):
                return False
            observation = verified
        if cancel is not None and cancel.is_set():
            return False
        observed_at = observation.observed_at if observation.observed_at is not None else self.clock()
        deadline = observed_at + self.state["settings"]["identity_ttl"]
        if self.clock() >= deadline:
            await self.pause(group, "观测已超过身份有效期，等待下一次重验")
            return False
        udp_ref = node_ref(self.state, self.state['nodes'][node])
        if observation.udp_checked:
            group.update(udp_ips=sorted(observation.udp_ips), udp_reason=observation.udp_reason,
                         udp_observed_at=self.wall(), udp_node_ref=udp_ref)
        elif group.get('udp_node_ref') != udp_ref:
            group.update(udp_ips=[], udp_reason='此节点尚未进行 UDP 观测', udp_observed_at=None, udp_node_ref=udp_ref)
        group.update(node=node, tcp_ips=sorted(observation.tcp_ips),
                     verified_at=self.wall() - (self.clock() - observed_at), status="active", reason="TCP 美国出口已验证", failure_since=None, retry_step=0, next_retry_at=0)
        if changed or group["since"] is None:
            group["since"] = self.wall()
        try:
            self.store.save()
        except BaseException:
            await self.gateway.revoke(group["id"])
            group["status"] = "paused"
            raise
        self.identities[group["id"]] = deadline
        accounting = self.traffic.context(self.state,group,self.state['nodes'][node],'business') if self.traffic else None
        if self.gateway.grant(group["id"], upstream, deadline, accounting) is None:
            await self.pause(group, "旧会话仍在清理，等待下一轮重验")
            return False
        if changed:
            self.record_quality(group, node, "state", {"status":"available","reason":"独立美国出口验证通过"})
            self.event("switch", "已提交独立美国出口；旧会话不会恢复", group["id"])
        return True

    async def find_backup(self, group, explicit=None, manual=False):
        if group["pinned"] or group["paused"]:
            return False
        if not manual and self.wall() < group.get('next_retry_at', 0):
            return False
        if explicit and (explicit not in self.state['nodes'] or not eligible(self.state, group, self.state['nodes'][explicit])):
            raise ValueError('所选节点不在本组允许范围内，或节点/订阅已停用')
        candidates = [n for n in self.state['nodes'].values() if eligible(self.state, group, n)]
        order={ref:i for i,ref in enumerate(group['scope']['ids'])} if group['scope']['mode']=='nodes' else {}
        candidates.sort(key=lambda n: (order.get(node_ref(self.state,n),0),not n["hint_us"], n["priority"], n["id"]))
        if explicit:
            candidates = [n for n in candidates if n["id"] == explicit]
        elif candidates:
            cursor = self.candidate_cursor.get(group["id"], 0) % len(candidates)
            candidates = candidates[cursor:] + candidates[:cursor]
        country_failures = []
        last_failure = None
        for node in candidates[:3]:
            self.candidate_cursor[group["id"]] = self.candidate_cursor.get(group["id"], 0) + 1
            result = await self.observe(group, node["id"], candidate=True)
            last_failure = result.reason
            if any(ip in result.country_errors for ip in result.tcp_ips):
                country_failures.append(result.reason)
            if result.us and result.tcp_ips & self.occupied(group['id']):
                last_failure = "出口与其他分组重复，不能作为独立出口"
            if result.us and not result.tcp_ips & self.occupied(group["id"]):
                if await self.accept(group, node["id"], result, revalidate=True):
                    return True
                last_failure = (self.state['nodes'][node['id']].get('last_probe') or {}).get('reason', '业务入口复验未通过')
        step = min(group.get("retry_step", 0), 2)
        group.update(retry_step=min(step+1, 3), next_retry_at=self.wall() + (60, 300, 900)[step])
        reason = country_failures[0] if country_failures and len(country_failures) == len(candidates[:3]) else "没有已验证的独立美国备用出口，保留占用等待恢复"
        if len(country_failures) != len(candidates[:3]) and last_failure:
            reason += "；最近候选：" + last_failure
        await self.pause(group, reason)
        self.store.save()
        return False

    async def tick(self):
        if self.maintenance:
            self.core_error = "恢复/维护未完成，保持全部代理暂停"
            return
        await self.progress()
        # Refresh subscriptions on a bounded schedule. Failed fetches keep the old payload.
        for source in list(self.state["sources"]):
            if source["enabled"] and source.get("url") and self.wall() - source.get("refreshed_at", 0) >= source.get("interval", 21600) and self.wall() >= source.get("retry_at", 0):
                try:
                    await self.refresh_source(source)
                except Exception:
                    source["retry_at"] = self.wall() + 300
                    source["error"] = "刷新失败，保留旧有效配置"
                    self.store.save()
                await self.progress()
        groups = list(self.state["groups"])
        observations = {}
        for group in groups:
            if group["pin_until"] and self.wall() >= group["pin_until"]:
                group.update(pinned=False, pin_until=None)
                self.store.save()
            if group["uid"] in self.selection_progress:
                node=self.state['nodes'].get(group['node'])
                if node and group['status']=='active' and eligible(self.state,group,node):
                    result=await self.observe(group,node['id'])
                    if result.us:
                        await self.accept(group,node['id'],result)
                    elif self.clock() >= self.identities.get(group['id'],0):
                        await self.pause(group,'出口身份到期；继续独立连通探测')
                continue
            if group["paused"]:
                await self.pause(group, "手动暂停")
                continue
            if self.wall() < group.get("next_retry_at", 0):
                if group["status"] == "active":
                    await self.pause(group, "等待下一次恢复检查")
                continue
            node = self.state["nodes"].get(group["node"])
            if not node or not eligible(self.state, group, node):
                await self.pause(group, "节点不可用或订阅已停用")
                observations[group["id"]] = None
                continue
            result = await self.observe(group, node["id"])
            observations[group["id"]] = result
        # Detect overlapping fresh observations before accepting any one side.
        collisions = set()
        for i, a in enumerate(groups):
            ar = observations.get(a["id"])
            if not ar:
                continue
            for b in groups[i+1:]:
                br = observations.get(b["id"])
                if br and ar.tcp_ips & br.tcp_ips:
                    anew = ar.tcp_ips
                    bnew = br.tcp_ips
                    aold = set(a["tcp_ips"])
                    bold = set(b["tcp_ips"])
                    overlap = anew & bnew
                    if overlap - aold and not overlap - bold:
                        collisions.add(a["id"])
                    elif overlap - bold and not overlap - aold:
                        collisions.add(b["id"])
                    else:
                        collisions.update((a["id"], b["id"]))
        for identity in collisions:
            await self.pause(self.group(identity), "组间出口冲突，等待串行重验")
        for group in groups:
            if group["uid"] in self.selection_progress or group["paused"] or self.wall() < group.get("next_retry_at", 0):
                continue
            result = observations.get(group["id"])
            needs_backup = result is None or group["id"] in collisions
            if result:
                known_bad = any(result.countries.get(ip, "US") != "US" for ip in result.tcp_ips)
                known_conflict = bool(result.tcp_ips & self.occupied(group["id"]))
                if known_bad or known_conflict:
                    await self.pause(group, "已观测非美国出口" if known_bad else "已观测组间出口冲突")
                    needs_backup = True
                if result.failure_confirmed:
                    if group["failure_since"] is None:
                        group["failure_since"] = self.clock()
                    needs_backup = needs_backup or self.clock() - group["failure_since"] >= self.state["settings"]["failure_seconds"]
                elif result.tcp_ok:
                    group["failure_since"] = None
                if result.complete:
                    conflict = group["id"] in collisions or bool(result.tcp_ips & self.occupied(group["id"]))
                    if not result.us or conflict:
                        await self.pause(group, "非美国出口" if not result.us else "组间出口冲突")
                        needs_backup = True
                    elif group["id"] not in collisions and await self.accept(group, group["node"], result):
                        continue
                elif self.clock() >= self.identities.get(group["id"], 0):
                    await self.pause(group, "出口身份到期；继续独立连通探测")
            if needs_backup and not group["pinned"]:
                await self.find_backup(group)
            elif group["pinned"] and (needs_backup or group["status"] != "active"):
                await self.pause(group, "钉住节点不合格，暂停且不切换")
        self.store.save()
        await self.progress()

    async def refresh_source(self, source, content=None, stored_source=None):
        if content is None:
            parsed = urlparse(source["url"])
            if parsed.scheme != "https":
                raise ValueError("订阅 URL 必须使用 HTTPS；本地可直接导入文本")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(source["url"], allow_redirects=False, headers={"User-Agent": "clash.meta"}) as response:
                    if response.status != 200:
                        raise ValueError("订阅下载失败")
                    raw = await read_limited(response, MAX_SOURCE_BYTES)
                    if len(raw) > MAX_SOURCE_BYTES:
                        raise ValueError("订阅超过 4 MiB")
                    content = raw.decode("utf-8-sig")
        if len(content.encode()) > MAX_SOURCE_BYTES:
            raise ValueError("订阅超过 4 MiB")
        nodes, payload = parse_subscription(content, source["slot"])
        old = {k: v for k, v in self.state["nodes"].items() if v["source"] == source["slot"]}
        # Block affected mappings before a provider can remove/change the selected node.
        for group in self.state["groups"]:
            if group["node"] in old and group["node"] not in nodes:
                await self.pause(group, "订阅节点改变或消失，重验前暂停")
        for key, node in nodes.items():
            if key in old:
                node.update(disabled=old[key]["disabled"], priority=old[key]["priority"], last_probe=old[key]["last_probe"])
        previous_source = dict(stored_source) if stored_source is not None else None
        previous_payload = None
        if stored_source is not None:
            previous_payload = (self.core.directory / "providers" / ("s%02d.yaml" % source["slot"])).read_text()
            # Provider and SQLite live on separate volumes. A crash during the edit must
            # keep the next process closed, rather than run mismatched URL/provider state.
            begin_maintenance(self.store.directory, "订阅编辑未完整提交，需恢复一致备份")
        try:
            await self.core.update_source(source["slot"], payload)
            for key in old:
                del self.state["nodes"][key]
            self.state["nodes"].update(nodes)
            source.update(refreshed_at=self.wall(), retry_at=0, error=None)
            if stored_source is not None:
                stored_source.update(source)
            self.store.save()  # New metadata and nodes are one SQLite commit.
        except BaseException:
            if stored_source is not None:
                self.state["nodes"] = {k: v for k, v in self.state["nodes"].items() if v["source"] != source["slot"]}
                self.state["nodes"].update(old)
                stored_source.clear()
                stored_source.update(previous_source)
                try:
                    await self.core.update_source(source["slot"], previous_payload)
                    self.store.save()
                    finish_maintenance(self.store.directory)
                except BaseException:
                    await self.halt_source_edit("订阅编辑回退未完成，保持维护暂停")
            raise
        if stored_source is not None:
            try:
                finish_maintenance(self.store.directory)
            except BaseException:
                await self.halt_source_edit("订阅编辑提交标记未清除，保持维护暂停")
                raise
        try:
            self.event("source", "订阅已校验并刷新，保持未变化的在用映射")
        except Exception:
            pass  # The edit is already committed; an audit-write error cannot claim rollback.

    def group_outcome(self, group, kind, outcome, message, selection=None):
        result = {'kind':kind, 'group':group['id'], 'group_uid':group['uid'], 'outcome':outcome,
                  'message':message, 'finished':self.wall()}
        if selection is not None:
            result['selection'] = copy.deepcopy(selection)
        group['last_action'] = result
        self.store.save()
        return copy.deepcopy(result)

    async def manual_search(self, kind, data, cancel=None, progress=None, scope_saved=False):
        """One candidate per lock acquisition; scheduled lease validation can run between candidates."""
        cancel = cancel or asyncio.Event()
        evaluating = kind == 'group_evaluate'
        async with self.lock:
            if self.maintenance:
                raise ValueError('维护期间不能选择节点')
            group = self.group(data['group'])
            uid = group['uid']
            if data.get('group_uid',uid)!=uid:
                raise ValueError('分组已被删除或重建，请刷新后重新操作')
            if uid in self.selection_progress:
                raise ValueError('此分组正在查找节点，请等待或先停止查找')
            if group['paused'] or group['pinned']:
                if scope_saved:
                    return self.group_outcome(group,kind,'saved_interrupted','可用节点清单已保存；分组已暂停或固定，未启动自动查找')
                raise ValueError('请先启用分组并允许自动换节点')
            explicit = data.get('node')
            if explicit and (explicit not in self.state['nodes'] or not eligible(self.state,group,self.state['nodes'][explicit])):
                raise ValueError('所选节点不在本组允许范围内，或节点/订阅已停用')
            nodes = [n for n in self.state['nodes'].values() if eligible(self.state,group,n)]
            order={ref:i for i,ref in enumerate(group['scope']['ids'])} if group['scope']['mode']=='nodes' else {}
            nodes.sort(key=lambda n:(order.get(node_ref(self.state,n),0),not n['hint_us'],n['priority'],n['id']))
            if explicit:
                nodes = [self.state['nodes'][explicit]]
            refs = [(n['id'],node_ref(self.state,n)) for n in nodes[:100]]
            scope, settings, epoch = copy.deepcopy(group['scope']), copy.deepcopy(self.state['settings']), self.history_epoch
            selection = {'kind':kind,'requested':self.state['nodes'][explicit]['name'] if explicit else '自动选择',
                         'total':len(nodes),'us_hint_count':sum(bool(n['hint_us']) for n in nodes),'limit':len(refs),'checked':0,'attempts':[],
                         'started':self.wall(),'current':None,'stage':'开始查找'}
            self.group_outcome(group,kind,'checking',('可用清单已保存；' if scope_saved else '')+'正在查找并验证节点',selection)
            self.selection_progress[uid] = selection
            self.selection_cancellations[uid] = cancel
        outcome, message = 'not_applied', '未找到通过全部验证且与其他分组出口不重复的节点'
        try:
            for index,(node_id,ref) in enumerate(refs):
                async with self.lock:
                    current = next((g for g in self.state['groups'] if g['uid']==uid),None)
                    node = self.state['nodes'].get(node_id)
                    if cancel.is_set():
                        outcome,message='cancelled','已停止查找；已完成的检查结果保留'
                        break
                    if (self.maintenance or current is None or current['paused'] or current['pinned'] or
                            current['scope']!=scope or self.state['settings']!=settings or self.history_epoch!=epoch or
                            not node or node_ref(self.state,node)!=ref or not eligible(self.state,current,node)):
                        outcome,message='interrupted','分组、订阅或检测配置已变化，已停止本次查找；请按新配置重新开始'
                        break
                    group = current
                    selection.update(current=node['name'],stage='正在验证',index=index+1)
                    if progress:progress(f"正在验证 {index+1}/{len(refs)}：{node['name']}；已检查 {selection['checked']} 个")
                    result = await self.observe(group,node_id,candidate=True)
                    conflict = bool(result.tcp_ips&self.occupied(group['id']))
                    reason = '出口与其他分组重复，不能作为独立出口' if conflict else result.reason
                    attempt = {'node_ref':ref,'name':node['name'],'reason':reason,
                               'tcp_ok':result.tcp_ok,'udp_ok':result.udp_ok,'udp_reason':result.udp_reason,'identity_errors':result.identity_errors,
                               'country_errors':result.country_errors,'conflict':conflict,'applied':False,'eligible':result.us and not conflict,'time':self.wall(),
                               'http_ms':max((s['ms'] for s in result.samples if s['method']=='http-get-headers' and s['ok'] and s.get('ms') is not None),default=None),
                               'http_failed':sum(not s['ok'] for s in result.samples if s['method']=='http-get-headers'),
                               'udp_ms':max((s['ms'] for s in result.samples if s['method']=='stun-with-retry' and s['ok'] and s.get('ms') is not None),default=None)}
                    if evaluating and self.history:
                        try:
                            history=await asyncio.to_thread(self.history.query,'',24,ref)
                            expected=hashlib.sha256(json.dumps([self.history_epoch,'tcp-us-independent-v1']+[settings[k] for k in ('tcp_check_urls','tcp_identity_urls','stun_servers','country_url')],sort_keys=True).encode()).hexdigest()[:16]
                            prior=[s for s in history['series'] if s['config']==expected and s['method']=='http-get-headers' and s['origin'] in ('running','candidate')]
                            prior.sort(key=lambda s:(s['origin']!='running',-(s['points'][-1]['time'] if s['points'] else 0)))
                            if prior:
                                attempt['history']={k:prior[0][k] for k in ('target','origin','attempts','successes','mean_ms','variation_ms')}
                        except Exception:
                            attempt['history_error']='历史暂不可读'
                    selection['attempts'].append(attempt);selection['checked']+=1
                    if cancel.is_set():
                        outcome,message='cancelled','已停止查找；本次检查结束，未应用该候选'
                        break
                    if not evaluating and result.us and not conflict:
                        previous_node=group.get('node')
                        previous_lease=self.gateway.leases.get(group['id'])
                        previous_available=bool(previous_node and previous_lease and self.gateway.allowed(group['id'],previous_lease.generation))
                        selection['stage']='正在复验业务入口'
                        if progress:progress(f"{node['name']} 候选验证通过，正在复验业务入口")
                        if await self.accept(group,node_id,result,revalidate=True,cancel=cancel):
                            attempt.update(applied=True,reason='已通过完整验证并应用')
                            outcome,message='applied',f"已应用节点：{node['name']}；业务入口可用"
                            selection.update(current=None,stage='已结束',finished=self.wall())
                            if scope_saved:
                                outcome,message='saved_available','可用节点清单已保存；'+message
                            # Commit the historical operation result with the actual grant, before
                            # releasing the lock to a later pause/edit operation.
                            completed=self.group_outcome(group,kind,outcome,message,selection)
                            self.event('operation',message,group['id'])
                            return completed
                        if previous_available and previous_node in self.state['nodes'] and eligible(self.state,group,self.state['nodes'][previous_node]):
                            selection['stage']='正在重新验证并恢复原节点'
                            if progress:progress('候选未应用，正在重新验证并恢复原节点')
                            await self.core.select(int(group['id']),previous_node)
                            restored=await self.observe(group,previous_node)
                            attempt['previous_restored']=await self.accept(group,previous_node,restored)
                        if cancel.is_set():
                            outcome,message='cancelled','已停止查找，未提交候选节点'
                            break
                        attempt['reason']=(node.get('last_probe') or {}).get('reason','业务入口复验未通过')
                    self.store.save()
                # Yield after each bounded candidate; a pending scheduler or edit gets its lock turn.
                await asyncio.sleep(0)
            async with self.lock:
                current=next((g for g in self.state['groups'] if g['uid']==uid),None)
                if current is None:
                    return {'kind':kind,'outcome':'interrupted','message':'分组已删除，本次查找已停止'}
                if (current['paused'] or current['pinned'] or current['scope']!=scope or self.state['settings']!=settings or self.history_epoch!=epoch):
                    outcome,message='interrupted','分组或检测配置已变化，按当前配置生效；本次查找已停止'
                if outcome=='applied':
                    lease=self.gateway.leases.get(current['id'])
                    if not (lease and self.gateway.allowed(current['id'],lease.generation)):
                        outcome,message='interrupted','候选验证后授权状态已变化，请查看当前分组状态'
                elif cancel.is_set() and outcome=='not_applied':
                    outcome,message='cancelled','已停止查找；未应用新的候选节点'
                selection.update(current=None,stage='已结束',finished=self.wall())
                if evaluating and outcome=='not_applied':
                    qualified=[a for a in selection['attempts'] if a['eligible']]
                    qualified.sort(key=lambda a:(a['http_failed'],a['http_ms'] if a['http_ms'] is not None else float('inf')))
                    selection['recommended']=[a['node_ref'] for a in qualified if a['http_ms'] is not None and a['http_failed']==0][:3]
                    selection['qualified']=len(qualified)
                    outcome='evaluated'
                    message=f"已评估 {selection['checked']}/{selection['total']} 个候选，其中 {len(qualified)} 个通过本次出口检查；推荐 {len(selection['recommended'])} 个，待你确认保存节点池"
                    current['last_evaluation']=copy.deepcopy(selection)
                if outcome=='not_applied':
                    if selection['checked'] < selection['total']:
                        message += f"；本次检查 {selection['checked']}/{selection['total']} 个，达到单次100个上限，未检查完全部范围"
                    else:
                        message += f"；已检查全部 {selection['checked']} 个候选"
                    lease=self.gateway.leases.get(current['id'])
                    available=bool(lease and self.gateway.allowed(current['id'],lease.generation))
                    if available:
                        message += '；未更换，继续使用原节点'
                    else:
                        detail=selection['attempts'][-1]['reason'] if selection['attempts'] else '允许范围内没有启用的候选节点'
                        await self.pause(current,message+'；最近候选：'+detail)
                        step=min(current.get('retry_step',0),2)
                        current.update(retry_step=min(step+1,3),next_retry_at=self.wall()+(60,300,900)[step])
                if scope_saved:
                    outcome='saved_available' if outcome=='applied' else 'saved_'+outcome
                    message='可用节点清单已保存；'+message
                result=self.group_outcome(current,kind,outcome,message,selection)
                self.event('operation',message,current['id'])
                return result
        except Exception:
            async with self.lock:
                current=next((g for g in self.state['groups'] if g['uid']==uid),None)
                if current is None:
                    raise
                selection.update(current=None,stage='已结束',finished=self.wall())
                return self.group_outcome(current,kind,'saved_error' if scope_saved else 'error',('可用节点清单已保存；' if scope_saved else '')+'查找遇到异常，已停止；已完成的检查结果如下，请查看当前入口状态',selection)
        finally:
            self.selection_progress.pop(uid,None)
            self.selection_cancellations.pop(uid,None)

    async def action(self, kind, data, cancel=None, progress=None):
        if kind in ('group_switch','group_retry','group_evaluate'):
            return await self.manual_search(kind,data,cancel,progress)
        result = await self._action(kind,data)
        if isinstance(result,dict) and result.pop('needs_search',False):
            return await self.manual_search(kind,data,cancel,progress,scope_saved=True)
        return result

    async def _action(self, kind, data):
        async with self.lock:
            if self.maintenance:
                raise ValueError("恢复未完成，仅允许重新恢复有效备份")
            if kind == "source_add":
                used = {s["slot"] for s in self.state["sources"]}
                slot = next((i for i in range(1, self.core.source_capacity + 1) if i not in used), None)
                if slot is None:
                    raise ValueError("订阅槽位已满，请维护重启扩大容量")
                source = {"slot": slot, "uid": identity(), "name": str(data.get("name", "订阅"))[:100], "url": data.get("url", ""),
                          "enabled": True, "interval": 21600, "refreshed_at": 0, "error": None}
                await self.refresh_source(source, data.get("content") or None)
                self.state["sources"].append(source)
            elif kind.startswith("source_"):
                source = next(s for s in self.state["sources"] if s["slot"] == int(data["slot"]))
                if kind == "source_update":
                    name, url = data.get("name", source["name"]), data.get("url", source["url"])
                    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
                        raise ValueError("订阅名称须为 1–100 个字符")
                    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 or c.isspace() for c in url.strip()):
                        raise ValueError("订阅 URL 无效")
                    url = url.strip()
                    try:
                        parsed = urlparse(url)
                        valid_url = not url or (parsed.scheme == "https" and parsed.hostname and (parsed.port is None or 1 <= parsed.port <= 65535))
                    except ValueError:
                        valid_url = False
                    if not valid_url:
                        raise ValueError("订阅 URL 必须是有效的 HTTPS 地址")
                    content = data.get("content")
                    if content is not None and not isinstance(content, str):
                        raise ValueError("订阅文本无效")
                    content = content if content and content.strip() else None
                    updated = dict(source, name=name.strip(), url=url)
                    refresh = content is not None or (url and url != source["url"])
                    if refresh:
                        # Do not replace the stored identity or credentials before validation succeeds.
                        try:
                            await self.refresh_source(updated, content, stored_source=source)
                        except Exception:
                            if self.maintenance:
                                raise ValueError("订阅编辑未完整提交，全部分组保持维护暂停；请恢复一致备份") from None
                            raise ValueError("订阅更新失败，已保留原地址和节点；请检查 URL 或导入内容") from None
                    elif not url:
                        updated.update(retry_at=0, error=None)
                    if not refresh:
                        previous = dict(source)
                        source.update(updated)
                        try:
                            self.store.save()
                        except BaseException:
                            source.clear()
                            source.update(previous)
                            raise
                        try:
                            self.event("operation", "订阅信息已更新")
                        except Exception:
                            pass
                        return  # Renaming or removing the URL must not trigger provider updates/probes.
                    return  # Refresh already committed metadata and nodes together; scheduler resumes normally.
                elif kind == "source_refresh":
                    await self.refresh_source(source, data.get("content") or None)
                elif kind in ("source_toggle", "source_delete"):
                    for group in self.state["groups"]:
                        node = self.state["nodes"].get(group["node"])
                        if node and node["source"] == source["slot"]:
                            await self.pause(group, "订阅状态改变")
                    if kind == "source_delete":
                        self.state["sources"].remove(source)
                        self.state["nodes"] = {k: v for k, v in self.state["nodes"].items() if v["source"] != source["slot"]}
                    else:
                        source["enabled"] = not source["enabled"]
                else:
                    raise ValueError("未知订阅操作")
            elif kind == "node_update":
                node = self.state["nodes"][data["node"]]
                node["disabled"] = bool(data.get("disabled", node["disabled"]))
                node["priority"] = max(0, min(10000, int(data.get("priority", node["priority"]))))
                if node["disabled"]:
                    for group in self.state["groups"]:
                        if group["node"] == node["id"]:
                            await self.pause(group, "节点已禁用")
            elif kind == "group_add":
                used = {int(g["id"]) for g in self.state["groups"]}
                slot = next((i for i in range(1, self.core.capacity + 1) if i not in used), None)
                if slot is None:
                    raise ValueError("分组槽位已满，请维护重启扩大容量")
                group = new_group(slot, str(data.get("name") or "出口组 %s" % slot)[:100])
                await self.gateway.listen(group["id"], group["port"])
                self.state["groups"].append(group)
            elif kind.startswith("group_"):
                group = self.group(data["group"])
                if data.get("group_uid",group["uid"])!=group["uid"]:
                    raise ValueError("分组已被删除或重建，请刷新后重新操作")
                if group["uid"] in self.selection_progress and kind not in ("group_pause","group_delete"):
                    raise ValueError("此分组正在查找节点，请先停止查找再修改配置")
                if kind == "group_scope":
                    scope = validate_scope(data.get('scope'), self.state)
                    node = self.state['nodes'].get(group['node'])
                    changed = node and not in_scope(self.state, dict(group, scope=scope), node)
                    if changed and data.get('confirm_disconnect') is not True:
                        raise ValueError('当前节点将被移出，请先确认断开已有连接')
                    if changed:
                        await self.pause(group, '当前节点已移出允许范围')
                        group['node'] = None
                    previous = group['scope']
                    group['scope'] = scope
                    try:
                        self.store.save()
                    except BaseException:
                        group['scope'] = previous
                        # Exclusion already revoked before the write. Retained-node
                        # saves must not disrupt its valid existing authorization.
                        raise
                    self.event('operation', '可用节点清单已保存', group['id'])
                    if scope != previous and not group['paused'] and not group['pinned'] and (changed or group['status']!='active'):
                        result=self.group_outcome(group,kind,'saved_pending','可用节点清单已保存；即将按新范围查找节点')
                        result['needs_search']=True
                        return result
                    message='可用节点清单已保存；'+('分组已手动暂停，未启动检查' if group['paused'] else '当前节点已固定，未启动自动查找' if group['pinned'] else '继续使用当前节点' if group['status']=='active' else '范围未变化，保留原重试安排')
                    return self.group_outcome(group,kind,'saved',message)
                elif kind == "group_pause":
                    group["paused"] = True
                    await self.pause(group, "手动暂停")
                elif kind == "group_resume":
                    group["paused"] = False
                elif kind == "group_pin":
                    group["pinned"] = bool(data["pinned"])
                    seconds = int(data.get("seconds", 0))
                    group["pin_until"] = self.wall() + seconds if group["pinned"] and seconds > 0 else None
                elif kind == "group_delete":
                    await self.gateway.remove(group["id"])
                    self.state["groups"].remove(group)
                    self.udp_schedule.pop(group['uid'], None)
                elif kind == "group_check":
                    if not group['paused'] and not group['pinned'] and group.get('next_retry_at'):
                        await self.find_backup(group, manual=True)
                else:
                    raise ValueError("未知分组操作")
            elif kind == "settings":
                updated = copy.deepcopy(self.state["settings"])
                for key in ("check_interval", "udp_check_interval", "failure_seconds", "health_grace", "identity_ttl", "probe_timeout",
                            "tcp_identity_urls", "tcp_check_urls", "stun_servers", "country_url", "alert_url", "alert_enabled"):
                    if key in data:
                        updated[key] = data[key]
                validate_settings(updated)
                await self.pause_all("配置变更后重验")
                self.identities.clear()
                self.state["settings"] = updated
            else:
                raise ValueError("未知操作")
            self.store.save()
            self.event("operation", "管理操作：" + kind, data.get("group"))
            await self.tick()

    def public(self):
        state = copy.deepcopy(self.state)
        for source in state["sources"]:
            source["has_url"] = bool(source.pop("url", ""))
        for node in state['nodes'].values():
            node['ref'] = node_ref(self.state, node)
            node['last_test'] = self.history.latest.get(node['ref']) if self.history else None
        state["settings"]["alert_configured"] = bool(state["settings"].pop("alert_url", ""))
        state["health"] = {"scheduler_valid": self.clock() < self.gateway.scheduler_deadline,
                           "error": self.core_error, "maintenance": self.maintenance, "coverage": "仅配置探测目标所见出口；未覆盖目的地/地址族未知"}
        for group in state["groups"]:
            candidates = [n for n in self.state['nodes'].values() if eligible(self.state, group, n)]
            group['selection_progress'] = copy.deepcopy(self.selection_progress.get(group['uid']))
            group['candidate_count'] = len(candidates)
            group['candidate_refs'] = [node_ref(self.state, n) for n in candidates]
            group['endpoint'] = "proxy-egress-manager:%s" % group["port"]
            lease = self.gateway.leases.get(group["id"])
            group["lease_valid"] = bool(lease and self.gateway.allowed(group["id"], lease.generation))
        return state
