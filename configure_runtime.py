#!/usr/bin/env python3
"""Explicit, reversible opt-in for Codex auto review and native Jev hooks."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tomllib

import tomlkit
import install as core

STATE = '.agent-kit/runtime.json'
ROOT = Path(__file__).resolve().parent
TOOLS = ['route_work', 'review_change', 'choose_file', 'choose_tool', 'retry_decision',
         'choose_action', 'pre_tool', 'post_tool', 'reviewed_retry', 'report_outcome',
         'routing_context']


def check_path(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise core.KitError(f'Refusing symlink: {item}')


def read(path):
    check_path(path)
    return path.read_bytes() if path.exists() else None


def value(doc, keys):
    item = core.lookup(doc, keys)
    return {'present': False} if item is core.MISSING else {'present': True, 'value': item}


def restore(doc, keys, record):
    core.assign(doc, keys, record['value'] if record['present'] else core.MISSING)


def own_key(doc, state, keys, installed):
    name = '.'.join(keys)
    if name not in state['keys']:
        state['keys'][name] = {'original': value(doc, keys)}
        for length in range(1, len(keys)):
            parent = keys[:length]
            if core.lookup(doc, parent) is core.MISSING and parent not in state['created_tables']:
                state['created_tables'].append(parent)
    core.assign(doc, keys, installed)
    state['keys'][name]['installed'] = value(doc, keys)


def hook_group(event):
    data = {'session_id': '${session_id}', 'cwd': '${cwd}', 'turn_id': '${turn_id}',
            'tool_use_id': '${tool_use_id}',
            'tool_name': '${tool_name}', 'tool_input': '${tool_input}'}
    if event == 'PostToolUse':
        data['tool_response'] = '${tool_response}'
    return {'matcher': '*', 'hooks': [{'type': 'mcp_tool', 'server': 'jev',
            'tool': 'pre_tool' if event == 'PreToolUse' else 'post_tool',
            'timeout': 20, 'statusMessage': 'Jev routing', 'input': data}]}


def is_own(group, event):
    return any(isinstance(h, dict) and h.get('server') == 'jev' and
               h.get('tool') == ('pre_tool' if event == 'PreToolUse' else 'post_tool')
               for h in group.get('hooks', []))


def verify(doc, state):
    for name, record in state['keys'].items():
        if value(doc, name.split('.')) != record['installed']:
            raise core.KitError(f'Local edit detected at {name}; restore the installed value first.')
    for event, record in state['hooks'].items():
        groups = core.lookup(doc, ['hooks', event])
        matches = [] if groups is core.MISSING else [g for g in groups if is_own(g, event)]
        if matches != [record['installed']]:
            raise core.KitError(f'Local edit detected in Jev {event} hook; restore it first.')


def add_hooks(doc, state):
    if 'hooks' not in doc:
        doc['hooks'] = tomlkit.table()
        state['created_tables'].append(['hooks'])
    for event in ('PreToolUse', 'PostToolUse'):
        group = hook_group(event)
        groups = core.lookup(doc, ['hooks', event])
        groups = [] if groups is core.MISSING else groups
        if not isinstance(groups, list):
            raise core.KitError(f'Expected hooks.{event} array.')
        matches = [g for g in groups if is_own(g, event)]
        if event not in state['hooks']:
            if matches and matches != [group]:
                raise core.KitError(f'Existing Jev {event} hook conflicts; merge it manually first.')
            state['hooks'][event] = {'original': matches, 'installed': group,
                                     'array_present': core.lookup(doc, ['hooks', event]) is not core.MISSING}
        if matches == [group]:
            continue
        if matches:
            groups = [group if is_own(item, event) else item for item in groups]
        else:
            groups.append(group)
        state['hooks'][event]['installed'] = group
        array = tomlkit.aot()
        for item in groups:
            array.append(tomlkit.item(item))
        doc['hooks'][event] = array


def commit(home, changes, dry_run):
    pending = {name: data for name, data in changes.items() if read(home / name) != data}
    for name in pending:
        print(f"{'Would update' if dry_run else 'Update'}: {name}")
    if dry_run or not pending:
        return
    originals = {name: read(home / name) for name in pending}
    backup = home / '.agent-kit/runtime-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    check_path(backup)
    backup.mkdir(parents=True, mode=0o700)
    for directory in (home / '.agent-kit', backup.parent, backup):
        os.chmod(directory, 0o700)
    for name, data in originals.items():
        if data is not None:
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            core.atomic_write(target, data)
            os.chmod(target, 0o600)
    applied = []
    try:
        for name, data in pending.items():
            target = home / name
            if read(target) != originals[name]:
                raise core.KitError(f'File changed during configuration: {name}')
            # Include the attempted write in rollback in case replacement succeeds then raises.
            applied.append(name)
            if data is None:
                target.unlink(missing_ok=True)
            else:
                core.atomic_write(target, data)
                if name == STATE:
                    os.chmod(target, 0o600)
    except Exception:
        for name in reversed(applied):
            target = home / name
            check_path(target)
            data = originals[name]
            if read(target) == data:
                continue
            if data is None:
                target.unlink(missing_ok=True)
            else:
                core.atomic_write(target, data)
        raise


def run(args):
    home = Path(args.codex_home).expanduser().absolute()
    check_path(home)
    check_path(home / '.agent-kit/runtime-backups')
    raw = read(home / 'config.toml')
    doc = tomlkit.parse((raw or b'').decode('utf-8'))
    tomllib.loads((raw or b'').decode('utf-8'))
    stored = read(home / STATE)
    state = json.loads(stored) if stored else {'format': 1, 'keys': {}, 'hooks': {}, 'created_tables': [], 'features': []}
    if state.get('format') != 1:
        raise core.KitError('Unsupported runtime state format.')
    verify(doc, state)
    if args.command == 'status':
        print('Configured features: ' + (', '.join(state['features']) or 'none'))
        if 'jev' in state['features']:
            print('Native hooks require /hooks trust and a new Codex session; runtime activity is not verified.')
        return
    if args.command == 'install':
        if not args.auto_review and not args.jev:
            raise core.KitError('Choose --auto-review and/or --jev explicitly.')
        if args.auto_review:
            for key, setting in [('approval_policy', 'on-request'), ('approvals_reviewer', 'auto_review'), ('sandbox_mode', 'workspace-write')]:
                own_key(doc, state, [key], setting)
            if 'auto-review' not in state['features']:
                state['features'].append('auto-review')
        if args.jev:
            if read(home / 'hooks.json') is not None:
                raise core.KitError('hooks.json exists: merge it into native config.toml hooks and remove hooks.json before enabling Jev.')
            if not args.jev_python:
                raise core.KitError('--jev requires --jev-python with an absolute executable path.')
            python = Path(args.jev_python).expanduser()
            if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
                raise core.KitError('--jev-python must be an existing absolute executable path.')
            env_vars = core.lookup(doc, ['mcp_servers', 'jev', 'env_vars'])
            env_vars = [] if env_vars is core.MISSING else env_vars
            if not isinstance(env_vars, list) or not all(isinstance(item, str) for item in env_vars):
                raise core.KitError('Expected mcp_servers.jev.env_vars to be an array of names.')
            env_vars = list(dict.fromkeys([*env_vars, 'TYPESAFE_API_KEY']))
            for keys, setting in [(['features', 'hooks'], True),
                (['mcp_servers', 'jev', 'env_vars'], env_vars),
                (['mcp_servers', 'jev', 'command'], str(python)),
                (['mcp_servers', 'jev', 'args'], [str(ROOT / 'integrations/jev/server.py')]),
                (['mcp_servers', 'jev', 'enabled'], True),
                (['mcp_servers', 'jev', 'enabled_tools'], TOOLS),
                (['mcp_servers', 'jev', 'env', 'JEV_STATE_PATH'], str(home / '.agent-kit/jev-routing.sqlite'))]:
                own_key(doc, state, keys, setting)
            add_hooks(doc, state)
            if 'jev' not in state['features']:
                state['features'].append('jev')
        changes = {'config.toml': tomlkit.dumps(doc).encode(), STATE: (json.dumps(state, indent=2) + '\n').encode()}
    else:
        if not stored:
            print('Runtime options are not configured.')
            return
        for name, record in state['keys'].items():
            restore(doc, name.split('.'), record['original'])
        for event, record in state['hooks'].items():
            groups = [g for g in core.lookup(doc, ['hooks', event]) if not is_own(g, event)] + record['original']
            if not groups and not record['array_present']:
                core.assign(doc, ['hooks', event])
            else:
                array = tomlkit.aot()
                for group in groups:
                    array.append(tomlkit.item(group))
                doc['hooks'][event] = array
        for keys in sorted(state['created_tables'], key=len, reverse=True):
            if core.lookup(doc, keys) == {}:
                core.assign(doc, keys)
        changes = {'config.toml': tomlkit.dumps(doc).encode(), STATE: None}
    commit(home, changes, args.dry_run)
    if 'jev' in state['features'] and args.command == 'install':
        print('Jev configured. Trust hooks using /hooks and start a new session; runtime activity is not verified.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('install', 'status', 'uninstall'):
        command = commands.add_parser(name)
        command.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', '~/.codex'))
        command.add_argument('--dry-run', action='store_true')
        if name == 'install':
            command.add_argument('--auto-review', action='store_true')
            command.add_argument('--jev', action='store_true')
            command.add_argument('--jev-python')
    try:
        run(parser.parse_args(argv))
    except (core.KitError, OSError, ValueError, TypeError, KeyError) as exc:
        # Parser/config exceptions may embed credential-bearing config text.
        if isinstance(exc, core.KitError):
            print(f'Error: {exc}', file=sys.stderr)
        else:
            print(f'Error: runtime configuration failed ({type(exc).__name__}); inspect configuration locally.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
