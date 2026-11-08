import asyncio
import copy
import time

import pytest
from gateway.server import Gateway
from scheduler.controller import Controller
from scheduler.probe import Observation
from scheduler.store import Store
from scheduler.policy import identity


def good(ip):
    return Observation(True, True, {ip}, {ip}, {ip: "US"}, False, "verified")


class FakeCore:
    host = "127.0.0.1"
    capacity = 32
    source_capacity = 16
    def __init__(self):
        self.selected = {1: "a", 2: "b"}
        self.updates = []
    async def ready(self):
        pass
    async def select(self, slot, node):
        self.selected[slot] = node
        return self.host, 20000+slot
    async def mapping_valid(self, slot, node):
        return self.selected.get(slot) == node
    async def update_source(self, slot, payload):
        self.updates.append((slot, payload))
    async def close(self):
        pass


class FakeProber:
    def __init__(self, core):
        self.core = core
        self.results = {"a": good("8.8.8.8"), "b": good("1.1.1.1"), "c": good("9.9.9.9")}
    async def observe(self, upstream, settings):
        return copy.deepcopy(self.results[self.core.selected[upstream[1]-20000]])


@pytest.fixture
def controller(tmp_path):
    now = [100.0]
    store = Store(tmp_path)
    store.state["sources"] = [{"slot": 1, "uid": identity(), "name": "one", "enabled": True, "url": "", "refreshed_at": time.time()}]
    store.state["nodes"] = {n: {"id": n, "source": 1, "name": n, "disabled": False, "priority": 100, "hint_us": True, "last_probe": None} for n in ("a", "b", "c")}
    for g, n in zip(store.state["groups"], ("a", "b")):
        g["node"] = n
    gateway = Gateway("127.0.0.1", clock=lambda: now[0])
    core = FakeCore()
    core.directory = tmp_path / 'core'
    (core.directory / 'providers').mkdir(parents=True)
    (core.directory / 'providers' / 's01.yaml').write_text('old provider')
    c = Controller(store, core, gateway, FakeProber(core), clock=lambda: now[0])
    c.now = now
    yield c
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('udp_ips,udp_country', [(set(), None), ({'1.1.1.1'}, 'US'), ({'4.2.2.2'}, 'DE')])
async def test_udp_observations_never_revoke_tcp_lease(controller, udp_ips, udp_country):
    c = controller
    await c.tick()
    generation = c.gateway.leases['1'].generation
    since = c.group('1')['since']
    countries = {'8.8.8.8': 'US', **{ip: udp_country for ip in udp_ips}}
    c.prober.results['a'] = Observation(True, bool(udp_ips), {'8.8.8.8'}, udp_ips, countries)
    c.group('1')['failure_since'] = c.clock() - 500
    await c.tick()
    assert c.group('1')['node'] == 'a' and c.group('1')['status'] == 'active'
    assert c.gateway.allowed('1', generation)
    assert c.gateway.leases['1'].generation == generation
    assert c.group('1')['since'] == since
    assert c.group('1')['failure_since'] is None
    assert c.group('1')['udp_ips'] == sorted(udp_ips)


@pytest.mark.asyncio
async def test_manual_switch_accepts_tcp_only_and_ignores_other_udp_occupation(controller):
    c = controller
    await c.tick()
    c.group('2')['udp_ips'] = ['9.9.9.9']
    c.prober.results['c'] = Observation(True, False, {'9.9.9.9'}, set(), {'9.9.9.9': 'US'})
    result = await c.action('group_switch', {'group': '1', 'node': 'c'})
    assert result['outcome'] == 'applied'
    assert result['selection']['attempts'][0]['eligible']
    assert c.group('1')['node'] == 'c' and c.group('1')['status'] == 'active'
    assert c.group('2')['status'] == 'active'


