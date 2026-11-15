"""Managed physical two-hop lines; no client-side chaining or peer identities."""
import uuid

from .model import Settings, TOKEN, username
from .topology import ws_path

MAX_CHAINS = 32


def credential(user_uuid, chain_id):
    return str(uuid.uuid5(uuid.UUID(user_uuid), chain_id))


def ready(node):
    return node['enabled'] and node['ready'] and node['acked'] == node['desired']


def validate(state):
    chains = state.get('chains', {})
    if not isinstance(chains, dict) or len(chains) > MAX_CHAINS:
        raise ValueError('chain limit reached (32)')
    names = {n['settings']['node_name'] for n in state['nodes'].values() if n['enabled']}
    pairs, ids = set(), set()
    for ident, chain in chains.items():
        username(ident)
        if not isinstance(chain, dict) or set(chain) != {'id', 'name', 'entry', 'exit'}:
            raise ValueError('invalid chain schema')
        Settings('validation.example', chain['name'])
        if not isinstance(chain['id'], str) or not TOKEN.fullmatch(chain['id']) or chain['id'] in ids:
            raise ValueError('invalid chain identity')
        pair = (chain['entry'], chain['exit'])
        if pair[0] == pair[1] or any(n not in state['nodes'] for n in pair):
            raise ValueError('chain requires distinct managed physical entry and exit nodes')
        if pair in pairs or chain['name'] in names:
            raise ValueError('duplicate chain endpoints or line name')
        names.add(chain['name'])
        pairs.add(pair)
        ids.add(chain['id'])
    return state


def entry_routes(state, node):
    """Only the entry's physical exits, never names, peer or subscription tokens."""
    result = []
    for chain in state.get('chains', {}).values():
        if state['nodes'][chain['entry']] == node and node['enabled']:
            exit_node = state['nodes'][chain['exit']]
            if exit_node['enabled']:
                endpoint = Settings(**exit_node['settings'])
                result.append({'id': chain['id'], 'exit': {'domain': endpoint.domain, 'port': endpoint.port,
                                                          'ws_path': exit_node['ws_path']}})
    return result


def validate_routes(routes):
    if not isinstance(routes, list) or not 1 <= len(routes) <= MAX_CHAINS:
        raise ValueError('invalid chain routes')
    ids, endpoints = set(), set()
    for route in routes:
        if not isinstance(route, dict) or set(route) != {'id', 'exit'}:
            raise ValueError('invalid chain route schema')
        if not isinstance(route['id'], str) or not TOKEN.fullmatch(route['id']) or route['id'] in ids:
            raise ValueError('invalid chain route ID')
        endpoint = route['exit']
        if not isinstance(endpoint, dict) or set(endpoint) != {'domain', 'port', 'ws_path'}:
            raise ValueError('invalid chain exit schema')
        settings = Settings(endpoint['domain'], 'Exit', port=endpoint['port'])
        ws_path(endpoint['ws_path'])
        pair = (settings.domain, settings.port)
        if pair in endpoints:
            raise ValueError('duplicate chain exit')
        ids.add(route['id'])
        endpoints.add(pair)
