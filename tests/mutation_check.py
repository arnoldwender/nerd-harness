#!/usr/bin/env python3
"""Prove the no-cargo-cult gate's tests actually defend it.

    python3 tests/mutation_check.py

Yes: a mutation check on the gate that runs mutation checks. That is not a joke
at the repo's expense, it is the only consistent position it can hold. A gate
whose thesis is "a test that survives the implementation being deleted is a
ritual" does not get to exempt its own tests from the same question.

For each check in gate/no_cargo_cult.py: delete it, run the suite, and require
the suite to go RED. A test that still passes with the mechanism removed is not
testing the mechanism.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run (the same contract as the gate).

The file is restored from an in-memory copy in a `finally`, never with
`git checkout`: this repo may hold uncommitted work, and a checkout to undo a
mutation would take that work with it.

Cost: one full suite run per mutant, plus one for the baseline. Measured on this
repo at roughly four seconds each.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "gate" / "no_cargo_cult.py"

# (name, the exact line in main() or the loop to neuter, what to put in its place)
#
# The first five are the checks themselves. The last two are the promises the
# gate makes about its own safety — that it restores what it mutated, and that
# the allowlist silences something. Those are behaviour too, and behaviour with
# no test behind it is a claim.
MUTANTS = [
    ("CHECK 1 mutation",
     "mutation = run_mutation(root, sources, tests, cmd, mutants, timeout, findings, allow)",
     "mutation = MutationReport()"),
    ("CHECK 2 empty-assertion",
     "check_empty_assertions(root, tests, findings, allow)", ""),
    ("CHECK 3 nondeterminism",
     "check_nondeterminism(root, tests, findings, allow)", ""),
    ("CHECK 4 orphan-snapshot",
     "check_orphan_snapshots(root, files, tests, findings, allow)", ""),
    ("CHECK 5 unpinned-dependencies",
     "check_lockfiles(root, files, findings, allow)", ""),
    ("SAFETY restore-after-mutation",
     'cand.path.write_text(original, encoding="utf-8")', ""),
    ("SAFETY allowlist",
     "if path and allowed(allow, check, path):", "if False:"),
]


def run_suite() -> bool:
    """True when the suite is green."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q", "-x",
         "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(ROOT), check=False)
    return result.returncode == 0


def main() -> int:
    original = GATE.read_text(encoding="utf-8")

    if not run_suite():
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2

    survivors: list[str] = []
    try:
        for name, call, replacement in MUTANTS:
            if original.count(call) != 1:
                print(f"  ?? {name}: the line is not in the gate exactly once — "
                      f"the mutation list is stale")
                survivors.append(f"{name} (stale)")
                continue
            GATE.write_text(original.replace(call, replacement or "pass", 1),
                            encoding="utf-8")
            if run_suite():
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    finally:
        GATE.write_text(original, encoding="utf-8")

    # The restore itself is verified. A mutation runner that leaves the file
    # mutated has done more harm than the bug it was hunting.
    if GATE.read_text(encoding="utf-8") != original:
        print("gate file was NOT restored cleanly", file=sys.stderr)
        return 2

    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; gate restored and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