@pytest.mark.asyncio
@pytest.mark.parametrize('ip,country', [('1.1.1.1', 'US'), ('9.9.9.9', 'DE')])
async def test_tcp_conflict_or_foreign_exit_still_rejects_candidate(controller, ip, country):
    c = controller
    await c.tick()
    c.prober.results['c'] = Observation(True, False, {ip}, set(), {ip: country})
    result = await c.action('group_switch', {'group': '1', 'node': 'c'})
    assert result['outcome'] == 'not_applied'
    assert c.group('1')['node'] == 'a'


@pytest.mark.asyncio
async def test_stable_does_not_change_and_third_source_does_not_switch(controller):
    c = controller
    await c.tick()
    assert [g["node"] for g in c.state["groups"]] == ["a", "b"]
    generations = {k: v.generation for k, v in c.gateway.leases.items()}
    c.state["sources"].append({"slot": 3, "enabled": True, "url": "", "name": "third"})
    await c.tick()
    assert [g["node"] for g in c.state["groups"]] == ["a", "b"]
    assert generations == {k: v.generation for k, v in c.gateway.leases.items()}


@pytest.mark.asyncio
async def test_failure_after_identity_expiry_still_switches(controller):
    c = controller
    await c.tick()
    c.prober.results["a"] = Observation(False, False, failure_confirmed=True)
    await c.tick()
    c.now[0] += 181
    await c.tick()
    assert c.group("1")["node"] == "c"
    assert c.group("1")["status"] == "active"
    assert c.group("2")["node"] == "b"


@pytest.mark.asyncio
async def test_identity_only_expires_without_blind_switch_then_recovers(controller):
    c = controller
    await c.tick()
    c.prober.results["a"] = Observation(True, True)
    c.now[0] += 181
    await c.tick()
    assert c.group("1")["node"] == "a"
    assert c.group("1")["status"] == "paused"
    assert "8.8.8.8" in c.occupied("2")
    c.prober.results["a"] = good("8.8.8.8")
    await c.tick()
    assert c.group("1")["status"] == "active"


@pytest.mark.asyncio
async def test_pin_all_failures_unpin_identity_does_not_switch(controller):
    c = controller
    await c.tick()
    c.group("1")["pinned"] = True
    c.prober.results["a"] = Observation(False, False, failure_confirmed=True)
    await c.tick()
    c.now[0] += 181
    await c.tick()
    assert c.group("1")["node"] == "a"
    assert c.group("1")["status"] == "paused"
    c.prober.results["a"] = Observation(True, True)
    c.group("1")["pinned"] = False
    await c.tick()
    assert c.group("1")["node"] == "a"
    assert c.group("1")["status"] == "paused"


@pytest.mark.asyncio
async def test_collision_blocks_changed_group_other_lease_remains(controller):
    c = controller
    await c.tick()
    old = c.gateway.leases["2"].generation
    c.group("1")["pinned"] = True
    c.prober.results["a"] = good("1.1.1.1")
    await c.tick()
    assert c.group("1")["status"] == "paused"
    assert c.group("2")["status"] == "active"
    assert c.gateway.leases["2"].generation == old
    assert "1.1.1.1" in c.occupied("1")


@pytest.mark.asyncio
async def test_partial_foreign_identity_immediately_blocks(controller):
    c = controller
    await c.tick()
    c.group("1")["pinned"] = True
    c.prober.results["a"] = Observation(True, False, {"8.8.4.4"}, set(), {"8.8.4.4": "DE"})
    await c.tick()
    assert not c.gateway.allowed("1", c.gateway.leases["1"].generation)


@pytest.mark.asyncio
async def test_benign_change_keeps_old_occupation_until_revoke_ack(controller):
    c = controller
    await c.tick()
    entered, release = asyncio.Event(), asyncio.Event()
    revoke = c.gateway.revoke
    async def delayed(identity):
        if identity == "1":
            await revoke(identity)
            entered.set()
            await release.wait()
        else:
            await revoke(identity)
    c.gateway.revoke = delayed
    task = asyncio.create_task(c.accept(c.group("1"), "a", good("8.8.4.4")))
    await entered.wait()
    assert "8.8.8.8" in c.occupied("2")
    assert not c.gateway.allowed("1", c.gateway.leases["1"].generation)
    release.set()
    assert await task
    assert "8.8.8.8" not in c.occupied("2")
    assert "8.8.4.4" in c.occupied("2")


