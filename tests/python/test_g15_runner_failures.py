# SPDX-License-Identifier: MIT
"""Black-box failure handling for the shell runner, using synthetic host binaries.

These checks exercise runner control flow only; they do not test GPU behavior or
establish equivalence between any production source revisions.
"""
from pathlib import Path
import json
import os
import shlex
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
RUNNER = Path(os.environ.get("M1N1_G15_RUNNER_SOURCE", REPO / "tests/run-kboot-gpu-g15.sh"))
STANDINS = ["t8122-j613-standin-10", "t8122-j613-standin-8"]

# A compiler can deliberately leave an executable and still fail. The executable
# logs every invocation, so failed discovery must not execute partial stdout.
FAKE_TOOL = r'''
import json, os, pathlib, shlex, sys
config = json.loads(pathlib.Path(os.environ["G15_FAKE_CONFIG"]).read_text())
mode = sys.argv[1]
if mode == "compile":
    args = sys.argv[2:]
    output = pathlib.Path(args[args.index("-o") + 1])
    role = output.parent.name
else:
    role = sys.argv[2]
settings = config.get(role, {})
with open(os.environ["G15_FAKE_EVENTS"], "a") as log:
    log.write(json.dumps([mode, role] + (sys.argv[3:] if mode == "run" else [])) + "\n")
if mode == "compile":
    if settings.get("write_binary", True):
        command = " ".join(shlex.quote(x) for x in [sys.executable, __file__, "run", role])
        output.write_text('#!/bin/sh\nexec ' + command + ' "$@"\n')
        output.chmod(0o755)
    sys.exit(settings.get("build_rc", 0))
scenario = sys.argv[3]
if scenario == "list":
    sys.stdout.write(settings.get("list_text", "one\ntwo\n"))
    sys.exit(settings.get("list_rc", 0))
print("scenario:" + scenario + settings.get("output_suffix", ""))
sys.exit(9 if scenario == settings.get("fail_scenario") else 0)
'''


