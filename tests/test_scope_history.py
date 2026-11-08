import asyncio
import copy
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from scheduler.policy import identity, node_ref, eligible
from scheduler.probe import Observation
from scheduler.store import Store
from scheduler.history import History
from test_controller import controller


@pytest.mark.asyncio
async def test_scope_preserves_current_session_and_rejects_outside(controller):
    c=controller
    await c.tick()
    group=c.group('1');generation=c.gateway.leases['1'].generation
    ref=node_ref(c.state,c.state['nodes']['a'])
    await c.action('group_scope',{'group':'1','scope':{'mode':'nodes','ids':[ref]}})
    assert c.gateway.leases['1'].generation==generation
    assert c.gateway.allowed('1',generation)
    with pytest.raises(ValueError,match='允许范围'):
        await c.action('group_switch',{'group':'1','node':'c'})
    assert group['node']=='a'


@pytest.mark.asyncio
async def test_scope_exclusion_requires_consent_and_fixed_stays_fixed(controller):
    c=controller;await c.tick();g=c.group('1');g['pinned']=True
    ref=node_ref(c.state,c.state['nodes']['c'])
    before=copy.deepcopy(g)
    with pytest.raises(ValueError,match='确认'):
        await c.action('group_scope',{'group':'1','scope':{'mode':'nodes','ids':[ref]}})
    assert g==before
    await c.action('group_scope',{'group':'1','scope':{'mode':'nodes','ids':[ref]},'confirm_disconnect':True})
    assert g['pinned'] and g['node'] is None and g['status']=='paused'
    assert not c.gateway.allowed('1',c.gateway.leases['1'].generation)
    with pytest.raises(ValueError):
        await c.action('group_scope',{'group':'1','scope':{'mode':'nodes','ids':[]}})


@pytest.mark.asyncio
async def test_source_slot_reuse_cannot_inherit_permission(controller):
    c=controller;g=c.group('1');uid=c.state['sources'][0]['uid']
    g['scope']={'mode':'sources','ids':[uid]}
    assert eligible(c.state,g,c.state['nodes']['a'])
    c.state['sources'][0]['uid']=identity()
    assert not eligible(c.state,g,c.state['nodes']['a'])


@pytest.mark.asyncio
async def test_failed_search_backoff_avoids_all_probes_and_survives_restart(controller):
    c=controller;g=c.group('1');c.group('2')['paused']=True
    c.wall=lambda:c.now[0]
    c.prober.results={n:Observation(False,False,failure_confirmed=True) for n in ('a','b','c')}
    g['node']=None
    count=0;original=c.prober.observe
    async def observe(*args):
        nonlocal count
        count+=1
        return await original(*args)
    c.prober.observe=observe
    await c.tick();assert count==3 and g['next_retry_at']==160
    for _ in range(3):await c.tick()
    assert count==3
    c.now[0]=161;await c.tick();assert count==6 and g['next_retry_at']==461
    await c.action('group_retry',{'group':'1'})
    assert count==9 and g['next_retry_at']==1061
    reopened=Store(c.store.directory)
    assert reopened.state['groups'][0]['next_retry_at']==1061
    reopened.close()


def test_migration_tombstone_makes_real_old_store_fail_closed(tmp_path):
    old_source=(Path(__file__).parent/'fixtures'/'legacy_store_v016.py').read_text()
    namespace={};exec(old_source,namespace)
    old=namespace['Store'](tmp_path)
    old.state['sources']=[{'slot':1,'name':'source','enabled':True,'url':''}]
    old.save();old.close()
    new=Store(tmp_path)
    assert len(new.state['sources'])==1 and new.state['schema']==2
    new.close()
    with pytest.raises(KeyError): namespace['Store'](tmp_path)
    fresh=Store(tmp_path);assert fresh.state['groups'][0]['scope']['mode']=='all';fresh.close()


