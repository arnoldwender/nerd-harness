"""Tests for the no-cargo-cult gate.

Every check gets the same treatment: build a repo the gate PASSES, then plant the
one defect that check exists to catch, and require the gate to go red. A suite
that only ever sees a clean repo proves nothing — it would still pass if every
check were deleted, which is the exact failure the gate itself hunts.

    python3 -m pytest tests/ -q

The gate is invoked as a subprocess rather than imported, because the exit code
is part of the contract the whole conduct-harness family shares (0 clean,
1 findings, 2 the gate itself broke). Importing would test the functions and
leave the contract untested.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "gate" / "no_cargo_cult.py"

# --- fixture material ---------------------------------------------------------

# A body of more than one line, so there is something to carve out.
CALC = """\
def add(a, b):
    total = a + b
    return total
"""

# The test that actually runs the function: mutate `add` and this goes red.
TEST_EXERCISES = """\
from calc import add


def test_add_returns_the_sum():
    assert add(2, 3) == 5
"""

# The cargo cult: it imports the function, asserts it exists, and never calls it.
# Replace the body with a raise and this test stays green forever.
TEST_ONLY_NAMES = """\
from calc import add


def test_add_exists():
    assert add is not None
"""

TEST_ALREADY_FAILING = """\
from calc import add


def test_add_returns_the_sum():
    assert add(2, 3) == 6
"""


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(GATE), "--target", str(root), *args],
                          capture_output=True, text=True, check=False)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal Python repo the gate passes cleanly, mutation included."""
    write(tmp_path, "calc.py", CALC)
    write(tmp_path, "test_calc.py", TEST_EXERCISES)
    return tmp_path


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    """A repo with no mutation candidates, for the checks that do not need them."""
    return tmp_path


# --- the control --------------------------------------------------------------

def test_clean_repo_passes(repo: Path) -> None:
    """Without this, every test below could pass because the gate always fails."""
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no ritual found" in result.stdout


# --- CHECK 1: mutation --------------------------------------------------------

def test_mutant_dies_when_a_test_really_exercises_the_function(repo: Path) -> None:
    result = run(repo)
    assert result.returncode == 0, result.stdout
    assert "1 mutated, 1 killed, 0 survived" in result.stdout


def test_mutant_survives_when_the_test_only_names_the_function(bare: Path) -> None:
    """The whole point of the gate: a green suite that defends nothing."""
    write(bare, "calc.py", CALC)
    write(bare, "test_calc.py", TEST_ONLY_NAMES)
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "surviving-mutant" in result.stdout
    assert "add()" in result.stdout


def test_the_gate_restores_every_file_it_mutated(bare: Path) -> None:
    """A mutation runner that leaves a file mutated is worse than the bug it hunts.

    Byte for byte, and no backup left behind. This one is not optional: it is the
    check that makes the gate safe to run on a tree with uncommitted work in it.
    """
    source = write(bare, "calc.py", CALC)
    write(bare, "test_calc.py", TEST_ONLY_NAMES)
    before = source.read_bytes()

    result = run(bare)
    assert result.returncode == 1, result.stdout      # it really did mutate

    assert source.read_bytes() == before, "the gate left the file mutated"
    assert list(bare.rglob("*.no-cargo-cult.bak")) == [], "a backup file was left behind"


def test_restore_happens_on_the_killed_path_too(repo: Path) -> None:
    """A finding is not the only way out of the mutation loop."""
    source = repo / "calc.py"
    before = source.read_bytes()
    assert run(repo).returncode == 0
    assert source.read_bytes() == before
    assert list(repo.rglob("*.no-cargo-cult.bak")) == []


def test_a_suite_that_is_already_red_is_exit_2_not_1(bare: Path) -> None:
    """Mutation is the difference between a green suite and a red one.

    With no green baseline there is no difference to measure, so there is no
    finding to report — only a gate that could not do its job.
    """
    write(bare, "calc.py", CALC)
    write(bare, "test_calc.py", TEST_ALREADY_FAILING)
    result = run(bare)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "RED before any mutation" in result.stderr


