#!/usr/bin/env python3
"""The Nerd Harness gate: a test that defends nothing is a ritual, not a proof.

    python3 gate/no_cargo_cult.py
    python3 gate/no_cargo_cult.py --target ../some-repo --test-cmd "npm test"
    python3 gate/no_cargo_cult.py --sarif out.json --max-mutants 4

Exit codes are the contract shared by the conduct-harness family:

    0   no findings
    1   findings — the suite performs the ritual without the substance
    2   the gate itself failed

The third one is not decoration. A checker that returns 1 when it crashed reads
as "I found something"; one that returns 0 reads as "clean" and fails OPEN. This
repo's third discipline is NO CARGO CULT, so its own gate distinguishes its
failure from its verdict.

WHAT THIS GATE IS FOR
---------------------
`scripts/check.py` verifies that the repo keeps its documented promises. It says
nothing about whether the *tests* keep theirs. This gate asks the one question a
green suite cannot answer about itself:

    if I delete the implementation, does anything go red?

If the answer is no, that suite is a ritual performed on a schedule. CODEX.md
III is named NO CARGO CULT for exactly this, and IV.3 spells out the falsifier:
"a test passes only because it was skipped, weakened, or had its assertion
deleted." Mutation is how you find that out, and there is no cheaper way.

The other four checks are the mechanical tells that usually travel with it: a
tautological assertion, a test whose verdict depends on the clock, a snapshot
nobody compares against, and a dependency set that resolves differently
tomorrow than it did today.

WHY IT MUTATES IN PLACE
-----------------------
A suite runs against its real tree — its config, its relative imports, its
installed dependencies. Copying that tree to a scratch directory to mutate it
there would either be enormous (node_modules) or would change the thing being
measured, and a measurement that changes its subject is worth nothing.

So the mutation is applied to the file itself, and the restore is treated as the
dangerous part it is:

  * the original is held in memory AND written to `<file>.no-cargo-cult.bak`
    before the first byte changes, so an unclean kill leaves the original on disk;
  * SIGINT and SIGTERM restore before they re-raise;
  * the restore runs in a `finally` and is then VERIFIED byte for byte — a
    mismatch is exit 2, not a warning;
  * `git checkout` is never used to undo a mutation. The target may hold
    uncommitted work, and a checkout would take that work with it.

A mutation runner that leaves a file mutated has done more damage than the bug
it went looking for.

WHAT IT CANNOT MEASURE
----------------------
Mutation is implemented for Python only, because it is done through `ast` and
the mutated source is re-`compile()`d before the suite ever sees it. A regex
mutation of JavaScript would sometimes produce a file that does not parse; the
suite would go red for the wrong reason and the gate would score that as a
killed mutant. A false PASS is the one failure mode this gate must not have.

For a JS/TS target the mutation phase reports itself as NOT MEASURED and the
other four checks still run. Saying "not measured" out loud is the point: a gate
that quietly skips half its job and prints a clean line is the cargo cult it was
written to catch.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- what the walker refuses to walk into ------------------------------------

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", ".next", ".astro", ".svelte-kit", ".nuxt",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".gradle",
    "site-packages", "vendor", "third_party", "coverage", "htmlcov", "target",
}

PY_TEST_NAME = re.compile(r"^(test_.+|.+_test|conftest)\.py$")
JS_TEST_NAME = re.compile(r".+\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$")
JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
TEST_DIRS = {"tests", "test", "__tests__", "spec"}

ALLOWLIST_REL = ".conduct/cargo-cult-allow.txt"
BACKUP_SUFFIX = ".no-cargo-cult.bak"

# A test may still assert `True` when it says, in its own name or docstring, that
# it is a marked placeholder. Declared emptiness is honest; undeclared emptiness
# is the thing this gate exists to find.
PLACEHOLDER_MARKER = "placeholder"

# Calls whose presence counts as "this test asserts something".
ASSERT_CALL = re.compile(r"(^|\.)(assert\w*|expect|fail|raises|warns)$", re.IGNORECASE)

# Python calls whose value changes between two runs of the same code.
NONDETERMINISTIC_PY = {
    "random.random", "random.randint", "random.randrange", "random.choice",
    "random.choices", "random.shuffle", "random.uniform", "random.sample",
    "random.getrandbits", "datetime.now", "datetime.utcnow", "datetime.today",
    "date.today", "time.time", "time.time_ns",
}

# JavaScript equivalents. `new Date(2020, 1, 1)` is fixed; `new Date()` is not.
NONDETERMINISTIC_JS = (
    (re.compile(r"\bMath\.random\s*\("), "Math.random()"),
    (re.compile(r"\bDate\.now\s*\("), "Date.now()"),
    (re.compile(r"\bnew\s+Date\s*\(\s*\)"), "new Date()"),
    (re.compile(r"\bperformance\.now\s*\("), "performance.now()"),
)

# Evidence that the clock or the seed is pinned somewhere in this file.
# `monkeypatch.setattr` is deliberately NOT here: it patches anything at all, so
# accepting it would exempt every test that patches something unrelated. Pin the
# clock with a recognised tool, or put the file in the allowlist and say why.
FREEZE_MARKERS = (
    "random.seed(", ".seed(", "freeze_time", "freezegun", "time_machine",
    "FrozenDateTimeFactory", "useFakeTimers", "setSystemTime",
)

SETUP_FILES = ("conftest.py", "setup.ts", "setup.js", "vitest.setup.ts",
               "vitest.setup.js", "jest.setup.ts", "jest.setup.js")

SNAPSHOT_SUFFIXES = (".snap", ".ambr", ".snapshot")
SNAPSHOT_ASSERT = re.compile(
    r"toMatchSnapshot|toMatchInlineSnapshot|toMatchFileSnapshot|"
    r"toMatchImageSnapshot|assert_match|\bsnapshot\b")

JS_LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                "npm-shrinkwrap.json", "bun.lockb", "bun.lock")
PY_LOCKFILES = ("poetry.lock", "uv.lock", "pdm.lock", "Pipfile.lock",
                "requirements.lock", "constraints.txt")


class GateFailure(Exception):
    """The gate could not do its job. Always exit 2, never 0 and never 1."""


@dataclass
class Finding:
    check: str
    message: str
    path: str = ""
    line: int = 0


@dataclass
class Candidate:
    """One function the tests name, and the line span of its body."""
    path: Path
    rel: str
    name: str
    lineno: int
    first_body: int
    end: int
    col: int


@dataclass
class MutationReport:
    candidates: int = 0
    mutated: int = 0
    killed: int = 0
    survivors: list[Candidate] = field(default_factory=list)
    unmeasured: list[str] = field(default_factory=list)
    baseline_seconds: float = 0.0
    command: str = ""


# --- walking ------------------------------------------------------------------

def iter_files(root: Path):
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            yield Path(base) / name


def relpath(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def is_test_file(root: Path, path: Path) -> bool:
    parts = set(relpath(root, path).split("/")[:-1])
    if path.suffix == ".py":
        return bool(PY_TEST_NAME.match(path.name)) or bool(parts & TEST_DIRS)
    if path.suffix in JS_SUFFIXES:
        return bool(JS_TEST_NAME.match(path.name)) or bool(parts & TEST_DIRS)
    return False


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# --- allowlist ----------------------------------------------------------------

def load_allowlist(root: Path) -> list[tuple[str, str]]:
    """Read `.conduct/cargo-cult-allow.txt`.

    One entry per line, `#` starts a comment:

        <check-id> <path glob>   silence that one check for matching paths
        <path glob>              silence every check for matching paths

    Paths are POSIX and relative to the target root.
    """
    path = root / ALLOWLIST_REL
    if not path.is_file():
        return []
    entries: list[tuple[str, str]] = []
    for raw in read_text(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 1:
            entries.append(("*", parts[0]))
        else:
            entries.append((parts[0], parts[1]))
    return entries


def allowed(allow: list[tuple[str, str]], check: str, path: str) -> bool:
    return any((c == "*" or c == check) and fnmatch.fnmatch(path, glob)
               for c, glob in allow)


def report(findings: list[Finding], allow: list[tuple[str, str]], check: str,
           message: str, path: str = "", line: int = 0) -> None:
    if path and allowed(allow, check, path):
        return
    findings.append(Finding(check, message, path, line))


# --- ast helpers --------------------------------------------------------------

def parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(read_text(path), filename=str(path))
    except SyntaxError:
        return None


def dotted(node: ast.expr) -> str:
    """`a.b.c(...)` -> "a.b.c". Anything else -> ""."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def function_defs(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def body_without_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