@pytest.mark.asyncio
async def test_history_statistics_separate_failures_and_origins_and_cap(tmp_path,monkeypatch):
    history=History(tmp_path)
    history.start()
    for origin,ok,ms in [('running',True,100),('running',False,None),('running',True,200),('manual',True,9)]:
        history.record('g','n',origin,{'node_ref':'n','config':'x','samples':[{'target':'https://x/check','method':'http-get-headers','ok':ok,'ms':ms}]})
    await history.queue.join()
    result=await asyncio.to_thread(history.query,'g',24)
    run=next(s for s in result['series'] if s['origin']=='running')
    assert run['attempts']==3 and run['successes']==2 and run['mean_ms']==150
    assert run['variation_ms'] is None
    monkeypatch.setattr('scheduler.history.MAX_ROWS',2)
    history.record('g','n','state',{'status':'unavailable'})
    await history.queue.join()
    with sqlite3.connect(history.path) as db:assert db.execute('select count(*) from samples').fetchone()[0]==2
    assert result['coverage_start'] is not None
    await history.close()


def test_missing_v2_cannot_replay_legacy_archive(tmp_path):
    store=Store(tmp_path);store.close()
    (tmp_path/'state-v2.db').unlink()
    with pytest.raises(ValueError,match='缺失'):
        Store(tmp_path)


def test_schema2_must_not_silently_fill_scope_or_identity(tmp_path):
    from scheduler.policy import normalize
    store=Store(tmp_path);s=copy.deepcopy(store.state);store.close()
    del s['groups'][0]['scope']
    with pytest.raises(ValueError):normalize(s)


@pytest.mark.asyncio
async def test_full_window_retains_udp_and_events_and_restart_statistics(tmp_path):
    h=History(tmp_path);h.start()
    h.record('g','n','manual',{'node_ref':'n','test':'udp','sent':20,'received':19})
    h.record('g','n','state',{'status':'unavailable'})
    for i in range(510):
        h.record('g','n','running',{'node_ref':'n','config':'same','segment':'before' if i<255 else 'after',
            'samples':[{'target':'local','method':'http-get-headers','ok':True,'ms':100 if i<255 else 200}]})
    await h.queue.join()
    q=h.query('g',24)
    assert q['records_truncated'] and len(q['records'])==500
    assert q['udp_records'][0]['received']==19 and q['state_records'][0]['status']=='unavailable'
    series=q['series'][0]
    assert series['attempts']==510 and series['mean_ms']==150 and series['variation_ms']==0
    assert any(p['break_before'] for p in series['points'])
    await h.close()


def test_migration_ready_cleans_leftover_private_archive(tmp_path):
    store=Store(tmp_path);store.close()
    for name in ('state-v1-migration.db','state-v2.migration.json'):
        (tmp_path/name).write_text('leftover')
    store=Store(tmp_path);store.close()
    assert not (tmp_path/'state-v1-migration.db').exists()
    assert not (tmp_path/'state-v2.migration.json').exists()


@pytest.mark.asyncio
async def test_failed_scope_save_retains_allowed_current_session(controller,monkeypatch):
    c=controller;await c.tick();g=c.group('1')
    before=copy.deepcopy(g['scope']);generation=c.gateway.leases['1'].generation
    def fail():raise OSError('fixture persistence failure')
    monkeypatch.setattr(c.store,'save',fail)
    with pytest.raises(OSError):
        await c.action('group_scope',{'group':'1','scope':{'mode':'nodes','ids':[node_ref(c.state,c.state['nodes']['a'])]}})
    assert g['scope']==before and c.gateway.leases['1'].generation==generation
    assert c.gateway.allowed('1',generation)


