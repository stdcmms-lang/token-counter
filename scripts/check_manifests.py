#!/usr/bin/env python3
"""Offline packaging, contract and isolated installed-copy checks for both plugins.

check(repo) returns errors. The CLI also prints missing surfaces as "not present".
--existing-only permits an unfinished Claude plugin, checking every file that is
already present. Full mode requires both marketplaces and the complete Claude product.
No installed-plugin directories or account/corpus files are inspected.
"""
import argparse
import ast
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import List


REPO = Path(__file__).resolve().parent.parent
CODEX = "plugins/token-counter"
CLAUDE = "plugins/token-counter-claude"
SHARED = ("render.py", "latency.py", "index.py", "images.py")
SCRIPT_DIR = "skills/token-report/scripts"
PINS = {"typescript": "5.9.3", "zod": "4.6.5"}
HASHES = {
    "scripts/fixtures/codex_2aebba1/render.py": "c4dc9e6592e4cc0948218b0313fde45d60ebe738a38c8f0af20df412b2b8f303",
    "scripts/server_contract/schema.ts": "61152fa71832a2565174e390041e0db8c2932b6c324d57fc799bd55288c69882",
    "scripts/server_contract/validate.ts": "8cf76f0d71c5371633f9d47419ff0073daa5fc7b1dd09da114a35c553f5cded5",
}
RESERVED_PREFIXES = ("claude-", "anthropic-", "anthropics-", "cc-plugin-")
RESERVED_NAMES = {"claude", "anthropic", "claude-code"}
RESERVED_MARKETPLACES = {"claude-plugins-official", "anthropic-marketplace", "github", "npm"}


def _within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read(repo, relative):
    target = (repo / relative).resolve()
    if not _within(target, repo):
        raise ValueError("file resolves outside the repository: " + str(relative))
    return target.read_bytes()


