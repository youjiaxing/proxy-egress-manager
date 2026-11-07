import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from scheduler.probe import Prober


@pytest.mark.asyncio
async def test_country_cache_expiry_configuration_and_fail_closed():
    now = [0]
    requests = []
    status = [200]
    async def country(request):
        requests.append(request.path)
        return web.json_response({'country_code': 'US'} if status[0] == 200 else {'message': 'limited'}, status=status[0])
    app = web.Application()
    app.router.add_get('/{provider}/{ip}', country)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        p = Prober(clock=lambda: now[0])
        template = str(server.make_url('/one/'))+'{ip}'
        assert await p.country(session, template, '8.8.8.8') == ('US', None)
        assert await p.country(session, template, '8.8.8.8') == ('US', None)
        assert len(requests) == 1
        # A changed observed IP or service cannot inherit another cached answer.
        await p.country(session, template, '8.8.4.4')
        await p.country(session, template.replace('/one/', '/two/'), '8.8.8.8')
        assert len(requests) == 3
        now[0] = 21601
        status[0] = 429
        assert (await p.country(session, template, '8.8.8.8'))[0] is None
        assert (await p.country(session, template, '1.1.1.1'))[0] is None
        assert len(requests) == 4
        # Cooldown expires; failed metadata never becomes a successful identity.
        now[0] += 901
        status[0] = 200
        assert await p.country(session, template, '8.8.8.8') == ('US', None)
        assert len(requests) == 5


@pytest.mark.asyncio
async def test_country_rejects_error_payload_and_bounds_cache():
    async def country(request):
        return web.json_response({'country_code':'US', 'success':False})
    app=web.Application();app.router.add_get('/{ip}', country)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        p=Prober()
        template=str(server.make_url('/'))+'{ip}'
        assert (await p.country(session,template,'8.8.8.8'))[0] is None
        p.country_cache.clear()
        for i in range(512): p.country_cache[(template,str(i))]=(0,None,'failed')
        await p.country(session,template,'1.1.1.1')
        assert len(p.country_cache) <= 512