def is_trivial(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A body with nothing to remove cannot be mutated into a different one.

    `pass`, `...`, a lone docstring, and a body that already does nothing but
    raise: neutralising any of these produces the same behaviour it had before,
    so the mutant would "survive" for a reason that has nothing to do with the
    tests. Scoring that as a finding would be inventing evidence.
    """
    body = body_without_docstring(node)
    if not body:
        return True
    if len(body) == 1 and isinstance(body[0], ast.Raise):
        return True
    return all(
        isinstance(s, ast.Pass)
        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
            and s.value.value is Ellipsis)
        for s in body)


# --- CHECK 1: mutation --------------------------------------------------------

def code_corpus(path: Path) -> str:
    """What a test file REFERENCES, with its prose left out.

    Docstrings and comments are dropped; imports, identifiers and ordinary string
    literals are kept. Prose is where a file talks ABOUT other files, and reading
    it as a reference is how the second version of this gate reported twelve
    surviving mutants in `scripts/check.py`: the docstring of the very test that
    fixed the first bug mentioned the filename, and that was enough.

    A comment is a claim. An import is a promise. Only promises are falsifiable.
    """
    if path.suffix != ".py":
        return read_text(path)
    tree = parse(path)
    if tree is None:
        return read_text(path)

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add(id(first.value))

    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            parts.append(node.value)
        elif isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, ast.Import):
            parts.extend(f"import {alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts.append(f"from {'.' * node.level}{node.module or ''}")
            parts.extend(alias.name for alias in node.names)
    return "\n".join(parts)


def module_is_named(rel: str, name: str, stem: str, corpus: str) -> bool:
    """Does the test corpus name this module by path, by filename, or by import?

    The lookbehind is the whole point: a filename only counts when it starts on a
    boundary, so `check.py` does not match inside `mutation_check.py`.
    """
    for needle in (rel, name):
        if re.search(rf"(?<![\w.-]){re.escape(needle)}", corpus):
            return True
    return bool(re.search(rf"(?:from|import)\s+[\w.]*\b{re.escape(stem)}\b", corpus))


def discover_candidates(root: Path, sources: list[Path], tests: list[Path]) -> list[Candidate]:
    """Functions the test suite claims to cover.

    "Claims to cover" is deliberately a claim, not a coverage measurement: this
    gate carries no dependencies, so it cannot read a coverage database. A module
    qualifies when the suite names it three specific ways — by relative path, by
    filename, or in an import statement — and then every non-trivial public
    function in it is a candidate. That is the promise being made. Mutation is
    what turns the promise into a measurement.

    Two looser rules were tried first and both produced phantom candidates:

      * a bare token match made `scripts/check.py` a candidate, because the word
        `check` appears in every `subprocess.run(..., check=False)` in the suite;
      * a plain substring match on the filename made it one again, because
        `check.py` is a substring of `mutation_check.py`. That one reported
        twelve untested functions as surviving mutants — findings that were
        arithmetically true and about the wrong file.

    So the filename has to sit on a boundary. A rule looser than that does not
    measure coverage, it measures vocabulary.
    """
    corpus = "\n".join(code_corpus(t) for t in tests)
    if not corpus:
        return []

    candidates: list[Candidate] = []
    for src in sources:
        rel = relpath(root, src)
        if not module_is_named(rel, src.name, src.stem, corpus):
            continue
        tree = parse(src)
        if tree is None:
            continue
        for node in function_defs(tree):
            if node.name.startswith("_"):
                continue
            if is_trivial(node):
                continue
            body = node.body
            first = body[0].lineno
            if first <= node.lineno:
                continue          # one-liner `def f(): ...`; nothing to carve out
            candidates.append(Candidate(src, rel, node.name, node.lineno, first,
                                        node.end_lineno or first, body[0].col_offset))
    candidates.sort(key=lambda c: (c.rel, c.lineno))
    return candidates


def mutate_source(text: str, cand: Candidate) -> str | None:
    """Replace a function body with a raise. None when the result will not parse."""
    lines = text.splitlines(keepends=True)
    if cand.end > len(lines):
        return None
    neutered = " " * cand.col + 'raise NotImplementedError("no-cargo-cult mutant")\n'
    mutated = "".join(lines[:cand.first_body - 1]) + neutered + "".join(lines[cand.end:])
    try:
        compile(mutated, str(cand.path), "exec")
    except SyntaxError:
        # A mutant that does not parse turns the suite red for the wrong reason,
        # and the gate would score that as "the tests defend this function".
        # That is a false PASS, so the candidate is dropped and declared instead.
        return None
    return mutated


_ACTIVE: list[tuple[Path, str]] = []


def _restore_active() -> None:
    for path, original in _ACTIVE:
        try:
            path.write_text(original, encoding="utf-8")
        except OSError:
            pass
    _ACTIVE.clear()


def _on_signal(signum: int, _frame: Any) -> None:
    _restore_active()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def run_suite(target: Path, cmd: list[str], timeout: float) -> tuple[bool, bool, float]:
    """(green, timed_out, seconds)."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for noisy in ("PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS", "HARNESS_ROOT"):
        env.pop(noisy, None)
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(target), capture_output=True, text=True,
                              timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        return False, True, time.monotonic() - started
    except (FileNotFoundError, PermissionError) as exc:
        raise GateFailure(f"test command is not runnable: {' '.join(cmd)} ({exc})") from exc
    return proc.returncode == 0, False, time.monotonic() - started


def detect_test_cmd(root: Path, tests: list[Path]) -> list[str] | None:
    if any(t.suffix == ".py" for t in tests):
        return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(read_text(pkg))
        except json.JSONDecodeError:
            return None
        declared = {**(data.get("dependencies") or {}),
                    **(data.get("devDependencies") or {})}
        if "vitest" in declared:
            return ["npx", "--no-install", "vitest", "run"]
        if "jest" in declared:
            return ["npx", "--no-install", "jest"]
        if "test" in (data.get("scripts") or {}):
            return ["npm", "test", "--silent"]
    return None


def run_mutation(root: Path, sources: list[Path], tests: list[Path],
                 cmd: list[str] | None, max_mutants: int, timeout: float,
                 findings: list[Finding], allow: list[tuple[str, str]]) -> MutationReport:
    """CHECK 1 — neutralise a function, demand the suite goes RED."""
    rep = MutationReport()
    candidates = discover_candidates(root, sources, tests)
    rep.candidates = len(candidates)

    if not candidates:
        if sources:
            rep.unmeasured.append(
                "mutation: no Python module here is named by a test file, or every "
                "function in the ones that are has a body with nothing to remove")
        else:
            rep.unmeasured.append(
                "mutation: implemented for Python only, and this target has no Python "
                "source outside its tests")
        return rep

    if cmd is None:
        rep.unmeasured.append(
            "mutation: no test command detected and none given with --test-cmd")
        return rep

    # Declared, not silently dropped. `--max-mutants` buys wall-clock time by
    # measuring less, and a gate that spends that budget without saying so is
    # reporting a coverage it does not have.
    selected = candidates[:max_mutants]
    if len(selected) < len(candidates):
        rep.unmeasured.append(
            f"mutation: {len(candidates) - len(selected)} of {len(candidates)} "
            f"candidate(s) left unmutated by --max-mutants {max_mutants}")
    if not selected:
        return rep

    rep.command = " ".join(cmd)
    green, timed_out, seconds = run_suite(root, cmd, timeout)
    rep.baseline_seconds = seconds
    if timed_out:
        raise GateFailure(
            f"the suite did not finish within {timeout:.0f}s before any mutation "
            f"({rep.command}) — raise --mutant-timeout or narrow the target")
    if not green:
        # Not a finding. Mutation measures the difference between a green suite
        # and a red one; with no green to start from there is no measurement to
        # make, and calling that a finding would be a number nobody took.
        raise GateFailure(
            f"the suite is RED before any mutation ({rep.command}) — "
            f"mutation cannot be measured against a broken baseline; fix that first")

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        for cand in selected:
            original = read_text(cand.path)
            mutated = mutate_source(original, cand)
            if mutated is None:
                rep.unmeasured.append(
                    f"mutation: {cand.rel}:{cand.lineno} {cand.name}() could not be "
                    f"neutralised without breaking the parse")
                continue
            backup = cand.path.with_name(cand.path.name + BACKUP_SUFFIX)
            try:
                backup.write_text(original, encoding="utf-8")
                _ACTIVE.append((cand.path, original))
                cand.path.write_text(mutated, encoding="utf-8")
                rep.mutated += 1
                green, timed_out, _ = run_suite(root, cmd, timeout)
                if timed_out:
                    rep.unmeasured.append(
                        f"mutation: {cand.rel}:{cand.lineno} {cand.name}() timed out "
                        f"after {timeout:.0f}s — neither killed nor survived")
                elif green:
                    rep.survivors.append(cand)
                else:
                    rep.killed += 1
            finally:
                cand.path.write_text(original, encoding="utf-8")
                _ACTIVE[:] = [e for e in _ACTIVE if e[0] != cand.path]
                if read_text(cand.path) != original:
                    raise GateFailure(
                        f"{cand.rel} was NOT restored after mutation; the original "
                        f"is in {backup.name} — restore it by hand before anything else")
                backup.unlink(missing_ok=True)
    finally:
        _restore_active()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    for cand in rep.survivors:
        report(findings, allow, "surviving-mutant",
               f"{cand.name}() was replaced with a raise and the suite stayed GREEN — "
               f"the tests name it but nothing defends it", cand.rel, cand.lineno)
    return rep


# --- CHECK 2: empty assertions ------------------------------------------------

def _asserting_helpers(tree: ast.Module) -> set[str]:
    """Module-level functions that assert, so a test delegating to one still counts."""
    out = set()
    for node in function_defs(tree):
        if node.name.startswith("test"):
            continue
        if _asserts(node, set()):
            out.add(node.name)
    return out


def _asserts(node: ast.AST, helpers: set[str]) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            return True
        if isinstance(sub, ast.Call):
            name = dotted(sub.func)
            if name and ASSERT_CALL.search(name):
                return True
            if name in helpers:
                return True
    return False


def _is_tautology(node: ast.Assert) -> bool:
    test = node.test
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.Compare) and len(test.ops) == 1 \
            and isinstance(test.ops[0], ast.Eq) \
            and isinstance(test.left, ast.Constant) \
            and isinstance(test.comparators[0], ast.Constant):
        return test.left.value == test.comparators[0].value
    return False