@pytest.mark.asyncio
async def test_changed_scope_without_current_node_checks_immediately_despite_backoff(controller):
    c=controller;g=c.group('1');g['node']=None
    g.update(next_retry_at=time.time()+900,retry_step=3)
    ref=node_ref(c.state,c.state['nodes']['c'])
    result=await c.action('group_scope',{'group':'1','scope':{'mode':'nodes','ids':[ref]}})
    assert g['node']=='c' and g['status']=='active'
    assert result['outcome']=='saved_available'
    assert g['last_action']['kind']=='group_scope'


@pytest.mark.asyncio
async def test_switch_failed_verification_has_persistent_explicit_outcome(controller):
    c=controller;g=c.group('1');g['node']=None
    c.prober.results['c']=Observation(True,True,{'9.9.9.9'},{'9.9.9.9'},{'9.9.9.9':'US'},identity_complete=False,identity_errors=['https://identity.test/'],reason='部分出口验证目标未返回有效结果：https://identity.test/')
    result=await c.action('group_switch',{'group':'1','node':'c'})
    assert g['node'] is None
    assert result['outcome']=='not_applied'
    assert result['selection']['requested']=='c'
    assert result['selection']['attempts'][0]['identity_errors']==['https://identity.test/']
    saved=json.loads(c.store.db.execute('select body from state where id=1').fetchone()[0])
    assert saved['groups'][0]['last_action']['outcome']=='not_applied'
    assert c.public()['groups'][0]['selection_progress'] is None


@pytest.mark.asyncio
async def test_manual_auto_search_continues_past_three_and_yields_to_scheduler(controller):
    c=controller;await c.tick();g=c.group('2');g['node']=None
    # Four candidates fail, a fifth works. The active other group still gets a scheduled check.
    for name in ('d','e'):
        c.state['nodes'][name]={**c.state['nodes']['c'],'id':name,'name':name}
    from test_controller import good
    c.prober.results.update(d=Observation(False,False),e=good('4.4.4.4'))
    c.prober.results.update(b=Observation(False,False),c=Observation(False,False))
    original=c.prober.observe; scheduled=[];order=[]
    async def scheduler():
        async with c.lock:
            await c.tick()
        order.append('scheduler')
    async def probe(upstream,settings):
        if upstream[1]==20000:
            order.append(c.core.selected[0])
            if len(order)==1:
                scheduled.append(asyncio.create_task(scheduler()))
                await asyncio.sleep(0)
        return await original(upstream,settings)
    c.prober.observe=probe
    result=await c.action('group_switch',{'group':'2'})
    await asyncio.gather(*scheduled)
    assert result['outcome']=='applied' and g['node']=='e'
    assert result['selection']['checked']==5
    assert order.index('scheduler') < order.index('b')
    assert c.group('1')['status']=='active'
    assert not c.selection_progress


@pytest.mark.asyncio
async def test_search_cancel_is_cooperative_and_never_applies_current_candidate(controller):
    c=controller;g=c.group('1');g['node']=None;cancel=asyncio.Event()
    original=c.prober.observe
    async def probe(*args):
        cancel.set()
        return await original(*args)
    c.prober.observe=probe
    result=await c.action('group_switch',{'group':'1','node':'c'},cancel=cancel)
    assert result['outcome']=='cancelled' and g['node'] is None
    assert result['selection']['checked']==1 and not c.selection_progress


@pytest.mark.asyncio
async def test_failed_manual_selection_preserves_working_current_node(controller):
    c=controller;await c.tick();g=c.group('1');before=c.gateway.leases['1'].generation
    c.prober.results['c']=Observation(False,False)
    result=await c.action('group_switch',{'group':'1','node':'c'})
    assert result['outcome']=='not_applied'
    assert g['node']=='a' and c.gateway.allowed('1',before)