def test_max_mutants_bounds_the_work_and_says_so(bare: Path) -> None:
    """Buying wall-clock time by measuring less has to be declared, not hidden."""
    write(bare, "calc.py", CALC)
    write(bare, "test_calc.py", TEST_ONLY_NAMES)
    result = run(bare, "--max-mutants", "0")
    assert result.returncode == 0, result.stdout
    assert "NOT MEASURED" in result.stdout
    assert "left unmutated by --max-mutants 0" in result.stdout


def test_a_filename_inside_a_longer_filename_is_not_a_reference(bare: Path) -> None:
    """The bug this gate found in itself on its first honest run.

    `check.py` is a substring of `mutation_check.py`, so a plain containment test
    made every function in `scripts/check.py` a candidate and reported twelve of
    them as surviving mutants. The findings were arithmetically true and about a
    file no test had ever claimed to cover.
    """
    write(bare, "calc.py", CALC)
    write(bare, "test_other.py",
          'FIXTURE = "helpers/my_calc.py"\n\n\n'
          "def test_arithmetic():\n    assert 2 + 2 == 4\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout
    assert "0 mutation candidate(s)" in result.stdout


def test_a_filename_named_only_in_prose_is_not_a_reference(bare: Path) -> None:
    """The second bug the gate found in itself, one commit after the first.

    The docstring of the test above mentioned `scripts/check.py` while explaining
    the fix — and that sentence was enough to make every function in that file a
    candidate again, and to report twelve of them as surviving mutants. A comment
    is a claim about a file; an import is a promise. The corpus keeps promises.
    """
    write(bare, "calc.py", CALC)
    write(bare, "test_other.py",
          '"""This suite eventually ought to cover calc.py as well."""\n\n\n'
          "def test_arithmetic():\n    # calc.py is next on the list\n"
          "    assert 2 + 2 == 4\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout
    assert "0 mutation candidate(s)" in result.stdout


def test_a_module_no_test_names_is_not_a_candidate(bare: Path) -> None:
    """Otherwise the gate reports every untested helper in the repo as a finding.

    The rule is "the suite claims to cover this module". A module nobody imports
    or names makes no claim, so there is no promise to falsify.
    """
    write(bare, "unrelated_helper.py", CALC)
    write(bare, "test_other.py", "def test_arithmetic():\n    assert 2 + 2 == 4\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout
    assert "0 mutation candidate(s)" in result.stdout


# --- CHECK 2: empty assertions ------------------------------------------------

def test_assert_true_is_caught(bare: Path) -> None:
    write(bare, "test_thing.py", "def test_it_works():\n    assert True\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "empty-assertion" in result.stdout


def test_assert_one_equals_one_is_caught(bare: Path) -> None:
    write(bare, "test_thing.py", "def test_it_works():\n    assert 1 == 1\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "asserts a constant" in result.stdout


def test_declared_stub_is_left_alone(bare: Path) -> None:
    """The false positive that would get the gate deleted in a week.

    Declared emptiness is honest. The gate hunts the undeclared kind.
    """
    write(bare, "test_thing.py",
          'def test_upload():\n    """Marked placeholder: the API is not built yet."""\n'
          "    assert True\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_a_test_with_no_assertion_at_all_is_caught(bare: Path) -> None:
    write(bare, "test_thing.py", "def test_the_thing():\n    value = 1 + 1\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "contains no assertion" in result.stdout


def test_a_test_that_only_prints_is_caught(bare: Path) -> None:
    write(bare, "test_thing.py", 'def test_the_thing():\n    print("ran")\n')
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "only prints" in result.stdout


def test_a_test_delegating_to_an_asserting_helper_passes(bare: Path) -> None:
    """A test whose assertion lives one call away is still a test."""
    write(bare, "test_thing.py",
          "def check_sum(a, b, expected):\n    assert a + b == expected\n\n\n"
          "def test_the_thing():\n    check_sum(2, 3, 5)\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_javascript_tautology_is_caught(bare: Path) -> None:
    write(bare, "app.test.js",
          "test('adds', () => {\n  expect(true).toBe(true);\n});\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "empty-assertion" in result.stdout


def test_javascript_file_with_no_assertion_at_all_is_caught(bare: Path) -> None:
    write(bare, "app.test.js", "test('adds', () => {\n  add(2, 3);\n});\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "no assertion at all" in result.stdout


def test_a_non_test_file_asserting_a_constant_is_not_a_finding(bare: Path) -> None:
    """`assert True` in production code is a different sin, and not this gate's."""
    write(bare, "runtime_guard.py", "def guard():\n    assert True\n    return 1\n")
    write(bare, "test_guard.py", "def test_two_plus_two():\n    assert 2 + 2 == 4\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout


# --- CHECK 3: non-determinism -------------------------------------------------

def test_unseeded_random_in_a_test_is_caught(bare: Path) -> None:
    write(bare, "test_pick.py",
          "import random\n\n\ndef test_pick():\n    assert random.random() < 1.0\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "nondeterministic-test" in result.stdout


def test_seeded_random_passes(bare: Path) -> None:
    write(bare, "test_pick.py",
          "import random\n\n\ndef test_pick():\n    random.seed(7)\n"
          "    assert random.random() < 1.0\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_datetime_now_in_a_test_is_caught(bare: Path) -> None:
    write(bare, "test_clock.py",
          "from datetime import datetime\n\n\ndef test_stamp():\n"
          "    assert datetime.now().year > 2000\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "datetime.now()" in result.stdout


def test_frozen_clock_passes(bare: Path) -> None:
    write(bare, "test_clock.py",
          "from datetime import datetime\nfrom freezegun import freeze_time\n\n\n"
          '@freeze_time("2026-01-01")\ndef test_stamp():\n'
          "    assert datetime.now().year == 2026\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_a_pattern_quoted_inside_a_string_is_not_a_call(bare: Path) -> None:
    """The false positive a regex scanner cannot avoid and `ast` never has.

    This file MENTIONS the pattern; it does not run it. A gate that cannot tell
    the difference fires on its own test fixtures — which is how this one is
    written, so the failure would be immediate and permanent.
    """
    write(bare, "test_sample.py",
          'SAMPLE = "def f():\\n    return random.random()\\n"\n\n\n'
          'def test_sample_is_text():\n    assert "random" in SAMPLE\n')
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_javascript_date_now_is_caught(bare: Path) -> None:
    write(bare, "app.test.ts",
          "test('now', () => {\n  expect(Date.now()).toBeGreaterThan(0);\n});\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "Date.now()" in result.stdout


def test_javascript_date_now_under_fake_timers_passes(bare: Path) -> None:
    write(bare, "app.test.ts",
          "beforeEach(() => { vi.useFakeTimers(); });\n"
          "test('now', () => {\n  expect(Date.now()).toBeGreaterThan(0);\n});\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout


# --- CHECK 4: orphan snapshots ------------------------------------------------

def test_snapshot_nobody_compares_against_is_caught(bare: Path) -> None:
    write(bare, "app.test.js",
          "test('render', () => {\n  expect(render()).toEqual('<p></p>');\n});\n")
    write(bare, "__snapshots__/app.test.js.snap", "exports[`render 1`] = `<p></p>`;\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "orphan-snapshot" in result.stdout


def test_snapshot_with_a_matching_assertion_passes(bare: Path) -> None:
    write(bare, "app.test.js",
          "test('render', () => {\n  expect(render()).toMatchSnapshot();\n});\n")
    write(bare, "__snapshots__/app.test.js.snap", "exports[`render 1`] = `<p></p>`;\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_python_snapshot_file_with_no_comparison_is_caught(bare: Path) -> None:
    """syrupy stores `.ambr`; an orphan one is the same failure in another suffix."""
    write(bare, "test_render.py",
          "def test_render():\n    assert 1 + 1 == 2\n")
    write(bare, "__snapshots__/test_render.ambr", "# name: test_render\n  '<p></p>'\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "orphan-snapshot" in result.stdout


# --- CHECK 5: unpinned dependencies -------------------------------------------

def test_package_json_without_a_lockfile_is_caught(bare: Path) -> None:
    write(bare, "package.json", '{"name": "x", "dependencies": {"left-pad": "^1.0.0"}}\n')
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "unpinned-dependencies" in result.stdout


def test_package_json_with_a_lockfile_passes(bare: Path) -> None:
    write(bare, "package.json", '{"name": "x", "dependencies": {"left-pad": "^1.0.0"}}\n')
    write(bare, "package-lock.json", '{"lockfileVersion": 3}\n')
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_package_json_with_no_dependencies_needs_no_lockfile(bare: Path) -> None:
    """Nothing to resolve, nothing to pin. Firing here would be noise."""
    write(bare, "package.json", '{"name": "x", "private": true}\n')
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_pyproject_with_an_open_upper_bound_is_caught(bare: Path) -> None:
    """The one that actually shipped: `ruff>=0.5` was 0.15 locally and 0.16 in CI."""
    write(bare, "pyproject.toml",
          '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = ["ruff>=0.5"]\n')
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "no upper bound" in result.stdout
    assert "ruff>=0.5" in result.stdout


def test_pyproject_with_a_pinned_requirement_passes(bare: Path) -> None:
    write(bare, "pyproject.toml",
          '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = ["ruff==0.15.0"]\n')
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_pyproject_with_a_lockfile_passes(bare: Path) -> None:
    write(bare, "pyproject.toml",
          '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = ["ruff>=0.5"]\n')
    write(bare, "uv.lock", "version = 1\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_requirements_txt_without_pins_is_caught(bare: Path) -> None:
    write(bare, "requirements.txt", "# deps\nruff\npytest>=8\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout
    assert "unpinned-dependencies" in result.stdout


def test_requirements_txt_fully_pinned_passes(bare: Path) -> None:
    write(bare, "requirements.txt", "ruff==0.15.0\npytest==9.0.3\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout


# --- the allowlist ------------------------------------------------------------

def test_an_allowlist_entry_silences_exactly_one_check(bare: Path) -> None:
    write(bare, "test_thing.py", "def test_it_works():\n    assert True\n")
    assert run(bare).returncode == 1
    write(bare, ".conduct/cargo-cult-allow.txt",
          "# the API is stubbed until the vendor ships\nempty-assertion test_thing.py\n")
    result = run(bare)
    assert result.returncode == 0, result.stdout


def test_an_allowlist_entry_for_another_check_does_not_silence_this_one(bare: Path) -> None:
    """An allowlist that silences everything is a mute button, not a decision."""
    write(bare, "test_thing.py", "def test_it_works():\n    assert True\n")
    write(bare, ".conduct/cargo-cult-allow.txt", "orphan-snapshot test_thing.py\n")
    result = run(bare)
    assert result.returncode == 1, result.stdout


# --- the contract -------------------------------------------------------------

def test_harness_root_env_selects_the_target(bare: Path) -> None:
    write(bare, "test_thing.py", "def test_it_works():\n    assert True\n")
    env = {**os.environ, "HARNESS_ROOT": str(bare)}
    result = subprocess.run([sys.executable, str(GATE)], capture_output=True,
                            text=True, env=env, check=False)
    assert result.returncode == 1, result.stdout
    assert "empty-assertion" in result.stdout


def test_a_missing_target_is_exit_2_not_a_pass(tmp_path: Path) -> None:
    """When the input is absent the answer is never 'clean'."""
    result = run(tmp_path / "does-not-exist")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no such target directory" in result.stderr


def test_an_empty_repo_is_clean_but_says_what_it_could_not_measure(bare: Path) -> None:
    result = run(bare)
    assert result.returncode == 0, result.stdout
    assert "NOT MEASURED" in result.stdout


# --- SARIF --------------------------------------------------------------------

def test_sarif_is_written_and_well_formed(bare: Path, tmp_path: Path) -> None:
    write(bare, "test_thing.py", "def test_it_works():\n    assert True\n")
    out = tmp_path / "out.sarif"
    result = run(bare, "--sarif", str(out))
    assert result.returncode == 1, result.stdout
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["results"], "SARIF carries no results for a failing run"
    assert doc["runs"][0]["results"][0]["ruleId"] == "empty-assertion"
