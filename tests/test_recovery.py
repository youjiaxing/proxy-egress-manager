import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from gateway.server import Gateway, Lease
from scheduler.core import Core, parse_subscription
from scheduler.controller import Controller
from scheduler.store import Store
from test_controller import FakeCore, FakeProber
from web.server import Management, validate_backup


def test_offline_backup_restore_and_live_lock_rejection(tmp_path):
    import fcntl
    root=Path(__file__).resolve().parents[1]
    core=Core(tmp_path/'core',secret='x'*32)
    core.initialize()
    store=Store(tmp_path)
    store.state['groups'][0]['name']='before upgrade'
    store.save()
    env={**os.environ,'DATA_DIR':str(tmp_path),'CORE_CONFIG_DIR':str(tmp_path/'core')}
    script=[sys.executable,str(root/'scripts/maintenance.py')]
    backup=tmp_path/'backups'/'before.json'
    lock=(tmp_path/'controller.lock').open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    result=subprocess.run(script+['backup',str(backup)],env=env,capture_output=True,text=True)
    assert result.returncode!=0 and '仍在运行' in result.stderr
    lock.close()
    store.close()
    subprocess.run(script+['backup',str(backup)],env=env,check=True,capture_output=True)
    assert backup.stat().st_mode & 0o777==0o600
    store=Store(tmp_path)
    store.state['groups'][0]['name']='after upgrade'
    store.save(); store.close()
    subprocess.run(script+['restore',str(backup)],env=env,check=True,capture_output=True)
    restored=Store(tmp_path)
    assert restored.state['groups'][0]['name']=='before upgrade'
    assert restored.state['quality_epoch'] != json.loads(backup.read_text())['state']['quality_epoch']
    from scheduler.history import History
    assert History(tmp_path).query('system',24)['restores']
    assert all(g['status']=='paused' for g in restored.state['groups'])
    restored.close()