def _declared_placeholder(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if PLACEHOLDER_MARKER in node.name.lower():
        return True
    doc = ast.get_docstring(node) or ""
    return PLACEHOLDER_MARKER in doc.lower()


JS_TAUTOLOGY = (
    (re.compile(r"expect\s*\(\s*(true|1)\s*\)\s*\.\s*"
                r"(toBe|toEqual|toStrictEqual)\s*\(\s*(true|1)\s*\)"),
     "expect(true).toBe(true)"),
    (re.compile(r"expect\s*\(\s*true\s*\)\s*\.\s*toBeTruthy\s*\(\s*\)"),
     "expect(true).toBeTruthy()"),
    (re.compile(r"\bassert\s*\.\s*(ok|isTrue|isOk)\s*\(\s*true\s*\)"),
     "assert.ok(true)"),
    (re.compile(r"(?<![.\w])assert\s*\(\s*true\s*\)"), "assert(true)"),
)


def check_empty_assertions(root: Path, tests: list[Path], findings: list[Finding],
                           allow: list[tuple[str, str]]) -> None:
    """CHECK 2 — an assertion that cannot fail is a green light wired to nothing."""
    for path in tests:
        rel = relpath(root, path)
        if path.suffix == ".py":
            tree = parse(path)
            if tree is None:
                report(findings, allow, "unparseable-test",
                       "does not parse as Python — it cannot be running", rel)
                continue
            helpers = _asserting_helpers(tree)
            for node in function_defs(tree):
                if not node.name.startswith("test"):
                    continue
                if _declared_placeholder(node):
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Assert) and _is_tautology(sub):
                        report(findings, allow, "empty-assertion",
                               f"{node.name}() asserts a constant — it passes with the "
                               f"implementation deleted", rel, sub.lineno)
                body = body_without_docstring(node)
                if not _asserts(node, helpers):
                    only_prints = bool(body) and all(
                        isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                        and dotted(s.value.func) == "print" for s in body)
                    if only_prints:
                        report(findings, allow, "empty-assertion",
                               f"{node.name}() only prints — nothing about it can fail",
                               rel, node.lineno)
                    else:
                        report(findings, allow, "empty-assertion",
                               f"{node.name}() contains no assertion — it reports green "
                               f"whatever the code does", rel, node.lineno)
        else:
            text = read_text(path)
            for n, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("//"):
                    continue
                for pattern, label in JS_TAUTOLOGY:
                    if pattern.search(line):
                        report(findings, allow, "empty-assertion",
                               f"{label} asserts a constant — it passes with the "
                               f"implementation deleted", rel, n)
            # Per file, not per test: a brace scan of JavaScript without a parser
            # produces false positives on regex literals, and a gate with false
            # positives gets deleted in a week.
            if re.search(r"\b(it|test)\s*\(", text) and \
                    not re.search(r"\b(expect|assert|should)\b", text):
                report(findings, allow, "empty-assertion",
                       "declares tests but the file contains no assertion at all",
                       rel, 1)


