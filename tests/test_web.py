import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from web.server import Management
from scheduler.store import Store
from scheduler.controller import Controller
from gateway.server import Gateway
from test_controller import FakeCore, FakeProber


async def make_client(tmp_path, password='test-password-long-enough'):
    core, store = FakeCore(), Store(tmp_path)
    gateway = Gateway('127.0.0.1')
    controller = Controller(store,core,gateway,FakeProber(core))
    for g in controller.state['groups']:
        gateway.leases[g['id']] = __import__('gateway.server',fromlist=['Lease']).Lease()
    management = Management(controller,password,'http://localhost',secure=False)
    client = TestClient(TestServer(management.app),cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    return client, management, store


@pytest.mark.asyncio
async def test_login_csrf_secret_redaction_and_headers(tmp_path):
    client, manager, store = await make_client(tmp_path)
    try:
        assert (await client.get('/api/state')).status == 401
        assert (await client.post('/api/login',json={'password':'test-password-long-enough'})).status == 403
        response = await client.post('/api/login',json={'password':'test-password-long-enough'},headers={'Origin':'http://localhost'})
        assert response.status==200
        csrf=(await response.json())['csrf']
        store.state['sources']=[{'slot':1,'name':'source','url':'https://secret.example/token?key=private','enabled':True}]
        store.state['settings']['alert_url']='https://secret.example/alert'
        response=await client.get('/api/state')
        text=await response.text()
        assert 'private' not in text and 'secret.example' not in text
        assert response.headers['Cache-Control']=='no-store'
        assert (await client.post('/api/logout',json={},headers={'Origin':'http://localhost'})).status==403
        assert (await client.post('/api/logout',json={},headers={'Origin':'http://localhost','X-CSRF-Token':csrf})).status==200
        assert (await client.get('/api/state')).status==401
    finally:
        await client.close(); store.close()


@pytest.mark.asyncio
async def test_no_default_password_and_login_throttle(tmp_path):
    client, manager, store = await make_client(tmp_path,password='')
    try:
        assert (await client.post('/api/login',json={'password':'admin'},headers={'Origin':'http://localhost'})).status==503
    finally:
        await client.close(); store.close()


@pytest.mark.asyncio
async def test_correct_password_not_locked_out_by_shared_proxy_failures(tmp_path):
    client, manager, store=await make_client(tmp_path)
    try:
        # All clients behind a reverse proxy share request.remote.
        manager.attempts['127.0.0.1']=(100,__import__('time').monotonic())
        response=await client.post('/api/login',json={'password':'test-password-long-enough'},headers={'Origin':'http://localhost'})
        assert response.status==200
        assert '127.0.0.1' not in manager.attempts
    finally:
        await client.close();store.close()
