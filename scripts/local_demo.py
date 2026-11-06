"""UI-only local preview with isolated data and no granted proxy leases."""
import asyncio
import argparse
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

async def main(sample_source=False):
    with tempfile.TemporaryDirectory(prefix='pem-preview-') as directory:
        store=Store(directory)
        if sample_source:
            store.state['sources'] = [{'slot':1, 'name':'示例订阅', 'url':'https://example.invalid/sub?token=preview-only',
                                       'enabled':False, 'interval':21600, 'refreshed_at':0, 'error':None}]
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
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample-source',action='store_true',help='加入停用的虚构来源，用于编辑交互检查')
    asyncio.run(main(parser.parse_args().sample_source))
