"""Offline, single-controller backup/restore. Stop the application first."""
import argparse
import fcntl
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scheduler.core import atomic_write
from scheduler.store import STATE_FILE, prepare_storage
from scheduler.policy import normalize, identity
from web.server import validate_backup
from scheduler.maintenance import marker_path, begin as begin_maintenance, finish as finish_maintenance


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['backup','restore'])
    parser.add_argument('file', type=Path)
    args = parser.parse_args()
    directory = Path(os.environ.get('DATA_DIR','data'))
    core = Path(os.environ.get('CORE_CONFIG_DIR','data/core'))
    lock = (directory/'controller.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('应用仍在运行；先停止全部网关和控制者，不允许在线升级备份/恢复')
    path = directory/STATE_FILE
    if args.command == 'backup':
        if marker_path(directory).exists():
            raise SystemExit('恢复未完成，不能备份不一致状态；请先完成有效恢复')
        if not path.exists():
            raise SystemExit('新版运行库不存在；升级前请使用旧版本备份命令')
        db = sqlite3.connect(str(path))
        snapshot = sqlite3.connect(':memory:')
        db.backup(snapshot)
        state = json.loads(snapshot.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        events = list(snapshot.execute('SELECT time,kind,group_id,message FROM events'))
        db.close(); snapshot.close()
        normalize(state)
        payload = {'format':2,'state':state,'events':events,
                   'providers':{str(s['slot']):(core/'providers'/('s%02d.yaml'%s['slot'])).read_text() for s in state['sources']}}
        atomic_write(args.file,json.dumps(payload,ensure_ascii=False))
        print('一致备份已保存（含秘密），请限制访问；部署 .env/secret 文件须另行安全备份。')
    else:
        backup = json.loads(args.file.read_text())
        validate_backup(backup,int(os.environ.get('GROUP_CAPACITY','32')),int(os.environ.get('SOURCE_CAPACITY','16')))
        normalize(backup['state'])
        from scheduler.policy import validate_v2
        validate_v2(backup['state'])
        # Publish the restore barrier BEFORE migration can expose an intermediate legacy snapshot.
        begin_maintenance(directory)
        if not (directory/'state-v2.ready').exists():
            prepare_storage(directory)
        fd, temp = tempfile.mkstemp(dir=directory, prefix='.restore-')
        os.close(fd)
        try:
            state = normalize(backup['state'])
            state['quality_epoch'] = identity()
            for group in state['groups']:
                group.update(status='paused',reason='离线恢复后等待重验',failure_since=None)
            db = sqlite3.connect(temp)
            db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL)')
            db.execute('INSERT INTO state VALUES(1,?)',(json.dumps(state,ensure_ascii=False),))
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,time REAL,kind TEXT,group_id TEXT,message TEXT)')
            for event in backup.get('events',[])[:5000]:
                db.execute('INSERT INTO events(time,kind,group_id,message) VALUES(?,?,?,?)',event)
            db.commit(); db.close()
            for slot, content in backup['providers'].items():
                if str(int(slot)) != slot or not 1 <= int(slot) <= int(os.environ.get('SOURCE_CAPACITY','16')):
                    raise ValueError('无效 provider 槽位')
                atomic_write(core/'providers'/('s%02d.yaml'%int(slot)),content)
            for suffix in ('-wal','-shm'):
                (directory/(STATE_FILE+suffix)).unlink(missing_ok=True)
            os.replace(temp,path)
            dirfd=os.open(str(directory),os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(dirfd)
            finally: os.close(dirfd)
            # Local quality history survives configuration restore and records the boundary.
            from scheduler.history import History
            import time
            history = History(directory)
            history.write([(time.time(), 'system', None, 'restore', json.dumps({'reason':'离线恢复配置；现有质量历史保留，后续样本重新分段'}))])
            try:
                from scheduler.traffic import mark_restore
                mark_restore(directory)
            except Exception:
                print('流量统计恢复标记未保存；原流量库保留，可能缺少恢复边界。')
            finish_maintenance(directory)
            print('已恢复到备份点。启动前确认旧镜像/配置匹配；授权不恢复，必须重新验证。')
        finally:
            Path(temp).unlink(missing_ok=True)
    lock.close()


if __name__=='__main__':
    main()
