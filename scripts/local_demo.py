"""UI-only local preview with isolated data and no granted proxy leases."""
import asyncio
import tempfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from gateway.server import Gateway, Lease
from scheduler.store import Store
from scheduler.controller import Controller
from scheduler.core import Core
from web.server import Management

async def main():
    with tempfile.TemporaryDirectory(prefix='pem-preview-') as directory:
        store=Store(directory)
        core=Core(directory)
        gateway=Gateway('127.0.0.1')
        for group in store.state['groups']:
            gateway.leases[group['id']]=Lease()
        controller=Controller(store,core,gateway)
        app=Management(controller,'local-preview-password-2026','http://127.0.0.1:18080',secure=False)
        runner=web.AppRunner(app.app,access_log=None)
        await runner.setup()
        await web.TCPSite(runner,'127.0.0.1',18080).start()
        print('Isolated UI at http://127.0.0.1:18080; no scheduler/listening proxy',flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            store.close()

if __name__=='__main__':
    asyncio.run(main())