# --- CHECK 3: non-determinism -------------------------------------------------

def _frozen(root: Path, path: Path) -> bool:
    """True when this file, or a setup file beside it, pins the clock or the seed."""
    texts = [read_text(path)]
    directory = path.parent
    while True:
        for name in SETUP_FILES:
            candidate = directory / name
            if candidate.is_file():
                texts.append(read_text(candidate))
        if directory == root or root not in directory.parents:
            break
        directory = directory.parent
    joined = "\n".join(texts)
    return any(marker in joined for marker in FREEZE_MARKERS)


def check_nondeterminism(root: Path, tests: list[Path], findings: list[Finding],
                         allow: list[tuple[str, str]]) -> None:
    """CHECK 3 — a test whose verdict depends on the clock lies differently each day."""
    for path in tests:
        rel = relpath(root, path)
        if _frozen(root, path):
            continue
        if path.suffix == ".py":
            tree = parse(path)
            if tree is None:
                continue                      # CHECK 2 already reported the parse
            # Through `ast`, so a pattern quoted inside a string is data, not a call.
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = dotted(node.func)
                if not name:
                    continue
                tail = ".".join(name.split(".")[-2:])
                if tail in NONDETERMINISTIC_PY or name in NONDETERMINISTIC_PY:
                    report(findings, allow, "nondeterministic-test",
                           f"{tail}() runs unseeded and unfrozen — this test's verdict "
                           f"is not the same fact twice", rel, node.lineno)
        else:
            for n, line in enumerate(read_text(path).splitlines(), 1):
                if line.lstrip().startswith("//"):
                    continue
                for pattern, label in NONDETERMINISTIC_JS:
                    if pattern.search(line):
                        report(findings, allow, "nondeterministic-test",
                               f"{label} runs unseeded and unfrozen — this test's verdict "
                               f"is not the same fact twice", rel, n)