def _json(repo, relative):
    data = json.loads(_read(repo, relative).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(str(relative) + ": JSON root must be an object")
    return data


def _assignment(repo, relative, name):
    parsed = ast.parse(_read(repo, relative).decode("utf-8"), filename=str(relative))
    for node in parsed.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(str(relative) + ": " + name + " assignment not found")


def _name(value):
    return (isinstance(value, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is not None
            and value not in RESERVED_NAMES and not value.startswith(RESERVED_PREFIXES))


def _local_path(repo, base, value, *, allow_missing=False):
    if not isinstance(value, str) or not value.startswith("./") or "\\" in value:
        raise ValueError("local component/source path must start with ./")
    parts = value[2:].split("/")
    if not parts or any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ValueError("local component/source path contains an unsafe segment")
    target = (base / value[2:]).resolve()
    if not _within(target, base) or not _within(target, repo):
        raise ValueError("component/source resolves outside its permitted root")
    if not target.exists() and not allow_missing:
        raise ValueError("component/source does not exist: " + value)
    return target


def _tree_within(root):
    for current, directories, files in os.walk(str(root), followlinks=False):
        for name in directories + files:
            path = Path(current) / name
            if not _within(path.resolve(), root):
                raise ValueError("installed component resolves outside its plugin")


def _import_script(repo, relative, name):
    path = (repo / relative).resolve()
    _read(repo, relative)  # Check containment before importlib opens it.
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frontmatter(repo, relative, skill, *, allow_missing_script=False):
    text = _read(repo, relative).decode("utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if match is None:
        raise ValueError(skill + ": missing skill frontmatter")
    fields = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            raise ValueError(skill + ": unsupported frontmatter line")
        key, value = line.split(":", 1)
        if key in fields:
            raise ValueError(skill + ": duplicate frontmatter key")
        fields[key] = value.strip().strip('"').strip("'")
    if fields.get("name") != skill or fields.get("user-invocable") != "true":
        raise ValueError(skill + ": wrong skill name or user invocation setting")
    if not fields.get("description") or not fields.get("argument-hint"):
        raise ValueError(skill + ": description and argument-hint are required")
    if fields.get("disable-model-invocation", "false") != "false":
        raise ValueError(skill + ": model invocation must be enabled")
    script = {"token-report": "report.py", "token-share": "share.py", "cc-api": "cc_api.py"}[skill]
    path = '"${CLAUDE_SKILL_DIR}/scripts/' + script + '"'
    if "python " + path not in text or "python3 " + path not in text:
        raise ValueError(skill + ": missing quoted Windows/POSIX interpreter variants")
    if "${CLAUDE_PLUGIN_ROOT}" in text or "$CLAUDE_SKILL_DIR/" in text:
        raise ValueError(skill + ": paths must use the documented skill-dir substitution")
    commands = re.findall(r'^python3? "([^"\n]+)".*$', text, re.M)
    for command_path in commands:
        if command_path != "${CLAUDE_SKILL_DIR}/scripts/" + script:
            raise ValueError(skill + ": command outside its skill scripts")
        _local_path(repo, (repo / relative).parent, "./scripts/" + script,
                    allow_missing=allow_missing_script)
    if skill == "token-report" and fields.get("disable-model-invocation") != "false":
        raise ValueError(skill + ": explicit model invocation setting missing")
    if skill in ("token-share", "cc-api") and "disable-model-invocation" in fields:
        raise ValueError(skill + ": use default model invocation, without a disabling field")
    return text


def _codex_cc_api_frontmatter(repo, *, allow_missing_script=False):
    relative = CODEX + "/skills/cc-api/SKILL.md"
    text = _read(repo, relative).decode("utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if match is None:
        raise ValueError("Codex cc-api: missing frontmatter")
    fields = {}
    for line in match.group(1).splitlines():
        key, value = line.split(":", 1)
        if key in fields:
            raise ValueError("Codex cc-api: duplicate frontmatter key")
        fields[key] = value.strip()
    if fields.get("name") != "cc-api" or not fields.get("description"):
        raise ValueError("Codex cc-api: name/description required")
    for interpreter in ("python", "python3"):
        if interpreter + ' "<skill-dir>/scripts/cc_api.py" -- "TASK"' not in text:
            raise ValueError("Codex cc-api: missing quoted absolute-path command")
    _local_path(repo, repo / CODEX / "skills/cc-api", "./scripts/cc_api.py",
                allow_missing=allow_missing_script)
    return text


def _installed_cc_api_smoke(repo) -> None:
    cases = _import_script(repo, "scripts/fixtures/cc_api_cases.py", "manifest_cc_api_cases")
    wrapper = ("import pathlib, runpy, socket, sys, urllib.request\n"
               "def deny(*a, **k): raise AssertionError('cc-api smoke attempted network')\n"
               "socket.socket = socket.create_connection = socket.getaddrinfo = deny\n"
               "urllib.request.urlopen = deny\n"
               "checkout = pathlib.Path(sys.argv.pop(1)).resolve()\n"
               "installed = pathlib.Path(sys.argv.pop(1)).resolve()\n"
               "def within(path, base):\n"
               "    try: path.resolve().relative_to(base); return True\n"
               "    except ValueError: return False\n"
               "assert not any(within(pathlib.Path(p), checkout) for p in sys.path), 'checkout on sys.path'\n"
               "sys.argv = sys.argv[1:]\n"
               "assert within(pathlib.Path(sys.argv[0]), installed), 'helper outside installed plugin'\n"
               "runpy.run_path(sys.argv[0], run_name='__main__')\n")
    # One plugin per temporary installation: neither a checkout nor a sibling is
    # available on the isolated helper's import path.
    for plugin in (CODEX, CLAUDE):
        source = repo / plugin
        _tree_within(source.resolve())
        with tempfile.TemporaryDirectory(prefix="cc api installed smoke ") as directory:
            root = Path(directory).resolve()
            installed = root / "independently installed plugin"
            shutil.copytree(str(source), str(installed), symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            fake_bin = cases.install_fake_claude(root)
            env = cases.base_env(root, fake_bin)
            cwd = root / "caller task directory"
            cwd.mkdir()
            text = (installed / "skills/cc-api/SKILL.md").read_text(encoding="utf-8")
            skill_dir = (installed / "skills/cc-api").as_posix()
            token = "${CLAUDE_SKILL_DIR}" if plugin == CLAUDE else "<skill-dir>"
            for interpreter in ("python", "python3"):
                match = re.search(r'^' + interpreter + r' "([^"\n]+)" -- "TASK"$', text, re.M)
                assert match is not None, "missing cc-api interpreter command"
                args = shlex.split(match.group(0).replace(token, skill_dir))[1:]
                task = 'Installed multiline task\nwith & | " % and Unicode \u4f60\u597d'
                args = [task if arg == "TASK" else arg for arg in args]
                result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", wrapper,
                                         str(repo), str(installed)] + args,
                                        input=cases.PARENT_STDIN, cwd=str(cwd), env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                assert result.returncode == 0, "installed cc-api helper failed"
                record = cases.read_record(Path(env["CC_API_RECORD"]))
                assert record["argv"] == ["-p", "--output-format", "json"], "installed argv differs"
                assert record["stdin_hex"] == task.encode("utf-8").hex(), "installed task stdin differs"
                assert record["cwd"] == str(cwd), "installed caller cwd differs"
                assert record["env"]["ANTHROPIC_API_KEY"] == cases.ENV_KEY, "installed key differs"
                assert "CC_API_KEY" not in record["env"], "installed source key leaked"
                assert "CC_API_KEY_FILE" not in record["env"], "installed file source leaked"
                assert record["env"]["CLAUDE_CODE_GIT_BASH_PATH"] == cases.KEPT["CLAUDE_CODE_GIT_BASH_PATH"], "installed configuration lost"
                expected = (cases.RESULT + "\ncc-api: total_cost_usd=0.2705; turns=3; model=" + cases.MODEL +
                            "\ncc-api: Anthropic decides whether API credits apply; check Console Billing: "
                            "https://platform.claude.com/settings/billing\n")
                assert result.stdout.decode("utf-8").replace("\r\n", "\n") == expected, "installed result differs"
                assert result.stderr == b"", "installed helper stderr differs"


def _installed_smoke(repo):
    plugin = (repo / CLAUDE).resolve()
    _tree_within(plugin)
    cases = _import_script(repo, "scripts/fixtures/claude_cases.py", "manifest_claude_cases")
    with tempfile.TemporaryDirectory(prefix="claude installed smoke ") as directory:
        root = Path(directory).resolve()
        installed = root / "installed plugin"
        shutil.copytree(str(plugin), str(installed), symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        sessions = root / "config/projects"
        cases.write_corpus(sessions, cases.baseline_B())
        # -I -S and a temp cwd remove the repository/sibling from the import path.
        # Block network inside each child as well as in the outer test harness.
        wrapper = ("import pathlib, runpy, socket, sys, urllib.request\n"
                   "def deny(*a, **k): raise AssertionError('installed smoke attempted network')\n"
                   "socket.socket = socket.create_connection = socket.getaddrinfo = deny\n"
                   "urllib.request.urlopen = deny\n"
                   "checkout = pathlib.Path(sys.argv.pop(1)).resolve()\n"
                   "installed = pathlib.Path(sys.argv.pop(1)).resolve()\n"
                   "def within(path, base):\n"
                   "    try: path.resolve().relative_to(base); return True\n"
                   "    except ValueError: return False\n"
                   "def imports_stay_installed():\n"
                   "    assert not any(within(pathlib.Path(p), checkout) for p in sys.path), 'checkout on sys.path'\n"
                   "    for name, module in list(sys.modules.items()):\n"
                   "        if name == 'report' or name == 'tokencounter' or name.startswith('tokencounter.'):\n"
                   "            assert within(pathlib.Path(module.__file__), installed), 'import outside installed plugin'\n"
                   "imports_stay_installed()\n"
                   "sys.argv = sys.argv[1:]\n"
                   "try: runpy.run_path(sys.argv[0], run_name='__main__')\n"
                   "except SystemExit as exc:\n"
                   "    if exc.code not in (None, 0): raise\n"
                   "imports_stay_installed()\n")
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(root / "config"), TOKEN_COUNTER_NO_INSTALL="1")
        for skill in ("token-report", "token-share"):
            text = (installed / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
            skill_dir = (installed / "skills" / skill).as_posix()
            for interpreter in ("python", "python3"):
                match = re.search(r'^' + interpreter + r' "\$\{CLAUDE_SKILL_DIR\}[^"\n]+".*$', text, re.M)
                assert match is not None, "missing skill interpreter command"
                args = shlex.split(match.group(0).replace("${CLAUDE_SKILL_DIR}", skill_dir))
                args = ["synthetic-smoke" if arg == "HANDLE" else arg for arg in args[1:]]
                artifact = root / (interpreter + ("-model.json" if skill == "token-report" else "-payload.json"))
                args += ["--sessions-root", str(sessions), "--no-account", "--procs", "1", "--quiet"]
                args += ["--json", str(artifact)] if skill == "token-report" else ["--out", str(artifact)]
                # Test both documented command variants using this CI cell's Python,
                # including skill-text substitution and paths containing spaces.
                command = [sys.executable, "-I", "-S", "-B", "-c", wrapper, str(repo), str(installed)] + args
                result = subprocess.run(command, cwd=str(root), env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=60)
                if result.returncode != 0:
                    raise ValueError("isolated installed-copy " + skill + "/" + interpreter +
                                     " failed (exit %d)" % result.returncode)
                value = json.loads(artifact.read_bytes())
                counts = value['totals'] if skill == 'token-report' else {
                    key: sum(d[key] for d in value['days']) for key in ('responses', 'input', 'cached', 'output', 'reasoning')}
                assert tuple(counts[k] for k in ('responses', 'input', 'cached', 'output', 'reasoning')) == (2, 255, 90, 18, 7), "installed B counts differ"
                if skill == 'token-share':
                    assert b'Dry run: nothing was sent.' in result.stdout, "share was not a dry run"
        assert not list(root.rglob('claude-share.json')), "installed dry run created a token"
        previews = list((root / 'config').rglob('report-shared.html'))
        assert len(previews) == 1 and previews[0].read_bytes().lower().startswith(b'<!doctype html>'), "public preview missing"
        if (root / "token-counter").exists():
            raise ValueError("installed-copy smoke unexpectedly has a Codex sibling")


def _run(repo, existing_only=False):
    repo = Path(repo).resolve()
    errors, notes = [], []

    def attempt(label, fn):
        try:
            return fn()
        except (OSError, ValueError, TypeError, KeyError, AssertionError, SyntaxError,
                ImportError, AttributeError, subprocess.SubprocessError) as exc:
            errors.append(label + ": " + str(exc))
            return None

    def optional(relative, required=False):
        target = repo / relative
        if target.exists():
            if not _within(target.resolve(), repo):
                errors.append(relative + ": resolves outside the repository")
                return False
            return True
        if required and not existing_only:
            errors.append("missing: " + relative)
        else:
            notes.append("not present: " + relative)
        return False

    marketplaces = {}
    for relative in (".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"):
        if optional(relative, required=True):
            data = attempt(relative, lambda relative=relative: _json(repo, relative))
            if data is not None:
                marketplaces[relative] = data
    codex = attempt("Codex manifest", lambda: _json(repo, CODEX + "/.codex-plugin/plugin.json"))
    client = attempt("Codex CLIENT", lambda: _assignment(repo, CODEX + "/skills/token-share/scripts/share.py", "CLIENT"))
    if codex is not None and client is not None:
        if (codex.get("name") != "token-counter" or codex.get("version") != "1.11.0" or
                client != {"name": "token-counter", "version": "1.11.0"}):
            errors.append("Codex manifest and share CLIENT must independently agree on token-counter 1.11.0")
        else:
            notes.append("Codex manifest and share CLIENT: 1.11.0")
        def codex_paths():
            market = marketplaces['.agents/plugins/marketplace.json']
            assert market['name'] == 'stdcmms-lang', 'wrong Codex marketplace name'
            entries = market['plugins']
            assert len(entries) == 1 and entries[0]['name'] == 'token-counter', 'wrong Codex entry'
            source = entries[0]['source']
            assert source['source'] == 'local', 'Codex source must be local'
            assert _local_path(repo, repo, source['path']) == repo / CODEX, 'wrong Codex source'
            plugin = (repo / CODEX).resolve()
            _tree_within(plugin)
            _local_path(repo, plugin, codex['skills'].rstrip('/'))
            skills = {p.name for p in (plugin / 'skills').iterdir() if p.is_dir()}
            assert (skills | {'cc-api'} if existing_only else skills) == {'token-report', 'token-share', 'cc-api'}, 'expected exactly three Codex skills'
            for key in ('composerIcon', 'logo', 'logoDark'):
                _local_path(repo, plugin, codex['interface'][key])
        attempt('Codex source/component paths', codex_paths)

    for relative, expected in HASHES.items():
        if optional(relative, required=True):
            digest = attempt(relative, lambda relative=relative: hashlib.sha256(_read(repo, relative)).hexdigest())
            if digest is not None and digest != expected:
                errors.append(relative + ": frozen SHA-256 mismatch")
    contract = "scripts/server_contract/"
    if optional(contract + "provenance.json", required=True):
        def provenance_check():
            provenance = _json(repo, contract + "provenance.json")
            assert provenance["server_commit"] == "9afabc0", "wrong server commit"
            datetime.date.fromisoformat(provenance["copy_date"])
            for package, version in PINS.items():
                assert provenance[package] == version, "wrong provenance dependency pin"
            for name in ("schema.ts", "validate.ts"):
                entry = provenance["files"][name]
                assert entry["source"] == "server_" + name, "wrong evidence source"
                assert entry["sha256"] == HASHES[contract + name], "wrong evidence hash"
            pkg = _json(repo, contract + "package.json")
            assert pkg['name'] == 'token-counter-server-contract-tests' and pkg['version'] == '0.1.0', 'contract package identity'
            assert pkg["private"] is True and pkg["dependencies"] == PINS, "contract package pins/private"
            assert not pkg.get("devDependencies") and not pkg.get("optionalDependencies"), "extra dependencies"
            lock = _json(repo, contract + "package-lock.json")
            assert lock['lockfileVersion'] == 3 and lock['version'] == '0.1.0', 'lockfile format/version'
            assert lock["packages"][""]["dependencies"] == PINS, "lockfile root pins"
            assert set(lock["packages"]) == {"", "node_modules/typescript", "node_modules/zod"}, "extra lockfile packages"
            for package, version in PINS.items():
                entry = lock["packages"]["node_modules/" + package]
                assert entry["version"] == version, "wrong lockfile resolved version"
                assert entry["resolved"].startswith("https://registry.npmjs.org/"), "lockfile must use public npm registry"
                assert entry['integrity'].startswith('sha512-'), 'lockfile integrity pin missing'
        attempt("server provenance/lockfile", provenance_check)

    manifest_path = CLAUDE + "/.claude-plugin/plugin.json"
    manifest_exists = optional(manifest_path, required=True)
    manifest = attempt("Claude manifest", lambda: _json(repo, manifest_path)) if manifest_exists else None
    marketplace = marketplaces.get(".claude-plugin/marketplace.json")
    if marketplace is not None:
        def marketplace_check():
            name = marketplace["name"]
            assert isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name), "invalid marketplace name"
            assert name not in RESERVED_MARKETPLACES and name not in RESERVED_NAMES, "reserved marketplace name"
            assert name == "stdcmms-lang", "wrong marketplace name"
            assert isinstance(marketplace["owner"]["name"], str) and marketplace["owner"]["name"], "missing owner name"
            assert marketplace['metadata']['version'] == '0.2.0', 'wrong Claude marketplace version'
            entries = marketplace["plugins"]
            assert isinstance(entries, list) and len(entries) == 1, "expected one Claude plugin entry"
            names = set()
            for entry in entries:
                assert _name(entry["name"]), "invalid or reserved plugin entry name"
                assert entry["name"] not in names, "duplicate plugin entry"
                names.add(entry["name"])
                target = _local_path(repo, repo, entry["source"])
                if entry["name"] == "token-counter-claude":
                    assert target == repo / CLAUDE, "wrong Claude source"
                    assert entry["version"] == "0.2.0", "wrong Claude entry version"
            assert "token-counter-claude" in names, "Claude marketplace entry missing"
        attempt("Claude marketplace rules", marketplace_check)
    if manifest is not None:
        def manifest_check():
            assert _name(manifest["name"]) and manifest["name"] == "token-counter-claude", "invalid/reserved Claude name"
            assert manifest["displayName"] == "Token Counter for Claude Code", "wrong display name"
            assert manifest["version"] == "0.2.0", "wrong Claude manifest version"
            assert "skills" not in manifest, "default skill discovery needs no manifest path"
            assert not any(k in manifest for k in ("hooks", "mcpServers", "agents")), "unexpected runtime components"
            plugin = (repo / CLAUDE).resolve()
            assert _within(plugin, repo), "Claude plugin outside repository"
            _tree_within(plugin)
            skills = {p.name for p in (plugin / 'skills').iterdir() if p.is_dir()}
            assert (skills | {'cc-api'} if existing_only else skills) == {'token-report', 'token-share', 'cc-api'}, 'expected exactly three Claude skills'
            for name in ("skills", "commands", "hooks", "agents"):
                assert not (plugin / ".claude-plugin" / name).exists(), "components must be at plugin root"
            for key in ("commands", "icon"):
                values = manifest.get(key, [])
                for value in values if isinstance(values, list) else [values]:
                    _local_path(repo, plugin, value)
            assert marketplace is not None, "Claude marketplace missing"
        attempt("Claude manifest/path rules", manifest_check)
    required = True
    package_path = CLAUDE + "/" + SCRIPT_DIR + "/tokencounter/__init__.py"
    share_path = CLAUDE + "/skills/token-share/scripts/share.py"
    if optional(package_path, required):
        version = attempt("Claude package version", lambda: _assignment(repo, package_path, "__version__"))
        if version is not None and version != "0.2.0":
            errors.append("Claude package version must be 0.2.0")
    if optional(share_path, required):
        client = attempt("Claude CLIENT", lambda: _assignment(repo, share_path, "CLIENT"))
        if client is not None and client != {"name": "claude-usage", "version": "0.2.0"}:
            errors.append("Claude CLIENT must be claude-usage 0.2.0")
    validator_path = CLAUDE + "/" + SCRIPT_DIR + "/tokencounter/share_validate.py"
    if optional(validator_path, required):
        validator_client = attempt("Claude validator CLIENT", lambda: _assignment(repo, validator_path, "CLIENT"))
        if validator_client is not None and validator_client != {"name": "claude-usage", "version": "0.2.0"}:
            errors.append("Claude validator CLIENT must be claude-usage 0.2.0")
    for name in SHARED:
        dest = CLAUDE + "/" + SCRIPT_DIR + "/tokencounter/" + name
        if optional(dest, required):
            def shared_check(name=name, dest=dest):
                source = CODEX + "/" + SCRIPT_DIR + "/tokencounter/" + name
                assert not (repo / source).is_symlink() and not (repo / dest).is_symlink(), "shared files must be real files"
                assert _read(repo, source) == _read(repo, dest), "shared file byte difference"
            attempt(dest, shared_check)
    cc_helpers = [plugin + "/skills/cc-api/scripts/cc_api.py" for plugin in (CODEX, CLAUDE)]
    cc_skills = [plugin + "/skills/cc-api/SKILL.md" for plugin in (CODEX, CLAUDE)]
    helper_exists = [optional(path, required=True) for path in cc_helpers]
    skill_exists = [optional(path, required=True) for path in cc_skills]
    for path, present in zip(cc_helpers, helper_exists):
        if present:
            def helper_check(path=path):
                assert not (repo / path).is_symlink(), "shared files must be real files"
                ast.parse(_read(repo, path).decode("utf-8"), filename=path, feature_version=(3, 8))
            attempt(path, helper_check)
    if all(helper_exists):
        def cc_bytes():
            assert _read(repo, cc_helpers[0]) == _read(repo, cc_helpers[1]), "cc-api shared file byte difference"
        attempt("cc-api shared bytes", cc_bytes)
    if skill_exists[0]:
        attempt("Codex cc-api frontmatter/commands", lambda: _codex_cc_api_frontmatter(
            repo, allow_missing_script=existing_only and not helper_exists[0]))
    if skill_exists[1]:
        attempt("Claude cc-api frontmatter/commands", lambda: _frontmatter(
            repo, cc_skills[1], "cc-api", allow_missing_script=existing_only and not helper_exists[1]))
    fixture_exists = optional("scripts/fixtures/cc_api_cases.py", required=True)
    if all(helper_exists + skill_exists) and fixture_exists:
        before = len(errors)
        attempt("cc-api isolated installed-copy smoke", lambda: _installed_cc_api_smoke(repo))
        if len(errors) == before:
            notes.append("cc-api installed-copy smoke: both plugins, python/python3 paths, fake child, inherited cwd, task on stdin then EOF, no network")
    prices_path = CLAUDE + "/assets/vendor/anthropic_prices.json"
    if optional(prices_path, required):
        def price_check():
            parser = _import_script(repo, "scripts/fetch_anthropic_prices.py", "manifest_price_parser")
            table = _json(repo, prices_path)
            assert table['as_of'] == '2026-10-10' and table['source_url'] == parser.PRICING_URL, 'price provenance differs'
            markdown = _read(repo, "scripts/fixtures/anthropic_pricing.md").decode("utf-8")
            expected = parser.parse_prices(markdown, as_of=table["as_of"], source_url=table["source_url"])
            assert expected == table, "offline price fixture differs from vendored table"
        attempt("Claude offline price parity", price_check)
    for skill in ("token-report", "token-share"):
        path = CLAUDE + "/skills/" + skill + "/SKILL.md"
        if optional(path, required):
            attempt(skill + " frontmatter/commands", lambda path=path, skill=skill: _frontmatter(repo, path, skill))
    report_exists = optional(CLAUDE + "/" + SCRIPT_DIR + "/report.py", required)
    if optional('scripts/test_codex_compat.py', required=True):
        def sensitivity_check():
            harness = _import_script(repo, 'scripts/test_codex_compat.py', 'manifest_codex_compat')
            harness.test_comparison_detects_render_byte_change()
        attempt('Codex compatibility sensitivity self-test', sensitivity_check)
    if manifest is not None and report_exists and (repo / share_path).exists() and not errors:
        attempt("Claude isolated installed-copy smoke", lambda: _installed_smoke(repo))
        if not errors:
            notes.append('Claude installed-copy smoke: report/share, python/python3 substitution, isolated imports, no network/token')
    elif manifest is None:
        notes.append("not present: Claude isolated installed-copy smoke (plugin manifest absent)")
    return errors, notes


def check(repo) -> List[str]:
    return _run(repo)[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing-only", action="store_true")
    args = parser.parse_args(argv)
    errors, notes = _run(REPO, existing_only=args.existing_only)
    for note in notes:
        print(note)
    for error in errors:
        print("[FAIL] " + error)
    print("[%s] manifest/contract checks (%d errors)" % ("FAIL" if errors else "PASS", len(errors)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
