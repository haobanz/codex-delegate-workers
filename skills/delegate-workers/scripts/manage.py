#!/usr/bin/env python3
"""Install, update, and configure Delegate Workers without changing Codex settings."""

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

import workers


PROJECT = "delegate-workers"
REPOSITORY = "https://github.com/haobanz/codex-delegate-workers.git"
RECEIPT = ".delegate-workers-install.json"
REQUIRED = {"SKILL.md", "workers.json", "VERSION", "scripts/workers.py", "scripts/manage.py"}


class ManagementError(ValueError):
    pass


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".delegate-workers-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def inventory(directory):
    result = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ManagementError(f"Symbolic links are not managed: {path}")
        relative = path.relative_to(directory)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_file() and relative.as_posix() not in {RECEIPT, "workers.json", "workers.json.bak"}:
            result[relative.as_posix()] = file_hash(path)
    return result


def load_receipt(directory):
    if directory.is_symlink():
        raise ManagementError(f"Refusing to replace a symlinked skill: {directory}")
    if not directory.exists():
        return None
    receipt_path = directory / RECEIPT
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise ManagementError(f"Existing directory is not a managed installation: {directory}")
    value = workers.read_json(receipt_path)
    if (not isinstance(value, dict) or value.get("project") != PROJECT
            or value.get("schema_version") != 1 or not isinstance(value.get("files"), dict)):
        raise ManagementError(f"Invalid installation receipt: {receipt_path}")
    if any(not isinstance(value.get(key), str) or not value[key] for key in ("version", "revision")):
        raise ManagementError(f"Invalid version or revision in receipt: {receipt_path}")
    for relative, digest in value["files"].items():
        path = PurePosixPath(relative)
        if (not relative or path.is_absolute() or ".." in path.parts or "\\" in relative
                or relative in {RECEIPT, "workers.json"} or not isinstance(digest, str)):
            raise ManagementError(f"Invalid managed path in receipt: {relative}")
    return value


