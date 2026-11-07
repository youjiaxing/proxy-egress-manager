"""Local loopback comparison, not a production capacity or latency guarantee."""
import asyncio
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import types

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gateway.server import Gateway
from gateway.socks import open_socks, read_address, pack_address
from scheduler.traffic import Traffic


async def main():
    baseline=types.ModuleType('gateway_baseline');sys.modules[baseline.__name__]=baseline
    exec(subprocess.check_output(['git','show','bc5bf37:gateway/server.py']),baseline.__dict__)
    connections=set()
    async def echo(r,w):
        task=asyncio.current_task();connections.add(task)
        try:
            header=await r.readexactly(2);await r.readexactly(header[1]);w.write(b'\5\0');await w.drain()
            await r.readexactly(3);await read_address(r);w.write(b'\5\0\0'+pack_address('127.0.0.1',1));await w.drain()
            while data:=await r.read(65536):w.write(data);await w.drain()
        except (OSError,asyncio.IncompleteReadError):pass
        finally:w.close();connections.discard(task)
    server=await asyncio.start_server(echo,'127.0.0.1',0);upstream=server.sockets[0].getsockname()[:2]
    results={'baseline':[],'metered':[]}
    try:
        with tempfile.TemporaryDirectory(prefix='pem-counter-benchmark-') as root:
            meter=Traffic(Path(root));context=('fixture-group','fixture-source/node','business','{"group":"fixture","node":"fixture","source":"fixture"}')
            for iteration in range(8):
                for mode in (('baseline','metered') if iteration%2==0 else ('metered','baseline')):
                    g=(baseline.Gateway if mode=='baseline' else Gateway)('127.0.0.1');g.pulse(60)
                    if mode=='metered':g.traffic=meter
                    await g.listen('1',0)
                    if mode=='metered':g.grant('1',upstream,g.clock()+60,context)
                    else:g.grant('1',upstream,g.clock()+60)
                    r,w,_=await open_socks('127.0.0.1',g.servers['1'].sockets[0].getsockname()[1],1,'example.com',443)
                    amount=64*1024*1024;chunk=b'x'*65536
                    async def receive():
                        left=amount
                        while left:
                            data=await r.read(min(65536,left));assert data;left-=len(data)
                    wall=time.perf_counter();cpu=time.process_time();reader=asyncio.create_task(receive())
                    for _ in range(amount//len(chunk)):w.write(chunk);await w.drain()
                    await reader
                    elapsed=time.perf_counter()-wall;cpu=time.process_time()-cpu
                    if iteration:results[mode].append({'MiB_s':64/elapsed,'cpu_s':cpu})
                    w.close();await g.close()
            print(json.dumps({k:{'median_MiB_s':round(statistics.median(r['MiB_s'] for r in v),1),'median_cpu_s':round(statistics.median(r['cpu_s'] for r in v),4)} for k,v in results.items()}))
            print('dirty aggregate rows=',len(meter.pending),' (independent of connection/chunk count)')
            start=time.perf_counter();await meter.flush();print('single flush_ms=',round((time.perf_counter()-start)*1000,2),'db_bytes=',meter.path.stat().st_size)
            await meter.close()
    finally:
        server.close();await server.wait_closed()
        for task in list(connections):task.cancel()
        await asyncio.gather(*connections,return_exceptions=True)

asyncio.run(main())