# --- CHECK 4: orphan snapshots ------------------------------------------------

def check_orphan_snapshots(root: Path, files: list[Path], tests: list[Path],
                           findings: list[Finding], allow: list[tuple[str, str]]) -> None:
    """CHECK 4 — a stored snapshot nobody compares against is a file, not a test."""
    snapshots = [p for p in files
                 if p.suffix in SNAPSHOT_SUFFIXES or p.parent.name == "__snapshots__"]
    if not snapshots:
        return
    anywhere = any(SNAPSHOT_ASSERT.search(read_text(t)) for t in tests)

    for snap in snapshots:
        rel = relpath(root, snap)
        # `__snapshots__/foo.test.ts.snap` belongs to `foo.test.ts` one level up;
        # syrupy's `__snapshots__/test_foo.ambr` belongs to `test_foo.py`.
        home = snap.parent.parent if snap.parent.name == "__snapshots__" else snap.parent
        stem = snap.name
        for suffix in SNAPSHOT_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        companions = [p for p in tests if p.parent == home
                      and (p.name == stem or p.stem == stem)]
        if companions:
            if not any(SNAPSHOT_ASSERT.search(read_text(c)) for c in companions):
                report(findings, allow, "orphan-snapshot",
                       f"its test {relpath(root, companions[0])} never compares "
                       f"against a snapshot — the file is stored and never read", rel)
        elif not anywhere:
            report(findings, allow, "orphan-snapshot",
                   "no test in this repo compares against a snapshot — "
                   "the file is stored and never read", rel)