@pytest.mark.asyncio
async def test_no_backup_does_not_reuse_other_group_exit(controller):
    c = controller
    await c.tick()
    c.prober.results["a"] = Observation(False, False, failure_confirmed=True)
    c.prober.results["c"] = good("1.1.1.1")
    await c.tick()
    c.now[0] += 181
    await c.tick()
    assert c.group("1")["status"] == "paused"
    assert c.group("2")["status"] == "active"


@pytest.mark.asyncio
async def test_restarted_store_has_no_restored_lease(controller, tmp_path):
    c = controller
    await c.tick()
    path = c.store.directory
    c.store.close()
    c.store = Store(path)
    assert c.group("1")["status"] == "paused"
    fresh = Gateway("127.0.0.1")
    assert not fresh.allowed("1", 0)


@pytest.mark.asyncio
async def test_partial_same_round_collision_stops_both_before_any_accept(controller):
    c=controller
    await c.tick()
    for g in c.state['groups']:
        g['pinned']=True
    c.prober.results['a']=Observation(True,False,{'4.2.2.2'},set(),{})
    c.prober.results['b']=Observation(True,False,{'4.2.2.2'},set(),{})
    await c.tick()
    for group in c.state['groups']:
        assert group['status']=='paused'
        assert not c.gateway.allowed(group['id'],c.gateway.leases[group['id']].generation)


@pytest.mark.asyncio
async def test_new_ip_partial_observation_revokes_before_next_group_probe(controller):
    c=controller
    await c.tick()
    c.prober.results['a']=Observation(True,False,{'4.2.2.2'},set(),{})
    observed=await c.observe(c.group('1'),'a')
    assert not observed.complete
    assert not c.gateway.allowed('1',c.gateway.leases['1'].generation)
    assert c.group('1')['tcp_ips']==['8.8.8.8']  # old occupation retained


@pytest.mark.asyncio
async def test_invalid_settings_do_not_partially_mutate(controller):
    c=controller
    previous=copy.deepcopy(c.state['settings'])
    with pytest.raises(ValueError):
        await c.action('settings',{'check_interval':5,'identity_ttl':-1})
    assert c.state['settings']==previous


@pytest.mark.asyncio
async def test_old_observation_cannot_receive_a_fresh_deadline(controller):
    c=controller
    await c.tick()
    result=good('8.8.8.8')
    result.observed_at=c.clock()-181
    assert not await c.accept(c.group('1'),'a',result)
    assert not c.gateway.allowed('1',c.gateway.leases['1'].generation)