class Installation:
    def __init__(self, codex_home):
        self.home = Path(codex_home).expanduser().resolve()
        self.skill = self.home / "skills" / PROJECT
        self.launcher = self.home / "bin" / PROJECT
        self.backups = self.home / "delegate-workers-backups"

    @contextlib.contextmanager
    def locked(self):
        self.home.mkdir(parents=True, exist_ok=True)
        with (self.home / ".delegate-workers.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ManagementError("Another Delegate Workers operation is running") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def launcher_content(self):
        command = " ".join(shlex.quote(part) for part in (
            "python3", str(self.skill / "scripts/manage.py"), "--codex-home", str(self.home)))
        return (f'#!/bin/sh\n# Managed by Delegate Workers\nexec {command} "$@"\n').encode()

    def check_launcher(self):
        if self.launcher.is_symlink():
            raise ManagementError(f"Launcher is a symlink: {self.launcher}")
        if self.launcher.exists() and self.launcher.read_bytes() != self.launcher_content():
            raise ManagementError(f"Launcher contains unmanaged changes: {self.launcher}")

    def config(self):
        if load_receipt(self.skill) is None:
            raise ManagementError("Install Delegate Workers first")
        return workers.validate_config(workers.read_json(self.skill / "workers.json"))

    def changed_files(self, receipt):
        actual = inventory(self.skill)
        return [name for name, digest in receipt["files"].items() if actual.get(name) != digest]

    def backup_path(self):
        self.backups.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
        return self.backups / name

    def apply(self, source, revision="local", source_files=None):
        """Stage and validate the candidate before swapping the installed directory."""
        source = Path(source).resolve()
        present = inventory(source)
        files = present if source_files is None else source_files
        for relative in REQUIRED:
            if not (source / relative).is_file():
                raise ManagementError(f"Release is missing {relative}")
        if not (REQUIRED - {"workers.json"}) <= files.keys():
            raise ManagementError("Release manifest is missing required files")
        for relative, digest in files.items():
            if present.get(relative) != digest:
                raise ManagementError(f"Release file has changed: {relative}")
        version = (source / "VERSION").read_text(encoding="utf-8").strip()
        if not version or len(version) > 64:
            raise ManagementError("Invalid release version")
        old = load_receipt(self.skill)
        self.check_launcher()
        if old:
            changes = self.changed_files(old)
            if changes:
                raise ManagementError("Installed code has local changes; preserve them before updating: "
                                      + ", ".join(changes))
            for relative in files.keys() - old["files"].keys():
                if (self.skill / relative).exists():
                    raise ManagementError(f"New release conflicts with an unmanaged file: {relative}")
        self.skill.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".delegate-workers-stage-", dir=self.skill.parent))
        backup = None
        old_launcher = self.launcher.read_bytes() if self.launcher.exists() else None
        try:
            if old:
                shutil.copytree(self.skill, stage, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
                for relative in old["files"].keys() - files.keys():
                    (stage / relative).unlink()
            for relative in files:
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, destination)
            if not old:
                shutil.copyfile(source / "workers.json", stage / "workers.json")
            validation = subprocess.run(
                [sys.executable, str(stage / "scripts/workers.py"), "validate"],
                capture_output=True, text=True, check=False, timeout=30)
            if validation.returncode:
                raise ManagementError("Candidate rejected the worker settings: " + validation.stderr.strip())
            receipt = {"schema_version": 1, "project": PROJECT, "repository": REPOSITORY,
                       "version": version, "revision": revision, "files": files}
            atomic_write(stage / RECEIPT, json_bytes(receipt))
            if old and old == receipt:
                atomic_write(self.launcher, self.launcher_content(), 0o755)
                return {"result": "unchanged", "version": version, "revision": revision}
            atomic_write(self.launcher, self.launcher_content(), 0o755)
            if old:
                backup = self.backup_path()
                self.skill.rename(backup)
            try:
                stage.rename(self.skill)
            except BaseException:
                if backup is not None:
                    backup.rename(self.skill)
                raise
            return {"result": "updated" if old else "installed", "version": version,
                    "revision": revision, "backup": str(backup) if backup else None}
        except BaseException:
            if old_launcher is None:
                if self.launcher.is_file() and self.launcher.read_bytes() == self.launcher_content():
                    self.launcher.unlink()
            else:
                atomic_write(self.launcher, old_launcher, 0o755)
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def install(self, source=None, require_existing=False):
        with self.locked():
            if require_existing and load_receipt(self.skill) is None:
                raise ManagementError("Install Delegate Workers before updating")
            if source is not None:
                root = Path(source).expanduser().resolve()
                return self.apply(root / "skills" / PROJECT, revision_of(root))
            with tempfile.TemporaryDirectory(prefix="delegate-workers-download-") as temporary:
                root = Path(temporary) / "repository"
                run_git(["clone", "--quiet", "--depth", "1", "--branch", "main", "--",
                         REPOSITORY, str(root)])
                return self.apply(root / "skills" / PROJECT, revision_of(root))

    def save_config(self, config):
        workers.validate_config(config)
        previous = self.skill / "workers.json"
        atomic_write(self.skill / "workers.json.bak", previous.read_bytes())
        atomic_write(previous, json_bytes(config))

    def configure(self, profile, model=None, effort=None, fallback=None, default=False):
        with self.locked():
            config = copy.deepcopy(self.config())
            current = config["profiles"].get(profile, {})
            candidate = dict(current)
            if model is not None:
                if effort is None:
                    raise ManagementError("Changing a model requires an explicit --effort")
                candidate["model"] = model
            if effort is not None:
                candidate["reasoning_effort"] = effort
            if fallback is not None:
                candidate["fallback"] = None if fallback == "none" else fallback
            config["profiles"][profile] = candidate
            if default:
                config["default_profile"] = profile
            self.save_config(config)
            return config

    def set_limits(self, parallel=None, attempts=None):
        with self.locked():
            config = copy.deepcopy(self.config())
            if parallel is not None:
                config["max_parallel_workers"] = parallel
            if attempts is not None:
                config["max_attempts_per_task"] = attempts
            self.save_config(config)
            return config

    def status(self):
        receipt = load_receipt(self.skill)
        if receipt is None:
            return {"installed": False, "skill": str(self.skill)}
        return {"installed": True, "version": receipt["version"], "revision": receipt["revision"],
                "skill": str(self.skill), "launcher": str(self.launcher),
                "local_code_changes": self.changed_files(receipt), "config": self.config(),
                "main_session": "unchanged"}

    def rollback(self):
        with self.locked():
            backups = sorted(self.backups.glob("*"), reverse=True)
            previous = next((path for path in backups if path.is_dir() and (path / RECEIPT).is_file()), None)
            if previous is None:
                raise ManagementError("No installation backup is available")
            receipt = load_receipt(previous)
            return self.apply(previous, receipt["revision"], receipt["files"])

    def uninstall(self):
        with self.locked():
            if load_receipt(self.skill) is None:
                return {"result": "not_installed"}
            self.check_launcher()
            backup = self.backup_path()
            self.skill.rename(backup)
            try:
                if self.launcher.exists():
                    self.launcher.unlink()
            except BaseException:
                backup.rename(self.skill)
                raise
            return {"result": "uninstalled", "backup": str(backup)}