# --- CHECK 5: unpinned dependencies -------------------------------------------

def _has_ceiling(spec: str) -> bool:
    """True when a requirement cannot silently resolve to a newer major tomorrow."""
    text = spec.split(";")[0]
    text = re.sub(r"\[[^\]]*\]", "", text).strip()
    if not text:
        return False
    return any(marker in text for marker in ("==", "<", "~=", "^"))


def _pyproject_requirements(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    project = data.get("project") or {}
    out.extend(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        out.extend(group)
    for group in (data.get("dependency-groups") or {}).values():
        out.extend(g for g in group if isinstance(g, str))
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    groups = [poetry.get("dependencies") or {}]
    for group in (poetry.get("group") or {}).values():
        groups.append(group.get("dependencies") or {})
    for table in groups:
        for name, value in table.items():
            if name == "python":
                continue
            version = value.get("version", "*") if isinstance(value, dict) else value
            out.append(f"{name}{version}")
    return [r for r in out if isinstance(r, str)]


def check_lockfiles(root: Path, files: list[Path], findings: list[Finding],
                    allow: list[tuple[str, str]]) -> None:
    """CHECK 5 — a suite that does not pin its dependencies is not reproducible.

    Measured in a sibling repo the same week this gate was written: `ruff>=0.5`
    with no ceiling resolved to 0.15 on a laptop and 0.16 in CI, on the same
    commit. Green locally, red in CI, and neither run was wrong. When the
    dependency set is free to move, the verdict is a property of the day.
    """
    for path in files:
        rel = relpath(root, path)
        if path.name == "package.json":
            try:
                data = json.loads(read_text(path))
            except json.JSONDecodeError:
                report(findings, allow, "unparseable-manifest",
                       "is not valid JSON", rel)
                continue
            declared = {**(data.get("dependencies") or {}),
                        **(data.get("devDependencies") or {})}
            if not declared:
                continue                       # nothing to resolve, nothing to pin
            if not any((path.parent / lock).is_file() for lock in JS_LOCKFILES):
                report(findings, allow, "unpinned-dependencies",
                       f"{len(declared)} dependencies and no lockfile beside it — "
                       f"the suite resolves a different tree on a different day", rel)

        elif path.name == "pyproject.toml":
            try:
                data = tomllib.loads(read_text(path))
            except tomllib.TOMLDecodeError:
                report(findings, allow, "unparseable-manifest",
                       "is not valid TOML", rel)
                continue
            requirements = _pyproject_requirements(data)
            if not requirements:
                continue
            if any((path.parent / lock).is_file() for lock in PY_LOCKFILES):
                continue
            loose = [r for r in requirements if not _has_ceiling(r)]
            if loose:
                report(findings, allow, "unpinned-dependencies",
                       f"no lockfile and {len(loose)} requirement(s) with no upper "
                       f"bound, first `{loose[0]}` — the suite resolves a different tree "
                       f"on a different day", rel)

        elif re.fullmatch(r"requirements(-[\w.]+)?\.txt", path.name):
            if any((path.parent / lock).is_file() for lock in PY_LOCKFILES):
                continue
            loose = []
            for raw in read_text(path).splitlines():
                line = raw.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                if not _has_ceiling(line):
                    loose.append(line)
            if loose:
                report(findings, allow, "unpinned-dependencies",
                       f"{len(loose)} requirement(s) with no upper bound, first "
                       f"`{loose[0]}` — the suite resolves a different tree on a "
                       f"different day", rel)


# --- output -------------------------------------------------------------------

def to_sarif(findings: list[Finding]) -> dict[str, Any]:
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "nerd-harness-no-cargo-cult",
                "informationUri": "https://github.com/arnoldwender/nerd-harness",
                "rules": [{"id": r} for r in sorted({f.check for f in findings})],
            }},
            "results": [{
                "ruleId": f.check,
                "level": "error",
                "message": {"text": f.message},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f.path or "."},
                    "region": {"startLine": max(f.line, 1)},
                }}],
            } for f in findings],
        }],
    }


