"""Durable group scope identities; display names and reusable slots never grant permission."""
import copy
import math
import secrets


def identity():
    return secrets.token_hex(16)


def normalize(state):
    old = state.get('schema', 1) == 1
    if state.get('schema', 1) not in (1, 2):
        raise ValueError('不兼容的状态版本')
    if not old:
        validate_v2(state)
        return state
    for source in state['sources']:
        source.setdefault('uid', identity())
    for group in state['groups']:
        group.setdefault('uid', identity())
        if old:
            group['scope'] = {'mode': 'all', 'ids': []}
        group.setdefault('scope', {'mode': 'all', 'ids': []})
        group.setdefault('retry_step', 0)
        group.setdefault('next_retry_at', 0)
    state['schema'] = 2
    return state


def source_for(state, node):
    return next((s for s in state['sources'] if s['slot'] == node['source']), None)


def node_ref(state, node):
    source = source_for(state, node)
    return (source or {}).get('uid', '') + '/' + node['id']


def in_scope(state, group, node):
    scope = group.get('scope', {'mode': 'all', 'ids': []})
    source = source_for(state, node)
    if not source:
        return False
    return scope['mode'] == 'all' or (scope['mode'] == 'sources' and source.get('uid') in scope['ids']) or (scope['mode'] == 'nodes' and node_ref(state, node) in scope['ids'])


def eligible(state, group, node):
    source = source_for(state, node)
    return bool(source and source['enabled'] and not node['disabled'] and in_scope(state, group, node))


def validate_scope(scope, state=None, allow_stale=False):
    if not isinstance(scope, dict) or set(scope) != {'mode', 'ids'} or scope.get('mode') not in ('all', 'sources', 'nodes'):
        raise ValueError('可用节点范围格式无效')
    ids = scope['ids']
    if not isinstance(ids, list) or len(ids) > 5000 or any(not isinstance(x, str) or not 1 <= len(x) <= 200 for x in ids) or len(set(ids)) != len(ids):
        raise ValueError('可用节点选择无效')
    if scope['mode'] == 'all' and ids:
        raise ValueError('全部订阅模式不能附带选择')
    if scope['mode'] != 'all' and not ids and not allow_stale:
        raise ValueError('请至少选择一个订阅或节点')
    if state is not None and not allow_stale:
        valid = {s['uid'] for s in state['sources']} if scope['mode'] == 'sources' else {node_ref(state, n) for n in state['nodes'].values()}
        if scope['mode'] != 'all' and not set(ids) <= valid:
            raise ValueError('所选订阅或节点已改变，请刷新后重新选择')
    return copy.deepcopy(scope)


def validate_v2(state):
    uids = []
    for obj in state['sources'] + state['groups']:
        uid = obj.get('uid')
        if not isinstance(uid, str) or len(uid) != 32 or any(c not in '0123456789abcdef' for c in uid) or uid in uids:
            raise ValueError('来源或分组代际标识无效')
        uids.append(uid)
    for group in state['groups']:
        validate_scope(group.get('scope'), allow_stale=True)
        step, retry = group.get('retry_step'), group.get('next_retry_at')
        if isinstance(step, bool) or not isinstance(step, int) or not 0 <= step <= 3 or isinstance(retry, bool) or not isinstance(retry, (int, float)) or not math.isfinite(retry) or retry < 0:
            raise ValueError('分组重试状态无效')