@pytest.mark.asyncio
async def test_expiry_revokes_when_control_lock_hung_web_still_serves(tmp_path):
    now=[0.]
    store=Store(tmp_path)
    core=FakeCore()
    gateway=Gateway('127.0.0.1',clock=lambda:now[0])
    controller=Controller(store,core,gateway,FakeProber(core),clock=lambda:now[0])
    for group in controller.state['groups']:
        gateway.leases[group['id']]=Lease()
        group['status']='active'
        controller.identities[group['id']]=180
        gateway.grant(group['id'],('127.0.0.1',9),180)
    gateway.pulse(60)
    await controller.lock.acquire()
    timer=asyncio.create_task(controller.expire())
    try:
        now[0]=61
        await asyncio.sleep(.3)
        assert all(g['status']=='paused' for g in controller.state['groups'])
        assert not gateway.allowed('1',gateway.leases['1'].generation)
        manager=Management(controller,'test-password-long-enough','http://localhost',secure=False)
        client=TestClient(TestServer(manager.app),cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        assert (await client.get('/')).status==200
        assert (await client.get('/healthz')).status==503
        await client.close()
    finally:
        timer.cancel(); await asyncio.gather(timer,return_exceptions=True)
        controller.lock.release(); store.close()


@pytest.mark.asyncio
async def test_web_backup_and_failed_restore_stays_maintenance(tmp_path):
    core=FakeCore()
    core.directory=tmp_path/'core'
    store=Store(tmp_path)
    gateway=Gateway('127.0.0.1')
    controller=Controller(store,core,gateway,FakeProber(core))
    for group in controller.state['groups']:
        gateway.leases[group['id']]=Lease()
    manager=Management(controller,'test-password-long-enough','http://localhost',secure=False)
    client=TestClient(TestServer(manager.app),cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        result=await client.post('/api/login',json={'password':'test-password-long-enough'},headers={'Origin':'http://localhost'})
        csrf=(await result.json())['csrf']
        headers={'Origin':'http://localhost','X-CSRF-Token':csrf}
        backup=await (await client.post('/api/backup',json={},headers=headers)).json()
        validate_backup(backup,32,16)
        # Introduce a valid source whose provider import will fail at the API boundary.
        nodes,payload=parse_subscription('proxies: [{name: US test, type: socks5, server: example.com, port: 1080}]',1)
        backup['state']['sources']=[{'slot':1,'uid':'1'*32,'name':'x','url':'','enabled':True}]
        backup['state']['nodes']=nodes
        backup['providers']={'1':payload}
        async def fail(slot,payload):
            raise RuntimeError('simulated core failure')
        core.update_source=fail
        response=await client.post('/api/restore',json=backup,headers=headers)
        assert response.status==202
        job=(await response.json())['job']
        for _ in range(50):
            result=await (await client.get('/api/jobs/'+job)).json()
            if result['status']!='running':
                break
            await asyncio.sleep(.01)
        assert result['status']=='error'
        assert controller.maintenance
        with pytest.raises(ValueError):
            await controller.action('group_switch',{'group':'1'})
        await controller.tick()
        assert not gateway.allowed('1',gateway.leases['1'].generation)
        assert (await client.get('/healthz')).status==503
    finally:
        await client.close(); store.close()


def test_malformed_restore_rejected_before_modifying_state(tmp_path):
    store=Store(tmp_path)
    backup=store.backup()
    backup['providers']={}
    validate_backup(backup,32,16)
    backup['state']['groups'][0]['pin_until']={'bad':'timestamp'}
    with pytest.raises(ValueError):
        validate_backup(backup,32,16)
    backup['state']['groups'][0]['pin_until']=None
    backup['providers']['999']='proxies: []'
    with pytest.raises(ValueError):
        validate_backup(backup,32,16)
    store.close()


def test_interrupted_restore_marker_survives_restart_and_blocks_backup(tmp_path):
    from scheduler.maintenance import begin, finish
    directory=tmp_path/'data'
    store=Store(directory)
    begin(directory)
    store.close()
    reopened=Store(directory)
    core=FakeCore()
    gateway=Gateway('127.0.0.1')
    controller=Controller(reopened,core,gateway,FakeProber(core))
    assert controller.maintenance
    assert (directory/'maintenance.json').exists()
    reopened.close()
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/'scripts/maintenance.py'),'backup',str(tmp_path/'bad-backup.json')],
                          env={**os.environ,'DATA_DIR':str(directory)},capture_output=True,text=True)
    assert result.returncode!=0 and '恢复未完成' in result.stderr
    finish(directory)
    assert not (directory/'maintenance.json').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('remove_group',[False,True])
async def test_successful_web_restore_changes_history_epoch_and_keeps_history(tmp_path,remove_group):
    from types import SimpleNamespace
    from scheduler.history import History
    store=Store(tmp_path);core=FakeCore();core.directory=tmp_path/'core'
    gateway=Gateway('127.0.0.1');controller=Controller(store,core,gateway,FakeProber(core))
    for g in store.state['groups']:g['paused']=True
    history=History(tmp_path);history.start();controller.history=history
    history.record('retained',None,'state',{'reason':'before restore'})
    async def listen(group,port):gateway.leases.setdefault(group,Lease())
    gateway.listen=listen
    for g in store.state['groups']:await listen(g['id'],g['port'])
    manager=Management(controller,'local-fixture-password-long','http://localhost',secure=False)
    backup=store.backup();backup['providers']={}
    if remove_group:backup['state']['groups'].pop()
    old_epoch=controller.history_epoch
    class Request:
        async def json(self):return backup
    response=await manager.restore(Request())
    job=json.loads(response.text)['job']
    await asyncio.gather(*list(manager.job_tasks))
    assert manager.jobs[job]['status']=='done',manager.jobs[job]
    assert not controller.maintenance and controller.history_epoch!=old_epoch
    await history.queue.join()
    assert history.query('retained',24)['records'][0]['reason']=='before restore'
    assert history.query('system',24)['restores']
    await gateway.close();await history.close();store.close()
