import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("kit", Path(__file__).resolve().parents[1] / "install.py")
kit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kit)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent kit tests ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "codex home"

    def run_command(self, command="install", *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kit.main([command, "--codex-home", str(self.home), *extra])
        return code, out.getvalue(), err.getvalue()

    def put(self, name, text):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def snapshot(self):
        if not self.home.exists():
            return {}
        return {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}

    def test_clean_install_and_uninstall(self):
        self.assertEqual(self.run_command()[0], 0)
        config = tomllib.loads((self.home / "config.toml").read_text())
        self.assertEqual(config["model"], "gpt-6.1-sol")
        self.assertEqual(config["agents"]["max_concurrent_threads_per_session"], 3)
        for role in kit.ROLES:
            data = tomllib.loads((self.home / f"agents/{role}.toml").read_text())
            self.assertEqual(data["name"], role)
        self.assertEqual(self.run_command("status")[0], 0)
        self.assertEqual(self.run_command("uninstall")[0], 0)
        self.assertFalse((self.home / "config.toml").exists())
        self.assertFalse((self.home / "AGENTS.md").exists())
        self.assertFalse((self.home / kit.STATE).exists())

    def test_preserves_complex_config_and_restores_exact_original(self):
        config = '# retained comment\nmodel = "previous" # model note\n\n[mcp_servers.local]\ncommand = "python"\nargs = ["path with spaces/server.py"]\n\n[projects."C:/My Project"]\ntrust_level = "trusted"\n\n[agents]\ninterrupt_message = true\n'
        agents = "# My rules\r\nKeep these.\r\n"
        role = 'name = "worker"\ndescription = "My worker"\ndeveloper_instructions = "Existing"\n'
        self.put("config.toml", config)
        self.put("AGENTS.md", agents)
        self.put("agents/worker.toml", role)
        self.assertEqual(self.run_command()[0], 0)
        parsed = tomllib.loads((self.home / "config.toml").read_text())
        self.assertEqual(parsed["mcp_servers"]["local"]["args"], ["path with spaces/server.py"])
        self.assertIn("# retained comment", (self.home / "config.toml").read_text())
        self.assertTrue((self.home / "AGENTS.md").read_bytes().startswith(agents.encode()))
        self.assertEqual(self.run_command("uninstall")[0], 0)
        self.assertEqual((self.home / "config.toml").read_bytes(), config.encode())
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), agents.encode())
        self.assertEqual((self.home / "agents/worker.toml").read_text(), role)

    def test_install_is_idempotent(self):
        self.assertEqual(self.run_command()[0], 0)
        before = self.snapshot()
        self.assertEqual(self.run_command()[0], 0)
        self.assertEqual(before, self.snapshot())

    def test_dry_run_creates_nothing(self):
        self.assertEqual(self.run_command("install", "--dry-run")[0], 0)
        self.assertFalse(self.home.exists())

    def test_uninstall_dry_run_changes_nothing(self):
        self.run_command()
        before = self.snapshot()
        self.assertEqual(self.run_command("uninstall", "--dry-run")[0], 0)
        self.assertEqual(before, self.snapshot())

    def test_invalid_toml_never_writes_or_echoes_values(self):
        self.put("config.toml", 'password = "LOCAL-PRIVATE-VALUE\n')
        before = self.snapshot()
        code, out, err = self.run_command()
        self.assertEqual(code, 1)
        self.assertNotIn("LOCAL-PRIVATE-VALUE", out + err)
        self.assertEqual(before, self.snapshot())

    def test_preserves_unmanaged_edits_during_uninstall(self):
        self.run_command()
        config = self.home / "config.toml"
        with config.open("a") as stream:
            stream.write('\n[mcp_servers.added_later]\ncommand = "node"\n')
        agents = self.home / "AGENTS.md"
        with agents.open("a") as stream:
            stream.write("\nMy new rule.\n")
        self.assertEqual(self.run_command("uninstall")[0], 0)
        parsed = tomllib.loads(config.read_text())
        self.assertEqual(parsed["mcp_servers"]["added_later"]["command"], "node")
        self.assertNotIn("model", parsed)
        self.assertIn("My new rule.", agents.read_text())
        self.assertNotIn(kit.START, agents.read_text())

    def test_refuses_changed_managed_settings(self):
        self.run_command()
        config = self.home / "config.toml"
        config.write_text(config.read_text().replace('"gpt-6.1-sol"', '"local-model"'))
        before = self.snapshot()
        self.assertEqual(self.run_command("uninstall")[0], 1)
        self.assertEqual(self.run_command()[0], 1)
        self.assertEqual(before, self.snapshot())

    def test_reinstall_then_uninstall_preserves_unmanaged_edits(self):
        for original in (None, '# My initial config\nmodel = "old"\n'):
            with self.subTest(original=original):
                # Separate home and manifest for each first-install condition.
                self.home = Path(self.temp.name) / ("existing config" if original else "new config")
                if original is not None:
                    self.put("config.toml", original)
                self.assertEqual(self.run_command()[0], 0)
                path = self.home / "config.toml"
                with path.open("a") as stream:
                    stream.write('\n[mcp_servers.after_install]\ncommand = "node"\n')
                self.assertEqual(self.run_command()[0], 0)
                self.assertEqual(self.run_command()[0], 0)
                updated = kit.defaults()
                updated["model"] = "updated-template-model"
                with patch.object(kit, "defaults", return_value=updated):
                    self.assertEqual(self.run_command()[0], 0)
                    self.assertEqual(self.run_command()[0], 0)
                self.assertEqual(tomllib.loads(path.read_text())["model"], "updated-template-model")
                self.assertEqual(self.run_command("uninstall")[0], 0)
                config = tomllib.loads(path.read_text())
                self.assertEqual(config["mcp_servers"]["after_install"]["command"], "node")
                if original is None:
                    self.assertNotIn("model", config)
                else:
                    self.assertEqual(config["model"], "old")
                    self.assertIn("# My initial config", path.read_text())

    def test_refuses_changed_role(self):
        self.run_command()
        with (self.home / "agents/worker.toml").open("a") as stream:
            stream.write("\n# Local edit\n")
        before = self.snapshot()
        self.assertEqual(self.run_command("uninstall")[0], 1)
        self.assertEqual(before, self.snapshot())

    def test_refuses_changed_workflow(self):
        self.run_command()
        path = self.home / "AGENTS.md"
        path.write_text(path.read_text().replace("# Automatic agent workflow", "# Local custom workflow"))
        before = self.snapshot()
        self.assertEqual(self.run_command("uninstall")[0], 1)
        self.assertEqual(before, self.snapshot())

    def test_legacy_limits_and_role_declarations_stop_safely(self):
        for value in ('[agents]\nmax_threads = 8\n', '[agents.worker]\nconfig_file = "custom.toml"\n'):
            with self.subTest(value=value):
                self.put("config.toml", value)
                before = self.snapshot()
                self.assertEqual(self.run_command()[0], 1)
                self.assertEqual(before, self.snapshot())

    def test_override_instructions_stop_safely(self):
        self.put("AGENTS.override.md", "My override rules")
        before = self.snapshot()
        self.assertEqual(self.run_command()[0], 1)
        self.assertEqual(before, self.snapshot())

    def test_rejects_file_symlinks(self):
        self.home.mkdir()
        target = Path(self.temp.name) / "external.toml"
        target.write_text('model = "external"\n')
        try:
            (self.home / "config.toml").symlink_to(target)
        except OSError:
            self.skipTest("Symlinks unavailable for current Windows user")
        self.assertEqual(self.run_command()[0], 1)
        self.assertEqual(target.read_text(), 'model = "external"\n')

    def test_write_error_rolls_back_applied_files(self):
        self.put("config.toml", '# Existing config\nmodel = "old"\n')
        original = (self.home / "config.toml").read_bytes()
        real_write = kit.atomic_write
        def fail_one(path, data):
            if path == self.home / "agents/explorer.toml":
                raise OSError("Simulated write failure")
            return real_write(path, data)
        with patch.object(kit, "atomic_write", fail_one):
            self.assertEqual(self.run_command()[0], 1)
        self.assertEqual((self.home / "config.toml").read_bytes(), original)
        self.assertFalse((self.home / "agents/worker.toml").exists())
        self.assertFalse((self.home / "agents/architect.toml").exists())
        self.assertFalse((self.home / kit.STATE).exists())

    def test_manifest_cannot_supply_arbitrary_paths(self):
        self.run_command()
        path = self.home / kit.STATE
        manifest = json.loads(path.read_text())
        manifest["files"]["../external"] = manifest["files"].pop("agents/worker.toml")
        path.write_text(json.dumps(manifest))
        before = self.snapshot()
        self.assertEqual(self.run_command("uninstall")[0], 1)
        self.assertEqual(before, self.snapshot())

    def test_rejects_linked_managed_directories(self):
        self.home.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            (self.home / "agents").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable for current Windows user")
        self.assertEqual(self.run_command()[0], 1)
        self.assertEqual(list(outside.iterdir()), [])

    def interrupt_install(self):
        real_write = kit.atomic_write
        def interrupt(path, data):
            if path == self.home / "agents/explorer.toml":
                raise KeyboardInterrupt()
            return real_write(path, data)
        with patch.object(kit, "atomic_write", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_command()

    def test_interrupted_install_recovery(self):
        self.put("config.toml", '# Local config\nmodel = "old"\n')
        self.interrupt_install()
        self.assertTrue((self.home / kit.JOURNAL).exists())
        self.assertEqual(self.run_command()[0], 1)
        self.assertEqual(self.run_command("recover")[0], 0)
        self.assertEqual((self.home / "config.toml").read_text(), '# Local config\nmodel = "old"\n')
        self.assertFalse((self.home / "agents/architect.toml").exists())
        self.assertFalse((self.home / kit.JOURNAL).exists())
        self.assertEqual(self.run_command()[0], 0)

    def test_recovery_dry_run_and_local_edit_conflict(self):
        self.interrupt_install()
        before = self.snapshot()
        self.assertEqual(self.run_command("recover", "--dry-run")[0], 0)
        self.assertEqual(before, self.snapshot())
        self.put("agents/worker.toml", "# New local edit\n")
        before = self.snapshot()
        self.assertEqual(self.run_command("recover")[0], 1)
        self.assertEqual(before, self.snapshot())

    def test_tree_reads_models_from_templates(self):
        custom_root = Path(self.temp.name) / "custom bundle"
        shutil.copytree(kit.ROOT / "config", custom_root / "config")
        base = custom_root / "config/base.toml"
        base.write_text(base.read_text().replace('gpt-6.1-sol', 'custom-main', 1).replace('max_concurrent_threads_per_session = 3', 'max_concurrent_threads_per_session = 2'))
        worker = custom_root / "config/agents/worker.toml"
        worker.write_text(worker.read_text().replace('gpt-6.1-sol', 'custom-worker'))
        with patch.object(kit, "ROOT", custom_root):
            code, out, _ = self.run_command("tree")
        self.assertEqual(code, 0)
        self.assertIn("main: custom-main / high", out)
        self.assertIn("worker: custom-worker / medium", out)
        self.assertIn("2 concurrent subagents, excluding main (3 total including main)", out)
        self.assertIn("configured roles, not live sessions", out)

    def test_upgrade_from_previous_bundle_preserves_originals_and_unmanaged_settings(self):
        self.put("config.toml", 'model = "personal-original"\n\n[mcp_servers.local]\ncommand = "python"\n')
        previous_root = Path(self.temp.name) / "previous bundle"
        shutil.copytree(kit.ROOT / "config", previous_root / "config")
        base = previous_root / "config/base.toml"
        base.write_text(base.read_text().replace('gpt-6.1-sol', 'gpt-5.6-sol', 1))
        for role in ("worker", "explorer", "researcher"):
            path = previous_root / f"config/agents/{role}.toml"
            text = path.read_text().replace("gpt-6.1-sol", "gpt-5.6-sol")
            path.write_text(text.replace("gpt-6-luna", "gpt-5.6-luna"))
        with patch.object(kit, "ROOT", previous_root):
            self.assertEqual(self.run_command()[0], 0)
        path = self.home / "config.toml"
        self.assertEqual(tomllib.loads(path.read_text())["model"], "gpt-5.6-sol")
        self.assertEqual(tomllib.loads((self.home / "agents/explorer.toml").read_text())["model"], "gpt-5.6-luna")
        with path.open("a") as stream:
            stream.write('\n[projects."/work"]\ntrust_level = "trusted"\n')
        self.assertEqual(self.run_command()[0], 0)
        upgraded = tomllib.loads(path.read_text())
        self.assertEqual(upgraded["model"], "gpt-6.1-sol")
        self.assertEqual(tomllib.loads((self.home / "agents/worker.toml").read_text())["model"], "gpt-6.1-sol")
        self.assertEqual(tomllib.loads((self.home / "agents/explorer.toml").read_text())["model"], "gpt-6-luna")
        self.assertEqual(upgraded["mcp_servers"]["local"]["command"], "python")
        self.assertEqual(upgraded["projects"]["/work"]["trust_level"], "trusted")
        before = self.snapshot()
        self.assertEqual(self.run_command()[0], 0)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.run_command("uninstall")[0], 0)
        restored = tomllib.loads(path.read_text())
        self.assertEqual(restored["model"], "personal-original")
        self.assertEqual(restored["mcp_servers"]["local"]["command"], "python")
        self.assertEqual(restored["projects"]["/work"]["trust_level"], "trusted")


if __name__ == "__main__":
    unittest.main()