@pytest.mark.asyncio
async def test_source_rename_preserves_active_leases_and_never_refreshes(controller):
    c = controller
    await c.tick()
    c.state['sources'][0]['url'] = 'https://old.example/sub?token=private'
    before_nodes = copy.deepcopy(c.state['nodes'])
    before_leases = {k: (v.generation, v.deadline) for k, v in c.gateway.leases.items()}
    await c.action('source_update', {'slot': 1, 'name': ' Renamed ', 'url': c.state['sources'][0]['url']})
    assert c.state['sources'][0]['name'] == 'Renamed'
    assert c.state['sources'][0]['slot'] == 1
    assert c.state['nodes'] == before_nodes
    assert not c.core.updates
    assert before_leases == {k: (v.generation, v.deadline) for k, v in c.gateway.leases.items()}
    saved = __import__('json').loads(c.store.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
    assert saved['sources'][0]['name'] == 'Renamed'


@pytest.mark.asyncio
async def test_source_url_change_downloads_new_address_and_preserves_node_preferences(controller, monkeypatch):
    from scheduler.core import parse_subscription
    c = controller
    content = 'proxies:\n- {name: US sample, type: socks5, server: 127.0.0.1, port: 12345}\n'
    nodes, _ = parse_subscription(content, 1)
    node = next(iter(nodes.values()))
    node.update(disabled=True, priority=7, last_probe={'reason': 'old observation'})
    c.state['nodes'] = nodes
    c.state['sources'][0]['url'] = 'https://old.example/sub'
    for group in c.state['groups']:
        group.update(node=None, paused=True)
    calls = []
    class Response:
        status = 200
        @property
        def content(self): return self
        async def iter_chunked(self, size): yield content.encode()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    monkeypatch.setattr('scheduler.controller.aiohttp.ClientSession', Session)
    await c.action('source_update', {'slot': 1, 'name': 'New name', 'url': 'https://new.example/sub?token=new'})
    assert calls[0][0] == 'https://new.example/sub?token=new'
    assert calls[0][1]['allow_redirects'] is False
    assert len(calls) == 1
    assert c.state['sources'][0]['url'] == calls[0][0]
    assert c.state['sources'][0]['slot'] == 1
    assert len(c.core.updates) == 1
    assert c.state['nodes'] == nodes
    assert c.state['nodes'][node['id']]['priority'] == 7


@pytest.mark.asyncio
async def test_source_failed_update_retains_old_metadata_nodes_and_redacts_error(controller, monkeypatch):
    c = controller
    original_source = copy.deepcopy(c.state['sources'][0])
    original_nodes = copy.deepcopy(c.state['nodes'])
    async def fail(slot, payload):
        raise ValueError('network exception contains https://secret.example/?token=new-secret')
    monkeypatch.setattr(c.core, 'update_source', fail)
    content = 'proxies:\n- {name: US replacement, type: socks5, server: 127.0.0.1, port: 12345}\n'
    with pytest.raises(ValueError) as error:
        await c.action('source_update', {'slot': 1, 'name': 'Must not persist', 'url': 'https://secret.example/?token=new-secret', 'content': content})
    assert 'new-secret' not in str(error.value) and 'secret.example' not in str(error.value)
    assert c.state['sources'][0] == original_source
    assert c.state['nodes'] == original_nodes


@pytest.mark.asyncio
async def test_source_clear_url_retains_nodes_and_stops_automatic_download(controller):
    c = controller
    source = c.state['sources'][0]
    source.update(url='https://old.example/sub', error='old failure', retry_at=123)
    before = copy.deepcopy(c.state['nodes'])
    await c.action('source_update', {'slot': 1, 'url': '', 'name': source['name']})
    assert not source['url'] and source['retry_at'] == 0 and source['error'] is None
    assert c.state['nodes'] == before and not c.core.updates


@pytest.mark.asyncio
@pytest.mark.parametrize('fields', [{'name': ''}, {'name': 'x'*101}, {'url': 'http://example.com/sub'}, {'url': 'https://example.com:bad/sub'}, {'url': 'https://bad host/sub'}, {'content': []}])
async def test_source_invalid_edit_does_not_partially_apply(controller, fields):
    c = controller
    before = copy.deepcopy(c.state['sources'][0])
    with pytest.raises(ValueError):
        await c.action('source_update', {'slot': 1, **fields})
    assert c.state['sources'][0] == before


@pytest.mark.asyncio
async def test_source_edit_commits_url_and_nodes_in_same_snapshot(controller, monkeypatch):
    import json
    from scheduler.core import parse_subscription
    c = controller
    for g in c.state['groups']: g.update(node=None, paused=True)
    content='proxies:\n- {name: replacement, type: socks5, server: 127.0.0.1, port: 12345}\n'
    expected, _ = parse_subscription(content, 1)
    save = c.store.save
    snapshots=[]
    def capture():
        save()
        snapshots.append(json.loads(c.store.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0]))
    monkeypatch.setattr(c.store,'save',capture)
    await c.action('source_update',{'slot':1,'url':'https://new.example/sub','name':'replacement','content':content})
    for state in snapshots:
        if set(state['nodes']) == set(expected):
            assert state['sources'][0]['url']=='https://new.example/sub'
            assert state['sources'][0]['name']=='replacement'
    assert len(snapshots)==1
    assert not (c.store.directory/'maintenance.json').exists()