@pytest.mark.asyncio
async def test_manual_search_stops_when_configuration_changes_between_candidates(controller):
    c=controller;g=c.group('1');g['node']=None
    c.prober.results={n:Observation(False,False) for n in ('a','b','c')}
    pending=[];original=c.prober.observe
    async def edit():
        async with c.lock:c.state['settings']['probe_timeout']=9
    async def probe(*args):
        pending.append(asyncio.create_task(edit()))
        await asyncio.sleep(0)
        return await original(*args)
    c.prober.observe=probe
    result=await c.action('group_switch',{'group':'1'})
    await asyncio.gather(*pending)
    assert result['outcome']=='interrupted'
    assert result['selection']['checked']==1 and g['node'] is None


@pytest.mark.asyncio
async def test_cancel_during_business_revalidation_never_grants_candidate(controller):
    c=controller;g=c.group('1');g['node']=None;cancel=asyncio.Event()
    original=c.prober.observe
    async def probe(upstream,settings):
        result=await original(upstream,settings)
        if upstream[1]==20001:cancel.set()
        return result
    c.prober.observe=probe
    result=await c.action('group_switch',{'group':'1','node':'c'},cancel=cancel)
    assert result['outcome']=='cancelled' and g['node'] is None
    assert not c.gateway.allowed('1',c.gateway.leases['1'].generation)


@pytest.mark.asyncio
async def test_evaluation_recommends_multiple_by_latency_without_switching(controller):
    from test_controller import good
    c=controller;await c.tick();g=c.group('2');generation=c.gateway.leases['2'].generation
    for name,delay in [('c',80),('d',20),('e',50)]:
        if name not in c.state['nodes']:c.state['nodes'][name]={**c.state['nodes']['c'],'id':name,'name':name}
        c.prober.results[name]=good({'c':'9.9.9.9','d':'4.4.4.4','e':'8.8.4.4'}[name])
        c.prober.results[name].samples=[{'method':'http-get-headers','ok':True,'ms':delay,'target':'https://test/'}]
    result=await c.action('group_evaluate',{'group':'2'})
    assert result['outcome']=='evaluated' and g['node']=='b'
    assert c.gateway.allowed('2',generation)
    names={node_ref(c.state,n):n['name'] for n in c.state['nodes'].values()}
    assert [names[r] for r in result['selection']['recommended']]==['d','e','c']
    assert g['last_evaluation']['qualified']==4  # b plus the three independent candidates; a conflicts with group1.


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_revalidation',[False,True])
async def test_failed_revalidation_immediately_revalidates_and_restores_previous(controller,cancel_revalidation):
    c=controller;await c.tick();g=c.group('1');cancel=asyncio.Event();original=c.prober.observe
    async def probe(upstream,settings):
        if upstream[1]==20001 and c.core.selected[1]=='c':
            if cancel_revalidation:cancel.set()
            return Observation(False,False)
        return await original(upstream,settings)
    c.prober.observe=probe
    result=await c.action('group_switch',{'group':'1','node':'c'},cancel=cancel)
    assert result['outcome']==('cancelled' if cancel_revalidation else 'not_applied')
    assert g['node']=='a' and c.core.selected[1]=='a' and g['next_retry_at']==0
    assert c.gateway.allowed('1',c.gateway.leases['1'].generation)
    assert result['selection']['attempts'][0]['previous_restored']


@pytest.mark.asyncio
async def test_applied_outcome_is_durable_before_later_queued_pause(controller):
    c=controller;await c.tick();original=c.prober.observe;tasks=[]
    async def later_pause():await c.action('group_pause',{'group':'1'})
    async def probe(upstream,settings):
        if upstream[1]==20001 and c.core.selected[1]=='c':
            tasks.append(asyncio.create_task(later_pause()))
            await asyncio.sleep(0)
        return await original(upstream,settings)
    c.prober.observe=probe
    result=await c.action('group_switch',{'group':'1','node':'c'})
    await asyncio.gather(*tasks)
    assert result['outcome']=='applied'
    assert c.group('1')['last_action']['outcome']=='applied'
    assert c.group('1')['paused'] and not c.gateway.allowed('1',c.gateway.leases['1'].generation)
