"""Start the real application and real pinned core, using only temporary state."""
import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import aiohttp
import pytest
import yaml
from scheduler.core import Core, atomic_write


@pytest.mark.asyncio
async def test_application_process_login_health_and_shutdown(tmp_path):
    binary=os.environ.get('MIHOMO_BIN')
    if not binary:
        pytest.skip('MIHOMO_BIN not provided')
    for port in (1081,1082,20000,20001,20002,29091):
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            try:
                check.bind(('127.0.0.1',port))
            except OSError:
                pytest.skip('local test port occupied; do not disrupt existing services')
    with socket.socket() as check:
        check.bind(('127.0.0.1',0))
        webport=check.getsockname()[1]
    directory=tmp_path/'core'
    core=Core(directory,secret='isolated-app-test-core-secret-2026',capacity=2,source_capacity=2)
    core.initialize()
    config=yaml.safe_load((directory/'config.yaml').read_text())
    config['external-controller']='127.0.0.1:29091'
    for listener in config['listeners']:
        listener['listen']='127.0.0.1'
    atomic_write(directory/'config.yaml',yaml.safe_dump(config))
    corelog=(tmp_path/'core.log').open('w')
    applog=(tmp_path/'app.log').open('w')
    kernel=subprocess.Popen([binary,'-d',str(directory)],stdout=corelog,stderr=corelog)
    url='http://127.0.0.1:%s'%webport
    env={**os.environ,'DATA_DIR':str(tmp_path/'state'),'CORE_CONFIG_DIR':str(directory),
         'CORE_API':'http://127.0.0.1:29091','CORE_HOST':'127.0.0.1',
         'CORE_SECRET':core.secret,'ADMIN_PASSWORD':'isolated-app-test-password-2026',
         'WEB_BIND':'127.0.0.1','SOCKS_BIND':'127.0.0.1','WEB_PORT':str(webport),
         'PUBLIC_URL':url,'ALLOW_LOCAL_HTTP':'1','GROUP_CAPACITY':'2','SOURCE_CAPACITY':'2'}
    app=subprocess.Popen([sys.executable,'main.py'],cwd=Path(__file__).resolve().parents[1],env=env,stdout=applog,stderr=applog)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),timeout=aiohttp.ClientTimeout(total=1)) as client:
            for _ in range(100):
                try:
                    async with client.get(url+'/healthz') as response:
                        if response.status==200:
                            break
                except Exception:
                    pass
                if app.poll() is not None:
                    pytest.fail((tmp_path/'app.log').read_text())
                await asyncio.sleep(.05)
            else:
                pytest.fail('application did not become ready: '+(tmp_path/'app.log').read_text())
            async with client.get(url+'/api/state') as response:
                assert response.status==401
            async with client.post(url+'/api/login',json={'password':env['ADMIN_PASSWORD']},headers={'Origin':url}) as response:
                assert response.status==200
                csrf=(await response.json())['csrf']
            async with client.get(url+'/api/state') as response:
                state=await response.json()
                assert len(state['groups'])==2
                assert all(not g['lease_valid'] for g in state['groups'])
                assert state['health']['scheduler_valid']
            async with client.post(url+'/api/backup',json={},headers={'Origin':url,'X-CSRF-Token':csrf}) as response:
                assert response.status==200
                assert (await response.json())['format']==1
            # No subscription means no proxy transfer authorization, even with healthy core/UI.
            reader,writer=await asyncio.open_connection('127.0.0.1',1081)
            writer.write(b'\x05\x01\x00'); await writer.drain()
            try:
                assert await asyncio.wait_for(reader.read(),1)==b''
            except ConnectionResetError:
                pass  # Immediate refusal may send RST when greeting bytes are unread.
            writer.close()
        app.terminate()
        assert await asyncio.to_thread(app.wait,5)==0
    finally:
        for process in (app,kernel):
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait,5)
                except subprocess.TimeoutExpired:
                    process.kill(); await asyncio.to_thread(process.wait)
        corelog.close(); applog.close()
