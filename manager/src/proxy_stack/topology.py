"""v1.0.0 topology initialization and validation."""
import time
from dataclasses import asdict

from .model import Settings, TOKEN


def ws_path(value):
    if not isinstance(value, str) or not value.startswith('/ws/') or not TOKEN.fullmatch(value[4:]):
        raise ValueError('invalid WebSocket path')
    return value


def initialize(state):
    settings = Settings(**state['settings'])
    state['settings'] = asdict(settings)
    state['generation'] = 1
    state['nodes'] = {}
    if settings.role == 'hub' and settings.local_node:
        state['nodes']['local'] = {
            'settings': asdict(settings), 'ws_path': ws_path(state['ws_path']),
            'enabled': True, 'desired': 1, 'acked': 0, 'ready': False, 'created': time.time(),
        }
    return state


def validate(state):
    if 'nodes' not in state:
        raise ValueError('invalid v1.0.0 topology; initialize a fresh v1.0.0 installation')
    if type(state.get('generation')) is not int or state['generation'] < 1 or len(state['nodes']) > 128:
        raise ValueError('invalid topology')
    names = set()
    for identifier, node in state['nodes'].items():
        if identifier != 'local' and not TOKEN.fullmatch(identifier):
            raise ValueError('invalid node ID')
        endpoint = Settings(**node['settings'])
        ws_path(node['ws_path'])
        if node['enabled']:
            if endpoint.node_name in names:
                raise ValueError('node names must be unique')
            names.add(endpoint.node_name)
        if any(type(node.get(k)) is not int or not 0 <= node[k] <= state['generation'] for k in ('desired', 'acked')):
            raise ValueError('invalid node generation')
    return state


def invalidate(state):
    state['generation'] += 1
    for node in state['nodes'].values():
        if node['enabled']:
            node['desired'] = state['generation']
            node['ready'] = False
