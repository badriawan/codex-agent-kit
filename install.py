#!/usr/bin/env python3
"""Install portable Codex roles without replacing the user's configuration."""
from __future__ import annotations

import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required.")

import argparse
import base64
from collections.abc import MutableMapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib

try:
    import tomlkit
except ImportError:
    raise SystemExit("Install dependencies first: python -m pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent
STATE = ".agent-kit/manifest.json"
JOURNAL = ".agent-kit/transaction.json"
START = "<!-- codex-agent-kit:start -->"
END = "<!-- codex-agent-kit:end -->"
ROLES = ("architect", "worker", "explorer", "researcher")
MISSING = object()


class KitError(Exception):
    pass


def read_file(path: Path) -> bytes | None:
    if path.is_symlink():
        raise KitError(f"Refusing to replace symlink: {path}")
    return path.read_bytes() if path.exists() else None


def check_parent_links(home):
    for path in (home / "agents", home / ".agent-kit", home / ".agent-kit/backups"):
        if path.is_symlink():
            raise KitError(f"Refusing linked managed directory: {path}")


def text_file(path: Path) -> str:
    data = read_file(path)
    return data.decode("utf-8") if data is not None else ""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lookup(doc, keys):
    value = doc
    for key in keys:
        if not isinstance(value, MutableMapping) or key not in value:
            return MISSING
        value = value[key]
    return value.unwrap() if hasattr(value, "unwrap") else value


def assign(doc, keys, value=MISSING):
    node = doc
    for key in keys[:-1]:
        if key not in node:
            if value is MISSING:
                return
            node[key] = tomlkit.table()
        if not isinstance(node[key], MutableMapping):
            raise KitError(f"Expected TOML table: {'.'.join(keys[:-1])}")
        node = node[key]
    if value is MISSING:
        node.pop(keys[-1], None)
    else:
        node[keys[-1]] = value


def defaults():
    data = tomllib.loads((ROOT / "config/base.toml").read_text(encoding="utf-8"))
    return {
        "model": data["model"],
        "model_reasoning_effort": data["model_reasoning_effort"],
        "agents.enabled": data["agents"]["enabled"],
        "agents.max_concurrent_threads_per_session": data["agents"]["max_concurrent_threads_per_session"],
    }


def role_settings():
    result = {}
    for role in ROLES:
        data = tomllib.loads((ROOT / "config" / f"agents/{role}.toml").read_text(encoding="utf-8"))
        if data.get("name") != role:
            raise KitError(f"Invalid bundled agent: {role}")
        result[role] = {
            "model": data.get("model", "inherit"),
            "effort": data.get("model_reasoning_effort", "inherit"),
        }
    return result


def tree_text():
    base = defaults()
    roles = role_settings()
    return ("CODEX AGENT TREE (configured roles, not live sessions)\n"
            f"main: {base['model']} / {base['model_reasoning_effort']}\n"
            f"  |-- architect: {roles['architect']['model']} / {roles['architect']['effort']} (review on demand)\n"
            f"  |-- worker: {roles['worker']['model']} / {roles['worker']['effort']}\n"
            f"  |-- explorer: {roles['explorer']['model']} / {roles['explorer']['effort']}\n"
            f"  `-- researcher: {roles['researcher']['model']} / {roles['researcher']['effort']}\n"
            "Architect triggers: before a large plan; same error repeats; before a long task is declared complete\n"
            "Jev: optional subagent-role advice; MCP validates, main spawns fixed roles\n"
            "Role-only Jev and Auto-review: explicitly opt in with configure_runtime.py\n"
            f"Default: {base['agents.max_concurrent_threads_per_session']} concurrent subagents, excluding main "
            f"({base['agents.max_concurrent_threads_per_session'] + 1} total including main)\n"
            "Architect shares the subagent slots; stage work within the runtime limit\n"
            "Main integrates results and performs final verification")


def load_manifest(home):
    raw = text_file(home / STATE)
    if not raw:
        return None
    result = json.loads(raw)
    if result.get("format") != 1:
        raise KitError("Unsupported manifest version; use the installer version that created it.")
    expected_files = {f"agents/{r}.toml" for r in ROLES}
    if set(result.get("files", {})) != expected_files or set(result.get("keys", {})) != set(defaults()):
        raise KitError("Invalid manifest entries; refusing to modify files.")
    return result


def checked_document(home):
    raw = text_file(home / "config.toml")
    # Parse with both libraries before writing anything.
    tomllib.loads(raw)
    doc = tomlkit.parse(raw)
    if "agents" in doc and not isinstance(doc["agents"], MutableMapping):
        raise KitError("Existing 'agents' is not a TOML table.")
    # Avoid two competing sources for the same limit.
    if "max_threads" in doc.get("agents", {}):
        raise KitError("Legacy agents.max_threads exists. Rename it to max_concurrent_threads_per_session first.")
    # A custom name declared through the legacy schema can shadow standalone roles.
    for role in ROLES:
        if role in doc.get("agents", {}):
            raise KitError(f"Existing agents.{role} declaration conflicts with standalone role discovery.")
    return raw, doc


def verify_current(home, manifest, doc):
    for key, record in manifest["keys"].items():
        if lookup(doc, key.split(".")) != record["installed"]:
            raise KitError(f"Local edit detected at {key}; restore the installed value before updating/uninstalling.")
    for name, record in manifest["files"].items():
        current = read_file(home / name)
        if current is None or digest(current) != record["installed_sha256"]:
            raise KitError(f"Local edit detected in {name}; preserve it and restore the installed file first.")
    instructions = text_file(home / "AGENTS.md")
    block = manifest["workflow"]
    if instructions.count(block) != 1 or instructions.count(START) != 1 or instructions.count(END) != 1:
        raise KitError("Managed AGENTS.md block changed; restore that block before updating/uninstalling.")


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temp_name = tempfile.mkstemp(prefix=".agent-kit-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def commit(home, changes, dry_run):
    pending = {name: data for name, data in changes.items() if read_file(home / name) != data}
    for name in pending:
        print(f"{'Would update' if dry_run else 'Update'}: {name}")
    if not pending:
        print("Already up to date.")
        return
    if dry_run:
        return
    originals = {name: read_file(home / name) for name in pending}
    # Backups can contain local configuration; restrict their directory and files.
    backup = home / ".agent-kit/backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(home / ".agent-kit", 0o700)
    os.chmod(home / ".agent-kit/backups", 0o700)
    for name, data in originals.items():
        if data is not None:
            atomic_write(backup / name, data)
    journal = {
        name: {"original": None if data is None else base64.b64encode(data).decode("ascii"),
               "planned_sha256": None if pending[name] is None else digest(pending[name])}
        for name, data in originals.items()
    }
    atomic_write(home / JOURNAL, json.dumps(journal).encode("utf-8"))
    applied = []
    try:
        for name, data in pending.items():
            path = home / name
            # Recheck before mutation, including symlink substitution.
            if read_file(path) != originals[name]:
                raise KitError(f"File changed during installation: {name}")
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)
            applied.append(name)
    except Exception:
        for name in reversed(applied):
            data = originals[name]
            if data is None:
                (home / name).unlink(missing_ok=True)
            else:
                atomic_write(home / name, data)
        (home / JOURNAL).unlink()
        raise
    (home / JOURNAL).unlink()
    print(f"Local backup: {backup}")


def recover(home, dry_run=False):
    raw = text_file(home / JOURNAL)
    if not raw:
        print("No interrupted transaction to recover.")
        return
    journal = json.loads(raw)
    allowed = {"config.toml", "AGENTS.md", STATE} | {f"agents/{r}.toml" for r in ROLES}
    if not isinstance(journal, dict) or not journal or not set(journal) <= allowed:
        raise KitError("Invalid recovery journal; inspect local backups manually.")
    changes = {}
    for name, record in journal.items():
        original = record["original"]
        data = None if original is None else base64.b64decode(original, validate=True)
        current = read_file(home / name)
        current_hash = None if current is None else digest(current)
        if current != data and current_hash != record["planned_sha256"]:
            raise KitError(f"Local edit after interrupted transaction: {name}; save it before recovery.")
        changes[name] = data
    for name, data in changes.items():
        if read_file(home / name) != data:
            print(f"{'Would restore' if dry_run else 'Restore'}: {name}")
            if not dry_run:
                if data is None:
                    (home / name).unlink(missing_ok=True)
                else:
                    atomic_write(home / name, data)
    if not dry_run:
        (home / JOURNAL).unlink()
    print("Recovery planned." if dry_run else "Interrupted transaction rolled back.")


def install(home, dry_run=False):
    original_config, doc = checked_document(home)
    manifest = load_manifest(home)
    instructions = text_file(home / "AGENTS.md")
    if manifest:
        verify_current(home, manifest, doc)
        # Once unrelated edits have occurred, later reinstallations must never
        # make the first-install full-file snapshot eligible for restoration.
        if original_config != manifest["config_installed"]:
            manifest["config_restore_exact"] = False
    elif START in instructions or END in instructions:
        raise KitError("An unmanaged agent-kit workflow marker already exists.")
    if (home / "AGENTS.override.md").exists():
        raise KitError("AGENTS.override.md takes precedence. Merge/remove it before installing global workflow.")
    manifest = manifest or {
        "format": 1, "keys": {}, "files": {},
        "config_existed": (home / "config.toml").exists(),
        "config_original": original_config,
        "config_restore_exact": True,
        "instructions_existed": (home / "AGENTS.md").exists(),
        "instructions_original": instructions,
    }
    for key, desired in defaults().items():
        old = lookup(doc, key.split("."))
        if key not in manifest["keys"]:
            manifest["keys"][key] = {"existed": old is not MISSING, "original": None if old is MISSING else old}
        if old != desired:
            print(f"Managed setting: {key}")
            assign(doc, key.split("."), desired)
        manifest["keys"][key]["installed"] = desired
    changes = {}
    for role in ROLES:
        name = f"agents/{role}.toml"
        data = (ROOT / "config" / name).read_bytes()
        parsed = tomllib.loads(data.decode("utf-8"))
        if parsed.get("name") != role:
            raise KitError(f"Invalid bundled agent: {role}")
        if name not in manifest["files"]:
            previous = read_file(home / name)
            manifest["files"][name] = {"original": None if previous is None else previous.decode("utf-8")}
        manifest["files"][name]["installed_sha256"] = digest(data)
        changes[name] = data
    policy = (ROOT / "config/AGENTS.md").read_text(encoding="utf-8").rstrip()
    block = f"{START}\n{policy}\n{END}\n"
    if "workflow" in manifest:
        new_block = ("\n\n" if manifest["workflow"].startswith("\n\n") else "") + block
        instructions = instructions.replace(manifest["workflow"], new_block, 1)
    else:
        new_block = ("\n\n" if instructions else "") + block
        instructions += new_block
    manifest["workflow"] = new_block
    rendered = tomlkit.dumps(doc)
    tomllib.loads(rendered)
    manifest["config_installed"] = rendered
    changes["config.toml"] = rendered.encode("utf-8")
    changes["AGENTS.md"] = instructions.encode("utf-8")
    changes[STATE] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    commit(home, changes, dry_run)
    if not dry_run:
        print("Installed. Start a new Codex session. Login and model access remain device-specific.")


def uninstall(home, dry_run=False):
    manifest = load_manifest(home)
    if not manifest:
        print("Agent kit is not installed.")
        return
    raw, doc = checked_document(home)
    verify_current(home, manifest, doc)
    # Exact restoration when untouched, otherwise restore only managed keys.
    if manifest.get("config_restore_exact", False) and raw == manifest["config_installed"]:
        config = manifest["config_original"].encode("utf-8") if manifest["config_existed"] else None
    else:
        for key, record in manifest["keys"].items():
            assign(doc, key.split("."), record["original"] if record["existed"] else MISSING)
        config = tomlkit.dumps(doc).encode("utf-8")
        tomllib.loads(config.decode("utf-8"))
    instructions = text_file(home / "AGENTS.md").replace(manifest["workflow"], "", 1)
    changes = {"config.toml": config, STATE: None}
    changes["AGENTS.md"] = instructions.encode("utf-8") if instructions or manifest["instructions_existed"] else None
    for name, record in manifest["files"].items():
        previous = record["original"]
        changes[name] = None if previous is None else previous.encode("utf-8")
    commit(home, changes, dry_run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "uninstall", "recover", "status", "tree"), nargs="?", default="install")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    parser.add_argument("--dry-run", action="store_true", help="Show planned paths/keys without writing anything")
    args = parser.parse_args(argv)
    home = args.codex_home.expanduser().absolute()
    try:
        check_parent_links(home)
        if args.command != "recover" and read_file(home / JOURNAL) is not None:
            raise KitError("Interrupted transaction detected. Run: python install.py recover (same --codex-home).")
        if args.command == "install":
            install(home, args.dry_run)
        elif args.command == "uninstall":
            uninstall(home, args.dry_run)
        elif args.command == "recover":
            recover(home, args.dry_run)
        elif args.command == "status":
            state = load_manifest(home)
            if not state:
                print("Agent kit is not installed.")
            else:
                _, doc = checked_document(home)
                verify_current(home, state, doc)
                print("Agent kit installed; managed settings and files match. This is not live agent status.")
        else:
            print(tree_text())
    except (KitError, OSError, ValueError, TypeError, tomlkit.exceptions.ParseError) as error:
        # Parser errors can contain existing configuration values. Do not echo them.
        message = str(error) if isinstance(error, KitError) else f"{type(error).__name__}: cannot safely process files; no configuration values are printed."
        print(f"Error: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
