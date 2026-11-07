import argparse
import asyncio
import fcntl
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from gateway.server import Gateway
from scheduler.controller import Controller
from scheduler.core import Core
from scheduler.store import Store
from scheduler.history import History
from scheduler.traffic import Traffic
from scheduler.diagnostics import Diagnostics, diagnostic_core
from web.server import Management


def secret(name):
    filename = os.environ.get(name + "_FILE")
    return Path(filename).read_text().strip() if filename else os.environ.get(name, "")


def core_from_env():
    return Core(os.environ.get("CORE_CONFIG_DIR", "data/core"),
                os.environ.get("CORE_API", "http://mihomo:9090"), secret("CORE_SECRET"),
                os.environ.get("CORE_HOST", "mihomo"), int(os.environ.get("GROUP_CAPACITY", "32")),
                int(os.environ.get("SOURCE_CAPACITY", "16")))


async def serve():
    os.umask(0o077)
    directory = Path(os.environ.get("DATA_DIR", "data"))
    directory.mkdir(parents=True, exist_ok=True)
    lockfile = (directory / "controller.lock").open("w")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("同一数据目录已有控制者，拒绝并行启动")
    core = core_from_env()
    if len(core.secret) < 24:
        raise SystemExit("CORE_SECRET 至少 24 字符；不能使用默认秘密")
    public_url = os.environ.get("PUBLIC_URL", "")
    parsed_url = urlparse(public_url)
    if not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.path not in ("", "/") or parsed_url.query or parsed_url.fragment:
        raise SystemExit("PUBLIC_URL 必须是无路径、查询或凭据的管理 origin")
    development = os.environ.get("ALLOW_LOCAL_HTTP") == "1" and public_url.startswith("http://127.0.0.1:")
    if not public_url.startswith("https://") and not (development and public_url.startswith("http://127.0.0.1:")):
        raise SystemExit("PUBLIC_URL 必须为 HTTPS；本地测试可显式启用回环 HTTP")
    password = secret("ADMIN_PASSWORD")
    if password and len(password) < 16:
        raise SystemExit("管理员密码至少 16 字符")
    store = Store(directory)
    gateway = Gateway(os.environ.get("SOCKS_BIND", "0.0.0.0"), os.environ.get("UDP_ADVERTISE_HOST"))
    controller = Controller(store, core, gateway)
    traffic = Traffic(store.directory)
    controller.traffic = gateway.traffic = traffic
    traffic.start()
    history = History(store.directory)
    controller.history = history
    history.start()
    management = Management(controller, password, public_url, secure=not development)
    diag = None
    if os.environ.get('DIAG_CONFIG_DIR'):
        diag = diagnostic_core(os.environ['DIAG_CONFIG_DIR'],core.secret,os.environ.get('DIAG_API','http://diagnostic-core:9090'),os.environ.get('DIAG_HOST','diagnostic-core'))
        management.diagnostic = Diagnostics(controller,diag,history)
    runner = web.AppRunner(management.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, os.environ.get("WEB_BIND", "0.0.0.0"), int(os.environ.get("WEB_PORT", "8080")))
    await site.start()
    await controller.start()
    print("proxy-egress-manager started; all groups require fresh validation", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        await controller.stop()
        await history.close()
        await traffic.close()
        if diag: await diag.close()
        store.close()
        lockfile.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "init-core", "healthcheck"], nargs="?", default="serve")
    args = parser.parse_args()
    if args.command == "init-core":
        os.umask(0o077)
        core = core_from_env()
        if len(core.secret) < 24:
            raise SystemExit("CORE_SECRET 至少 24 字符")
        core.initialize()
        if os.environ.get("DIAG_CONFIG_DIR"):
            diagnostic_core(os.environ["DIAG_CONFIG_DIR"],core.secret).initialize()
    elif args.command == "healthcheck":
        import urllib.request
        try:
            urllib.request.urlopen("http://127.0.0.1:%s/healthz" % os.environ.get("WEB_PORT", "8080"), timeout=2)
        except Exception:
            sys.exit(1)
    else:
        asyncio.run(serve())


if __name__ == "__main__":
    main()