@pytest.mark.asyncio
async def test_source_edit_save_failure_rolls_back_provider_metadata_and_nodes(controller, monkeypatch):
    c=controller
    for g in c.state['groups']: g.update(node=None, paused=True)
    before_source=copy.deepcopy(c.state['sources'][0]);before_nodes=copy.deepcopy(c.state['nodes'])
    old_payload=(c.core.directory/'providers/s01.yaml').read_text()
    save=c.store.save
    calls=[0]
    def fail_once():
        calls[0]+=1
        if calls[0]==1:raise OSError('disk full')
        save()
    monkeypatch.setattr(c.store,'save',fail_once)
    content='proxies:\n- {name: replacement, type: socks5, server: 127.0.0.1, port: 12345}\n'
    with pytest.raises(ValueError,match='已保留'):
        await c.action('source_update',{'slot':1,'url':'https://new.example/sub','name':'replacement','content':content})
    assert c.state['sources'][0]==before_source and c.state['nodes']==before_nodes
    assert c.core.updates[-1]==(1,old_payload)
    assert not c.maintenance and not (c.store.directory/'maintenance.json').exists()


@pytest.mark.asyncio
async def test_source_edit_process_interruption_leaves_durable_barrier(controller, monkeypatch):
    from scheduler.controller import Controller
    c=controller
    for g in c.state['groups']:g.update(node=None,paused=True)
    class ProcessExit(BaseException):pass
    async def interrupted(*args):raise ProcessExit()
    monkeypatch.setattr(c.core,'update_source',interrupted)
    content='proxies:\n- {name: replacement, type: socks5, server: 127.0.0.1, port: 12345}\n'
    with pytest.raises(ProcessExit):
        await c.action('source_update',{'slot':1,'url':'https://new.example/sub','content':content})
    assert (c.store.directory/'maintenance.json').exists()
    restarted=Controller(c.store,c.core,c.gateway,c.prober)
    assert restarted.maintenance
    assert all(g['status']=='paused' for g in c.state['groups'])


@pytest.mark.asyncio
async def test_source_edit_audit_failure_does_not_report_false_rollback(controller, monkeypatch):
    c=controller
    for g in c.state['groups']:g.update(node=None,paused=True)
    monkeypatch.setattr(c,'event',lambda *args: (_ for _ in ()).throw(OSError('audit failure')))
    content='proxies:\n- {name: replacement, type: socks5, server: 127.0.0.1, port: 12345}\n'
    await c.action('source_update',{'slot':1,'url':'https://new.example/sub','content':content})
    assert c.state['sources'][0]['url']=='https://new.example/sub'
    assert not (c.store.directory/'maintenance.json').exists()


@pytest.mark.asyncio
async def test_source_rename_save_failure_keeps_old_metadata(controller, monkeypatch):
    c=controller
    before=copy.deepcopy(c.state['sources'][0])
    def fail():raise OSError('disk full')
    monkeypatch.setattr(c.store,'save',fail)
    with pytest.raises(OSError):
        await c.action('source_update',{'slot':1,'name':'must not remain in memory'})
    assert c.state['sources'][0]==before


@pytest.mark.asyncio
async def test_source_edit_persistent_disk_failure_revokes_every_group(controller, monkeypatch):
    c=controller
    await c.tick()
    assert all(c.gateway.allowed(g['id'],c.gateway.leases[g['id']].generation) for g in c.state['groups'])
    def fail():raise OSError('persistent disk failure')
    monkeypatch.setattr(c.store,'save',fail)
    # Same nodes: provider editing would normally preserve the existing leases.
    with pytest.raises(OSError):await c.halt_source_edit('rollback failed')
    assert c.maintenance
    assert all(not c.gateway.allowed(g['id'],c.gateway.leases[g['id']].generation) for g in c.state['groups'])