@pytest.fixture
def runner(tmp_path):
    tree = tmp_path / "tree"
    (tree / "tests/g15_host").mkdir(parents=True)
    (tree / "src").mkdir()
    script = tree / "tests/run-kboot-gpu-g15.sh"
    script.write_bytes(RUNNER.read_bytes())
    (tree / "src/kboot_gpu.c").write_text("/* g15_soc_t8122 */\n")
    (tree / "src/firmware.c").write_text("/* synthetic input */\n")
    (tree / "tests/g15_host/stub.h").write_text("/* synthetic header */\n")
    fixture = tree / "tests/kboot_gpu_g15_host.c"
    fixture.write_text("/* synthetic fixture */\n")
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL)
    compiler = tmp_path / "g15-runner-fake-cc"
    compiler.write_text("#!/bin/sh\nexec " + " ".join(
        shlex.quote(x) for x in [sys.executable, str(tool), "compile"]
    ) + ' "$@"\n')
    compiler.chmod(0o755)
    events = tmp_path / "events.jsonl"
    config_path = tmp_path / "config.json"
    environment = os.environ.copy()
    environment.pop("G15_SCENARIOS", None)
    environment.pop("TESTS", None)
    environment.update(CC=compiler.name, PATH=str(tmp_path) + os.pathsep + environment["PATH"],
                       G15_FAKE_EVENTS=str(events),
                       G15_FAKE_CONFIG=str(config_path))

    def run(config=None, *, compare=True, own_fixture=False, override=None, ref="HEAD"):
        config = {} if config is None else config
        if own_fixture:
            fixture.write_text('/* "t8122-j613-standin-10" */\n')
            config.setdefault("new", {}).setdefault("list_text", "one two " + " ".join(STANDINS))
        config_path.write_text(json.dumps(config))
        subprocess.run(["git", "add", "."], cwd=tree, check=True)
        subprocess.run([
            "git", "-c", "user.name=Runner Test", "-c", "user.email=runner@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
            "commit", "-qm", "synthetic runner inputs",
        ], cwd=tree, check=True)
        env = environment.copy()
        if override is not None:
            env["G15_SCENARIOS"] = override
        result = subprocess.run(["sh", str(script)] + ([ref] if compare else []),
                                cwd=tree, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        calls = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
        return result, calls

    return run


def rejected(result):
    assert result.returncode != 0, result.stdout
    assert "PASS: 0" not in result.stdout, result.stdout


@pytest.mark.parametrize("role", ["new", "base", "base-own"])
def test_compiler_failure_even_if_executable_remains(runner, role):
    result, calls = runner({role: {"build_rc": 7}}, own_fixture=role == "base-own")
    rejected(result)
    assert ["compile", role] in calls
    assert not any(call[:2] == ["run", role] for call in calls)


@pytest.mark.parametrize("role", ["new", "base", "base-own"])
def test_successful_compiler_without_executable(runner, role):
    result, _ = runner({role: {"write_binary": False}}, own_fixture=role == "base-own")
    rejected(result)


@pytest.mark.parametrize("role", ["new", "base"])
@pytest.mark.parametrize("text", ["", "one\n"])
def test_failed_discovery_never_executes_partial_stdout(runner, role, text):
    result, calls = runner({role: {"list_rc": 8, "list_text": text}})
    rejected(result)
    assert ["run", role, "list"] in calls
    assert not any(call[:2] == ["run", role] and call[2] != "list" for call in calls)


@pytest.mark.parametrize("role", ["new", "base"])
@pytest.mark.parametrize("text", ["", " \t\n\t \n"])
def test_empty_discovery_is_not_a_pass(runner, role, text):
    result, _ = runner({role: {"list_text": text}})
    rejected(result)


def test_whitespace_scenario_override_is_not_a_pass(runner):
    result, _ = runner(override=" \t\n ")
    rejected(result)


@pytest.mark.parametrize("role", ["new", "base", "base-own"])
def test_scenario_failure_is_reported(runner, role):
    scenario = STANDINS[0] if role == "base-own" else "one"
    result, calls = runner({role: {"fail_scenario": scenario}}, own_fixture=role == "base-own")
    rejected(result)
    assert ["run", role, scenario] in calls
    assert "FAIL:" in result.stdout


@pytest.mark.parametrize("role", ["base", "base-own"])
def test_comparison_difference_is_not_a_pass(runner, role):
    result, _ = runner({role: {"output_suffix": " DIFFERENT"}}, own_fixture=role == "base-own")
    rejected(result)
    assert "differs" in result.stdout


def test_selected_current_scenarios_cannot_omit_baseline_output(runner):
    result, calls = runner(override="one")
    rejected(result)
    assert ["run", "new", "two"] not in calls
    assert ["run", "base", "two"] in calls


def test_invalid_baseline_archive_fails(runner):
    result, _ = runner(ref="no-such-baseline")
    rejected(result)


@pytest.mark.parametrize("compare,own_fixture", [(False, False), (True, False), (True, True)])
def test_positive_counts_require_real_executions_and_comparisons(runner, compare, own_fixture):
    result, calls = runner(compare=compare, own_fixture=own_fixture)
    assert result.returncode == 0, result.stdout
    current_count = 4 if own_fixture else 2
    assert f"PASS: {current_count} G15 scenarios" in result.stdout
    assert len([c for c in calls if c[:2] == ["run", "new"] and c[2] != "list"]) == current_count
    if compare:
        assert "PASS: 2 T6030 and T8122" in result.stdout
        assert len([c for c in calls if c[:2] == ["run", "base"] and c[2] != "list"]) == 2
    else:
        assert not any(c[1] == "base" for c in calls)
    if own_fixture:
        assert "PASS: the stand-in experiment's scenarios match" in result.stdout
        assert len([c for c in calls if c[:2] == ["run", "base-own"]]) == 2
