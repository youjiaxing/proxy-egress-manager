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
        self.core_error = None
        self.maintenance = marker_path(store.directory).exists()
        self.history = None
        self.history_epoch = self.state.get('quality_epoch') or identity()
        self.session_epoch = identity()
        self.alert_queue = asyncio.Queue(maxsize=100)

    @property
    def state(self):
        return self.store.state

    def group(self, identity):
        return next(g for g in self.state["groups"] if g["id"] == str(identity))

    def occupied(self, exclude=None):
        return set().union(*(set(g["tcp_ips"]) | set(g["udp_ips"]) for g in self.state["groups"] if g["id"] != exclude)) if any(g["id"] != exclude for g in self.state["groups"]) else set()

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
            config = hashlib.sha256(json.dumps([self.history_epoch]+[settings[k] for k in ('tcp_check_urls','tcp_identity_urls','stun_servers','country_url')],sort_keys=True).encode()).hexdigest()[:16]
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
        try:
            budget = max(1, min(45, self.state["settings"]["health_grace"] * 0.75))
            result = await asyncio.wait_for(self.prober.observe(upstream, self.state["settings"]), budget)
        except asyncio.TimeoutError:
            result = Observation(reason="探测总超时，身份未刷新")
        result.observed_at = observed_at
        if not candidate:
            observed = result.tcp_ips | result.udp_ips
            previous = set(group["tcp_ips"]) | set(group["udp_ips"])
            if (observed - previous or observed & self.occupied(group["id"]) or
                    any(code != "US" for code in result.countries.values())):
                await self.pause(group, "观测出口变化/异常，清理会话并等待完整提交")
        data = result.public()
        data["time"] = self.wall()
        self.record_quality(group, node, "candidate" if candidate else "running", data)
        if node in self.state["nodes"]:
            self.state["nodes"][node]["last_probe"] = data
        return result

    async def accept(self, group, node, observation, revalidate=False):
        addresses = observation.tcp_ips | observation.udp_ips
        if not eligible(self.state, group, self.state["nodes"][node]) or not observation.us or addresses & self.occupied(group["id"]):
            return False
        previous = set(group["tcp_ips"]) | set(group["udp_ips"])
        changed = node != group["node"] or addresses != previous or set(group["tcp_ips"]) != observation.tcp_ips or set(group["udp_ips"]) != observation.udp_ips
        if changed:
            await self.pause(group, "出口/节点更新：清理旧会话后提交占用")
        upstream = await self.core.select(int(group["id"]), node)
        # Candidate mapping must itself be verified before receiving a business lease.
        if node != group["node"] or revalidate:
            verified = await self.observe(group, node)
            if not verified.us or (verified.tcp_ips | verified.udp_ips) & self.occupied(group["id"]):
                return False
            observation = verified
        observed_at = observation.observed_at if observation.observed_at is not None else self.clock()
        deadline = observed_at + self.state["settings"]["identity_ttl"]
        if self.clock() >= deadline:
            await self.pause(group, "观测已超过身份有效期，等待下一次重验")
            return False
        group.update(node=node, tcp_ips=sorted(observation.tcp_ips), udp_ips=sorted(observation.udp_ips),
                     verified_at=self.wall() - (self.clock() - observed_at), status="active", reason="双协议美国出口已验证", failure_since=None, retry_step=0, next_retry_at=0)
        if changed or group["since"] is None:
            group["since"] = self.wall()
        try:
            self.store.save()
        except BaseException:
            await self.gateway.revoke(group["id"])
            group["status"] = "paused"
            raise
        self.identities[group["id"]] = deadline
        if self.gateway.grant(group["id"], upstream, deadline) is None:
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
        candidates.sort(key=lambda n: (not n["hint_us"], n["priority"], n["id"]))
        if explicit:
            candidates = [n for n in candidates if n["id"] == explicit]
        elif candidates:
            cursor = self.candidate_cursor.get(group["id"], 0) % len(candidates)
            candidates = candidates[cursor:] + candidates[:cursor]
        for node in candidates[:3]:
            self.candidate_cursor[group["id"]] = self.candidate_cursor.get(group["id"], 0) + 1
            result = await self.observe(group, node["id"], candidate=True)
            if result.us and not (result.tcp_ips | result.udp_ips) & self.occupied(group["id"]):
                if await self.accept(group, node["id"], result, revalidate=True):
                    return True
        step = min(group.get("retry_step", 0), 2)
        group.update(retry_step=min(step+1, 3), next_retry_at=self.wall() + (60, 300, 900)[step])
        await self.pause(group, "没有已验证的独立美国备用出口，保留占用等待恢复")
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
                if br and (ar.tcp_ips | ar.udp_ips) & (br.tcp_ips | br.udp_ips):
                    anew = ar.tcp_ips | ar.udp_ips
                    bnew = br.tcp_ips | br.udp_ips
                    aold = set(a["tcp_ips"]) | set(a["udp_ips"])
                    bold = set(b["tcp_ips"]) | set(b["udp_ips"])
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
            if group["paused"] or self.wall() < group.get("next_retry_at", 0):
                continue
            result = observations.get(group["id"])
            needs_backup = result is None or group["id"] in collisions
            if result:
                known_bad = any(country != "US" for country in result.countries.values())
                known_conflict = bool((result.tcp_ips | result.udp_ips) & self.occupied(group["id"]))
                if known_bad or known_conflict:
                    await self.pause(group, "已观测非美国出口" if known_bad else "已观测组间出口冲突")
                    needs_backup = True
                if result.failure_confirmed:
                    if group["failure_since"] is None:
                        group["failure_since"] = self.clock()
                    needs_backup = needs_backup or self.clock() - group["failure_since"] >= self.state["settings"]["failure_seconds"]
                elif result.tcp_ok and result.udp_ok:
                    group["failure_since"] = None
                if result.complete:
                    conflict = group["id"] in collisions or bool((result.tcp_ips | result.udp_ips) & self.occupied(group["id"]))
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

    async def action(self, kind, data):
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
                    if changed and not group['paused'] and not group['pinned']:
                        await self.find_backup(group, manual=True)
                    return
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
                elif kind == "group_switch":
                    if group["pinned"] or group["paused"]:
                        raise ValueError("请先解除钉住/手动暂停")
                    await self.find_backup(group, data.get("node"), manual=True)
                elif kind == "group_retry":
                    if group['paused'] or group['pinned']:
                        raise ValueError('请先启用分组并允许自动换节点')
                    await self.find_backup(group, manual=True)
                elif kind == "group_check":
                    if not group['paused'] and not group['pinned'] and group.get('next_retry_at'):
                        await self.find_backup(group, manual=True)
                else:
                    raise ValueError("未知分组操作")
            elif kind == "settings":
                updated = copy.deepcopy(self.state["settings"])
                for key in ("check_interval", "failure_seconds", "health_grace", "identity_ttl", "probe_timeout",
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
            group['candidate_count'] = len(candidates)
            group['candidate_refs'] = [node_ref(self.state, n) for n in candidates]
            group['endpoint'] = "proxy-egress-manager:%s" % group["port"]
            group["lease_valid"] = self.gateway.allowed(group["id"], self.gateway.leases[group["id"]].generation)
        return state
