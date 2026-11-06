import asyncio
import copy
import time

import pytest
from gateway.server import Gateway
from scheduler.controller import Controller
from scheduler.probe import Observation
from scheduler.store import Store


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
    store.state["sources"] = [{"slot": 1, "name": "one", "enabled": True, "url": "", "refreshed_at": time.time()}]
    store.state["nodes"] = {n: {"id": n, "source": 1, "name": n, "disabled": False, "priority": 100, "hint_us": True, "last_probe": None} for n in ("a", "b", "c")}
    for g, n in zip(store.state["groups"], ("a", "b")):
        g["node"] = n
    gateway = Gateway("127.0.0.1", clock=lambda: now[0])
    core = FakeCore()
    c = Controller(store, core, gateway, FakeProber(core), clock=lambda: now[0])
    c.now = now
    yield c
    store.close()


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
