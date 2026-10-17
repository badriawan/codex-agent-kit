import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import configure_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'codex'
        self.home.mkdir()
        self.config = self.home / 'config.toml'

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = runtime.main([args[0], '--codex-home', str(self.home), *args[1:]])
        return code, output.getvalue()

    def install(self, *args):
        return self.call('install', '--jev', '--jev-python', sys.executable, *args)

    def parsed(self):
        return tomllib.loads(self.config.read_text())

    def test_fresh_env_vars_and_scoped_hooks_repeat_uninstall(self):
        self.assertEqual(self.install()[0], 0)
        doc = self.parsed()
        self.assertEqual(doc['mcp_servers']['jev']['env_vars'], ['TYPESAFE_API_KEY'])
        self.assertIn('routing_context', doc['mcp_servers']['jev']['enabled_tools'])
        for event in ('PreToolUse', 'PostToolUse'):
            template = doc['hooks'][event][0]['hooks'][0]['input']
            self.assertEqual(template['cwd'], '${cwd}')
            self.assertEqual(template['turn_id'], '${turn_id}')
        before = self.config.read_bytes()
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(before, self.config.read_bytes())
        self.assertEqual(self.call('uninstall')[0], 0)
        self.assertEqual(self.parsed(), {})

    def test_upgrade_owned_hooks_adds_scope_without_duplicates(self):
        self.assertEqual(self.install()[0], 0)
        doc = runtime.tomlkit.parse(self.config.read_text())
        state_path = self.home / runtime.STATE
        state = json.loads(state_path.read_text())
        for event in ('PreToolUse', 'PostToolUse'):
            for key in ('cwd', 'turn_id'):
                del doc['hooks'][event][0]['hooks'][0]['input'][key]
                del state['hooks'][event]['installed']['hooks'][0]['input'][key]
        self.config.write_text(runtime.tomlkit.dumps(doc))
        state_path.write_text(json.dumps(state))
        self.assertEqual(self.install()[0], 0)
        for event in ('PreToolUse', 'PostToolUse'):
            groups = self.parsed()['hooks'][event]
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]['hooks'][0]['input']['turn_id'], '${turn_id}')
        self.assertEqual(self.call('uninstall')[0], 0)
        self.assertEqual(self.parsed(), {})

    def test_upgrade_adds_context_tool_and_restores_original_allowlist(self):
        self.config.write_text('[mcp_servers.jev]\nenabled_tools=["route_work"]\n')
        self.assertEqual(self.install()[0], 0)
        doc = runtime.tomlkit.parse(self.config.read_text())
        state_path = self.home / runtime.STATE
        state = json.loads(state_path.read_text())
        old_tools = [name for name in runtime.TOOLS if name != 'routing_context']
        doc['mcp_servers']['jev']['enabled_tools'] = old_tools
        state['keys']['mcp_servers.jev.enabled_tools']['installed']['value'] = old_tools
        self.config.write_text(runtime.tomlkit.dumps(doc))
        state_path.write_text(json.dumps(state))
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.parsed()['mcp_servers']['jev']['enabled_tools'].count('routing_context'), 1)
        before = self.config.read_bytes()
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.call('uninstall')[0], 0)
        self.assertEqual(self.parsed(), {'mcp_servers': {'jev': {'enabled_tools': ['route_work']}}})

    def test_custom_env_vars_merge_preserves_secrets_and_restores(self):
        self.config.write_text('[mcp_servers.jev]\nenv_vars=["CUSTOM", "TYPESAFE_API_KEY", "CUSTOM"]\n[mcp_servers.jev.env]\nTYPESAFE_API_KEY="secret-test-key"\nOTHER="kept"\n')
        original = self.parsed()
        code, output = self.install()
        self.assertEqual(code, 0, output)
        self.assertNotIn('secret-test-key', output)
        doc = self.parsed()
        self.assertEqual(doc['mcp_servers']['jev']['env_vars'], ['CUSTOM', 'TYPESAFE_API_KEY'])
        self.assertEqual(doc['mcp_servers']['jev']['env']['TYPESAFE_API_KEY'], 'secret-test-key')
        self.assertEqual(doc['mcp_servers']['jev']['env']['OTHER'], 'kept')
        before = self.config.read_bytes()
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(before, self.config.read_bytes())
        self.assertEqual(self.call('uninstall')[0], 0)
        self.assertEqual(self.parsed(), original)

    def test_optional_permissions_and_core_state_untouched(self):
        self.config.write_text('approval_policy="never"\nsandbox_mode="danger-full-access"\n')
        state = self.home / '.agent-kit/manifest.json'
        state.parent.mkdir()
        state.write_text('{"format":1,"unrelated":true}')
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.parsed()['approval_policy'], 'never')
        self.assertEqual(self.parsed()['sandbox_mode'], 'danger-full-access')
        self.assertEqual(state.read_text(), '{"format":1,"unrelated":true}')

    def test_round_trip_preserves_env_hooks_and_unrelated_changes(self):
        self.config.write_text('model="existing"\n[mcp_servers.jev]\nstartup_timeout_sec=45\n[mcp_servers.jev.env]\nTYPESAFE_API_KEY="secret-test-key"\n[[hooks.PreToolUse]]\nmatcher="Bash"\n[[hooks.PreToolUse.hooks]]\ntype="command"\ncommand="echo existing"\n')
        code, output = self.install('--auto-review')
        self.assertEqual(code, 0, output)
        self.assertNotIn('secret-test-key', output)
        doc = self.parsed()
        self.assertEqual(doc['mcp_servers']['jev']['env']['TYPESAFE_API_KEY'], 'secret-test-key')
        self.assertEqual(doc['mcp_servers']['jev']['startup_timeout_sec'], 45)
        self.assertEqual(len(doc['hooks']['PreToolUse']), 2)
        self.assertEqual(doc['approval_policy'], 'on-request')
        with self.config.open('a') as stream:
            stream.write('\n[unrelated]\nnew=true\n')
        code, output = self.call('uninstall')
        self.assertEqual(code, 0, output)
        doc = self.parsed()
        self.assertEqual(doc['unrelated'], {'new': True})
        self.assertEqual(len(doc['hooks']['PreToolUse']), 1)
        self.assertNotIn('PostToolUse', doc['hooks'])
        self.assertEqual(doc['mcp_servers']['jev'], {'startup_timeout_sec':45, 'env': {'TYPESAFE_API_KEY':'secret-test-key'}})
        self.assertNotIn('approval_policy', doc)
        self.assertNotIn('secret-test-key', (self.home / runtime.STATE).read_text() if (self.home / runtime.STATE).exists() else '')

    def test_idempotent_extend_and_restore_original(self):
        self.config.write_text('approval_policy="never"\n')
        self.assertEqual(self.call('install', '--auto-review')[0], 0)
        self.assertEqual(self.install()[0], 0)
        before = self.config.read_bytes()
        self.assertEqual(self.install('--auto-review')[0], 0)
        self.assertEqual(before, self.config.read_bytes())
        self.assertEqual(len(self.parsed()['hooks']['PreToolUse']), 1)
        self.assertEqual(self.call('uninstall')[0], 0)
        self.assertEqual(self.parsed(), {'approval_policy':'never'})

    def test_owned_edit_rejected_without_writes(self):
        self.assertEqual(self.call('install', '--auto-review')[0], 0)
        self.config.write_text(self.config.read_text().replace('on-request', 'never'))
        before = self.config.read_bytes()
        self.assertEqual(self.call('uninstall')[0], 1)
        self.assertEqual(before, self.config.read_bytes())

    def test_hook_edit_rejected(self):
        self.assertEqual(self.install()[0], 0)
        self.config.write_text(self.config.read_text().replace('Jev routing', 'edited'))
        self.assertEqual(self.install()[0], 1)

    def test_dry_run_no_writes_and_status(self):
        self.assertEqual(self.install('--dry-run')[0], 0)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.call('status')[1].strip(), 'Configured features: none')

    def test_hooks_json_conflict_and_invalid_python(self):
        (self.home / 'hooks.json').write_text('{}')
        self.assertEqual(self.install()[0], 1)
        self.assertFalse(self.config.exists())
        (self.home / 'hooks.json').unlink()
        self.assertEqual(self.call('install', '--jev', '--jev-python', 'relative')[0], 1)

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink and permissions')
    def test_symlinks_and_private_backups(self):
        real = self.home.parent / 'real'
        real.mkdir()
        linked = self.home / '.agent-kit'
        linked.symlink_to(real, target_is_directory=True)
        self.assertEqual(self.install()[0], 1)
        linked.unlink()
        self.config.write_text('model="existing"\n')
        self.assertEqual(self.install()[0], 0)
        for entry in (self.home / '.agent-kit').rglob('*'):
            if entry.is_file():
                self.assertEqual(entry.stat().st_mode & 0o077, 0)
            elif entry.is_dir():
                self.assertEqual(entry.stat().st_mode & 0o077, 0)

    def test_update_repository_path_and_preserve_original_restore(self):
        self.config.write_text('[mcp_servers.jev]\ncommand="old-python"\nargs=["old-server"]\n')
        self.assertEqual(self.install()[0], 0)
        relocated_root = self.home.parent / 'another device' / 'agent-kit'
        with patch.object(runtime, 'ROOT', relocated_root):
            self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.parsed()['mcp_servers']['jev']['args'],
                         [str(relocated_root / 'integrations' / 'jev' / 'server.py')])
        self.assertEqual(self.call('uninstall')[0], 0)
        self.assertEqual(self.parsed(), {'mcp_servers': {'jev': {'command':'old-python', 'args':['old-server']}}})

    def test_existing_state_failure_rolls_back_and_keeps_credentials_private(self):
        self.config.write_text('[mcp_servers.jev.env]\nTYPESAFE_API_KEY="secret-test-key"\n')
        self.assertEqual(self.install()[0], 0)
        original = self.config.read_bytes()
        old_state = (self.home / runtime.STATE).read_bytes()
        self.assertNotIn(b'secret-test-key', old_state)
        atomic = runtime.core.atomic_write
        def failing(path, data):
            if path == self.home / runtime.STATE:
                raise OSError('injected')
            return atomic(path, data)
        with patch.object(runtime.core, 'atomic_write', side_effect=failing):
            code, output = self.call('install', '--auto-review')
        self.assertEqual(code, 1)
        self.assertNotIn('secret-test-key', output)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual((self.home / runtime.STATE).read_bytes(), old_state)

    def test_failed_state_write_rolls_back_config(self):
        self.config.write_text('model="original"\n')
        original = self.config.read_bytes()
        atomic = runtime.core.atomic_write
        def failing(path, data):
            if path == self.home / runtime.STATE:
                raise OSError('injected')
            return atomic(path, data)
        with patch.object(runtime.core, 'atomic_write', side_effect=failing):
            self.assertEqual(self.install()[0], 1)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.home / runtime.STATE).exists())


if __name__ == '__main__':
    unittest.main()
