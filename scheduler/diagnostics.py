"""User-triggered probes on a distinct Mihomo process. No lease or scheduler mutations."""
import asyncio
import copy
import hashlib
import json
import socket
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp_socks import ProxyConnector
import yaml
from scheduler.core import Core, SubscriptionLoader, atomic_write
from scheduler.policy import node_ref, source_for
from scheduler.probe import COOKIE, DatagramReply, stun_request, stun_response
from gateway.socks import open_socks, pack_address, unpack_datagram
import os


def diagnostic_core(directory, secret, api='http://diagnostic-core:9090', host='diagnostic-core'):
    return Core(directory, base_url=api, host=host, secret=hashlib.sha256(('diagnostics:'+secret).encode()).hexdigest(), capacity=1, source_capacity=1)


async def udp_sample(upstream, target):
    """One UDP association; 20 distinct transactions, no retransmission."""
    control = transport = None
    sent, received, durations = 0, 0, []
    try:
        host, port = target.rsplit(':', 1)
        _, control, relay = await open_socks(*upstream, 3, '0.0.0.0', 0, timeout=8)
        if relay[0] in ('0.0.0.0','::'):
            relay = upstream[0], relay[1]
        address = (await asyncio.get_running_loop().getaddrinfo(*relay, type=socket.SOCK_DGRAM))[0][4][:2]
        transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(DatagramReply, remote_addr=address)
        for _ in range(20):
            transaction = os.urandom(12)
            payload = b'\0\0\0'+pack_address(host, int(port))+stun_request(transaction)
            start = time.monotonic()
            transport.sendto(payload)
            sent += 1
            deadline = start+2
            try:
                while time.monotonic() < deadline:
                    packet = await asyncio.wait_for(protocol.queue.get(), max(.001, deadline-time.monotonic()))
                    try:
                        stun_response(unpack_datagram(packet)[2], transaction)
                        received += 1
                        durations.append(round((time.monotonic()-start)*1000,2))
                        break
                    except ValueError:
                        continue
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(.2)
        return {'sent':sent,'received':received,'unanswered_pct':round((sent-received)/sent*100,1),
                'mean_ms':round(sum(durations)/received,1) if received else None,
                'min_ms':min(durations) if durations else None,'max_ms':max(durations) if durations else None,
                'target':target,'complete':True}
    finally:
        if transport: transport.close()
        if control: control.close()


class Diagnostics:
    def __init__(self, controller, core, history):
        self.controller, self.core, self.history = controller, core, history
        self.lock = asyncio.Lock()
        self.inflight = set()

    async def snapshot(self, ref, group_id=None):
        c = self.controller
        async with c.lock:
            if c.maintenance:
                raise ValueError('维护期间不能测试')
            node = next((n for n in c.state['nodes'].values() if node_ref(c.state,n)==ref), None)
            if not node or node['disabled']:
                raise ValueError('节点已改变或禁用，请刷新后重试')
            source = source_for(c.state,node)
            if not source['enabled']:
                raise ValueError('请先启用该订阅')
            group = c.group(group_id) if group_id else None
            if group and group['node'] != node['id']:
                raise ValueError('分组当前节点已改变，请重新打开详情')
            path = c.core.directory/'providers'/('s%02d.yaml'%source['slot'])
            # Generated provider has no aliases. Read only a trusted local file; never fetch a subscription.
            proxies = yaml.load(path.read_text(), Loader=SubscriptionLoader)['proxies']
            cfg = next((p for p in proxies if p['name']==node['id']), None)
            if cfg is None: raise ValueError('节点配置尚未就绪')
            return {'config':copy.deepcopy(cfg),'node_ref':ref,'owner':group['uid'] if group else 'node:'+ref,
                    'settings':copy.deepcopy(c.state['settings']), 'name':node['name'], 'epoch':c.history_epoch, 'segment':c.session_epoch}

    async def run(self, data, progress=None):
        refs, group_id, kind = data.get('nodes'), data.get('group'), data.get('test','http')
        if kind not in ('http','udp') or not isinstance(refs,list) or not 1<=len(refs)<=100 or any(not isinstance(r,str) for r in refs) or len(set(refs))!=len(refs):
            raise ValueError('请选择 1–100 个不同节点')
        if (kind=='udp' or group_id) and len(refs)!=1:
            raise ValueError('UDP 诊断或分组测试一次只支持一个节点')
        if any(r in self.inflight for r in refs):
            raise ValueError('所选节点已有测试进行中')
        self.inflight.update(refs)
        results = []
        try:
            async with self.lock:
                for index, ref in enumerate(refs):
                    if progress: progress(f"正在测试第 {index+1} / {len(refs)} 个节点；已完成 {len(results)} 个")
                    snapshot = await self.snapshot(ref, group_id)
                    try:
                        await self.core.ready()
                        await self.core.update_source(1,yaml.safe_dump({'proxies':[snapshot['config']]},allow_unicode=True))
                        upstream = await self.core.select(0,snapshot['config']['name'])
                        settings = snapshot['settings']
                        if kind=='udp':
                            result = await asyncio.wait_for(udp_sample(upstream,settings['stun_servers'][0]),60)
                            result['samples'] = []
                        else:
                            url = settings['tcp_check_urls'][0]
                            parts = urlsplit(url)
                            target = parts.scheme+'://'+parts.netloc+parts.path
                            start=time.monotonic()
                            connector=ProxyConnector.from_url('socks5://%s:%s'%upstream,rdns=True)
                            async with aiohttp.ClientSession(connector=connector,timeout=aiohttp.ClientTimeout(total=8)) as client:
                                async with client.get(url,allow_redirects=False) as response:
                                    ms=round((time.monotonic()-start)*1000,2)
                                    ok=200<=response.status<400
                            result={'samples':[{'target':target,'method':'http-get-headers','ok':ok,'ms':ms if ok else None}], 'ok':ok}
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        result={'samples':[], 'error':'测试未完成；无法据此判断线路丢包或完整出口可用性'}
                    result.update(node_ref=ref, config=hashlib.sha256(json.dumps([snapshot['epoch'],snapshot['settings']],sort_keys=True).encode()).hexdigest()[:16],test=kind,name=snapshot['name'],time=time.time(),segment=snapshot['segment'])
                    # A changed/deleted source or group must not acquire fresh-looking results.
                    c=self.controller
                    current=next((n for n in c.state['nodes'].values() if node_ref(c.state,n)==ref),None)
                    group=next((g for g in c.state['groups'] if g['id']==str(group_id)),None) if group_id else None
                    result['current']=bool(c.history_epoch==snapshot['epoch'] and current and not current['disabled'] and source_for(c.state,current)['enabled'] and (not group_id or group and group['uid']==snapshot['owner'] and group['node']==current['id']))
                    self.history.record(snapshot['owner'],ref,'manual',result)
                    results.append(result)
                    await self.clear()
            return results
        finally:
            self.inflight.difference_update(refs)
            # Cleanup owns the diagnostic lock, so it can never erase another job's provider.
            async with self.lock:
                await self.clear()

    async def clear(self):
        # Never roll the empty file back to credentials on a failed API reload.
        payload = yaml.safe_dump({'proxies':[{'name':'empty-01','type':'socks5','server':'127.0.0.1','port':9}]})
        atomic_write(self.core.directory/'providers'/'s01.yaml',payload)
        try:
            await self.core.request('DELETE','/connections')
            await self.core.request('PUT','/providers/proxies/s01')
        except Exception:
            raise ValueError('独立诊断核心清理失败，磁盘已清除测试凭据；请检查诊断服务') from None