def run_git(arguments):
    try:
        result = subprocess.run(["git", *arguments], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagementError(f"Git failed: {exc}") from exc
    if result.returncode:
        raise ManagementError(result.stderr.strip() or "Git operation failed")
    return result.stdout.strip()


def revision_of(root):
    if not (root / ".git").exists():
        return "local"
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "HEAD"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode == 1:
        return "local"
    if result.returncode:
        raise ManagementError(result.stderr.strip() or "Cannot inspect source revision")
    return result.stdout.strip()


def print_result(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def prompt(stream, label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    print(f"{label}{suffix}: ", end="", flush=True)
    line = stream.readline()
    if not line:
        raise EOFError
    return line.strip() or default or ""


def menu(installation, source=None, stream=None):
    if stream is None:
        try:
            with open("/dev/tty", "r", encoding="utf-8") as terminal:
                return menu(installation, source, terminal)
        except OSError as exc:
            raise ManagementError("The menu needs a terminal; use install, update, configure, or status in scripts") from exc
    while True:
        print("\nDelegate Workers\n"
              "1. Install / update\n2. Worker model settings\n3. Concurrency / attempts\n"
              "4. Status / validate\n5. Roll back code (keep worker settings)\n6. Uninstall\n0. Exit")
        try:
            choice = prompt(stream, "Select", "0")
            if choice == "0":
                return
            if choice == "1":
                print_result(installation.install(source))
                print("Open a new Codex task to load changed skill instructions.")
                print(f"Reopen the menu with: {shlex.quote(str(installation.launcher))} menu")
                return
            elif choice == "2":
                config = installation.config()
                for name, profile in config["profiles"].items():
                    print(f"  {name}: {profile['model']} / {profile['reasoning_effort']}")
                name = prompt(stream, "Profile name (existing or new)", config["default_profile"])
                current = config["profiles"].get(name, {})
                model = prompt(stream, "Worker model ID", current.get("model"))
                effort = prompt(stream, "Reasoning (low/medium/high/xhigh/max; depends on the model)",
                                current.get("reasoning_effort", "medium"))
                fallback = prompt(stream, "Fallback profile or none", current.get("fallback") or "none")
                default = prompt(stream, "Set as the default worker? y/N", "n").lower() == "y"
                installation.configure(name, model, effort, fallback, default)
                print("Worker settings saved. Main session settings are unchanged.")
            elif choice == "3":
                config = installation.config()
                parallel = int(prompt(stream, "Maximum parallel workers", config["max_parallel_workers"]))
                attempts = int(prompt(stream, "Maximum attempts per task", config["max_attempts_per_task"]))
                installation.set_limits(parallel, attempts)
                print("Limits saved.")
            elif choice == "4":
                print_result(installation.status())
            elif choice == "5":
                print_result(installation.rollback())
                print("Open a new Codex task to reload skill instructions.")
                return
            elif choice == "6":
                if prompt(stream, "Type uninstall to remove this skill") == "uninstall":
                    print_result(installation.uninstall())
                    return
            else:
                print("Choose 0 through 6.")
        except EOFError:
            return
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            print(f"Error: {exc}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    parser.add_argument("--source", type=Path, help="Use a local repository checkout instead of downloading")
    commands = parser.add_subparsers(dest="command")
    for command in ("install", "update", "menu", "status", "rollback"):
        commands.add_parser(command)
    configure = commands.add_parser("configure")
    configure.add_argument("--profile", required=True)
    configure.add_argument("--model")
    configure.add_argument("--effort")
    configure.add_argument("--fallback", help="An existing worker profile, or none")
    configure.add_argument("--default", action="store_true")
    limits = commands.add_parser("limits")
    limits.add_argument("--parallel", type=int)
    limits.add_argument("--attempts", type=int)
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    installation = Installation(args.codex_home)
    try:
        if args.command is None or args.command == "menu":
            menu(installation, args.source)
            return 0
        if args.command in {"install", "update"}:
            output = installation.install(args.source, require_existing=args.command == "update")
        elif args.command == "configure":
            output = installation.configure(args.profile, args.model, args.effort, args.fallback, args.default)
        elif args.command == "limits":
            output = installation.set_limits(args.parallel, args.attempts)
        elif args.command == "rollback":
            output = installation.rollback()
        elif args.command == "uninstall":
            if not args.yes:
                raise ManagementError("Use uninstall --yes or choose Uninstall in the menu")
            output = installation.uninstall()
        else:
            output = installation.status()
        print_result(output)
        if args.command in {"install", "update", "rollback"}:
            print(f"\nMenu: {shlex.quote(str(installation.launcher))} menu")
            print("Open a new Codex task, then invoke $delegate-workers.")
        return 0
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