def default_target() -> Path:
    env = os.environ.get("HARNESS_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", metavar="PATH", help="repo to check (default: this one)")
    ap.add_argument("--test-cmd", metavar="CMD",
                    help="how to run the suite (default: autodetected)")
    ap.add_argument("--sarif", metavar="PATH", help="write SARIF 2.1.0 to PATH")
    ap.add_argument("--max-mutants", type=int, default=10, metavar="N",
                    help="how many functions to neutralise (default: 10)")
    ap.add_argument("--mutant-timeout", type=float, default=120.0, metavar="SECONDS",
                    help="per suite run, baseline included (default: 120)")
    args = ap.parse_args(argv)

    findings: list[Finding] = []
    mutation = MutationReport()
    try:
        root = Path(args.target).resolve() if args.target else default_target().resolve()
        if not root.is_dir():
            raise GateFailure(f"no such target directory: {root}")
        if args.max_mutants < 0:
            raise GateFailure("--max-mutants cannot be negative")

        allow = load_allowlist(root)
        files = list(iter_files(root))
        tests = [p for p in files if is_test_file(root, p)]
        test_set = set(tests)
        sources = [p for p in files if p.suffix == ".py" and p not in test_set]
        cmd = shlex.split(args.test_cmd) if args.test_cmd else detect_test_cmd(root, tests)

        # One greppable line per check: tests/mutation_check.py deletes each of
        # these in turn and requires the suite to go red. A call split across two
        # lines would silently fall out of that list.
        mutants, timeout = args.max_mutants, args.mutant_timeout
        mutation = run_mutation(root, sources, tests, cmd, mutants, timeout, findings, allow)
        check_empty_assertions(root, tests, findings, allow)
        check_nondeterminism(root, tests, findings, allow)
        check_orphan_snapshots(root, files, tests, findings, allow)
        check_lockfiles(root, files, findings, allow)
    except GateFailure as exc:
        print(f"gate failure: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                            # noqa: BLE001
        print(f"gate failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.sarif:
        Path(args.sarif).write_text(json.dumps(to_sarif(findings), indent=2),
                                    encoding="utf-8")

    print(f"no-cargo-cult: {root}")
    print(f"  {len(tests)} test file(s), {mutation.candidates} mutation candidate(s)")
    if mutation.mutated:
        print(f"  mutation via `{mutation.command}` "
              f"(baseline {mutation.baseline_seconds:.1f}s): "
              f"{mutation.mutated} mutated, {mutation.killed} killed, "
              f"{len(mutation.survivors)} survived")
    for note in mutation.unmeasured:
        print(f"  NOT MEASURED  {note}")
    for f in findings:
        where = f"{f.path}:{f.line}" if f.line else (f.path or ".")
        print(f"  FAIL [{f.check}] {where}: {f.message}")
    if findings:
        print(f"\n{len(findings)} finding(s)")
        return 1
    if mutation.mutated and not mutation.unmeasured:
        print("  no ritual found: every mutant died and every assertion can fail")
    else:
        # Exit 0 means "no findings", never "fully measured". A clean line printed
        # over an unmeasured half is the ritual this gate is named after, and the
        # gate does not get an exemption from its own rule.
        print("  no findings — read the NOT MEASURED lines above before calling "
              "this suite defended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
