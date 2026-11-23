#!/usr/bin/env python3
"""Re-runnable verification of the receipt sign-off workflow (``verify``).

Runs entirely offline with the standard library only.  Invoke with isolated
mode so a planted ``json.py`` in the working directory cannot be imported::

    python3 -I examples/receipt/verify_receipt.py

Scenarios (all logs are derived from the two shipped correction packages;
variants are generated in a temporary directory):

1. the shipped slices: the round-2024-05 group {BK-A, BK-B} has every
   expected entry posted exactly once and the amounts align in total, yet
   the entries are split across two transactions -- it cannot be signed
   off, and the round-2024-06 group depending on it is held back while an
   independent group signs off;
2. moving the earlier-round group's actual commit *after* the dependent
   round's transaction flips that transaction's verdict (including the
   other group sharing it); a genuinely independent control group is
   unaffected; posting in the right order signs everything off;
3. re-exporting identical records at the same commit/line (export overlap)
   changes nothing, but the same entry content at a *different* line of
   the transaction is a second posting and breaks the sign-off;
4. an incomplete prefix (a missing line) is an error with exit code 2 and
   never replaces the conclusion written by an earlier run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CLI = os.path.join(ROOT, "src", "cube", "cli.py")
PLAN = os.path.join(HERE, "plan.json")


def load(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return json.load(fh)


PKG1 = load("packages/round1.json")
PKG2 = load("packages/round2.json")
R1, R2 = "2024-05", "2024-06"


def entries(package, *books):
    out = []
    for rec in package["reversals"] + package["postings"]:
        if rec["book"] in books:
            out.append(rec)
    return out


def line(commit, ln, lc, rnd, e):
    return {"commit": commit, "line": ln, "line_count": lc, "round": rnd,
            "entry": e["entry"], "book": e["book"], "rect": e["rect"],
            "kind": e["kind"], "new_pair": e["new_pair"],
            "amount": e["amount"]}


def lines_of(commit, rnd, entries):
    return [line(commit, i + 1, len(entries), rnd, e)
            for i, e in enumerate(entries)]


def write_log(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for entry in lines:
            fh.write(json.dumps(entry) + "\n")


def run_verify(log_paths, cutoff, out_path=None):
    cmd = [sys.executable, "-I", CLI, "verify", "--plan", PLAN,
           "--logs", *log_paths, "--cutoff", str(cutoff)]
    if out_path:
        cmd += ["--out", out_path]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def group(conclusion, rnd, members):
    for g in conclusion["groups"]:
        if g["round"] == rnd and g["members"] == members:
            return g
    raise AssertionError(f"group {rnd}/{members} missing")


def main() -> int:
    g1 = entries(PKG1, "BK-A", "BK-B")          # round 1, prerequisite group
    g3 = entries(PKG1, "BK-D")                  # round 1, independent control
    g2 = entries(PKG2, "BK-B", "BK-C")          # round 2, depends on g1
    g5 = entries(PKG2, "BK-F")                  # round 2, no prerequisites

    # --- [1] shipped slices: split group, aligned totals, no sign-off ----
    slices = [os.path.join(HERE, "logs", "slice_a.ndjson"),
              os.path.join(HERE, "logs", "slice_b.ndjson")]
    proc = run_verify(slices, 4)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    g = group(out, R1, ["BK-A", "BK-B"])
    assert g["status"] == "receipt_not_signable", g
    assert g["reasons"][0]["reason"] == "group_entries_in_multiple_commits"
    assert g["reasons"][0]["commits"] == [1, 2]
    assert group(out, R2, ["BK-B", "BK-C"])["status"] == \
        "receipt_not_signable"
    assert group(out, R2, ["BK-B", "BK-C"])["reasons"][0]["reason"] == \
        "prerequisite_not_signable"
    assert group(out, R2, ["BK-F"])["reasons"][0]["reason"] == \
        "co_transaction_group_prerequisite_unmet"
    assert group(out, R1, ["BK-D"])["status"] == "signable"
    assert [tx["commit"] for tx in out["failed_transactions"]] == [1, 2, 3]
    assert out["stats"]["duplicate_export_lines"] == 1  # overlap, not repost
    print("[1] split across two transactions: no sign-off despite aligned "
          "totals; dependent round held back; control group signs off: OK")

    with tempfile.TemporaryDirectory(prefix="cube-receipt-demo-") as tmp:
        def log_path(name, lines):
            path = os.path.join(tmp, name)
            write_log(path, lines)
            return path

        # --- [2] earlier-round commit moved after the dependent round ---
        late = log_path("late.ndjson",
                        lines_of(1, R2, g2 + g5)
                        + lines_of(2, R1, g3)
                        + lines_of(3, R1, g1))
        proc = run_verify([late], 3)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert group(out, R1, ["BK-A", "BK-B"])["status"] == "signable"
        assert group(out, R1, ["BK-A", "BK-B"])["commit"] == 3
        reason = group(out, R2, ["BK-B", "BK-C"])["reasons"][0]
        assert reason["reason"] == "prerequisite_commit_not_earlier"
        assert (reason["prerequisite_commit"], reason["commit"]) == (3, 1)
        assert group(out, R2, ["BK-F"])["status"] == \
            "receipt_not_signable"          # sunk with its transaction
        assert group(out, R1, ["BK-D"])["status"] == "signable"  # control
        assert [tx["commit"] for tx in out["failed_transactions"]] == [1]

        ordered = log_path("ordered.ndjson",
                           lines_of(1, R1, g1)
                           + lines_of(2, R2, g2 + g5)
                           + lines_of(3, R1, g3))
        proc = run_verify([ordered], 3)
        assert proc.returncode == 0, proc.stderr
        out_ok = json.loads(proc.stdout)
        assert out_ok["failed_transactions"] == []
        assert out_ok["stats"]["groups_signable"] == 4
        print("[2] moving the earlier-round commit flips the dependent "
              "transaction (co-transaction group included); control group "
              "independent; right order signs all: OK")

        # --- [3] export overlap != duplicate posting ----------------------
        overlap = log_path("overlap.ndjson",
                           [json.loads(json.dumps(x))
                            for x in lines_of(1, R1, g1)[:2]])
        proc = run_verify([ordered, overlap], 3)
        assert proc.returncode == 0, proc.stderr
        out_overlap = json.loads(proc.stdout)
        assert out_overlap["groups"] == out_ok["groups"]
        assert out_overlap["failed_transactions"] == []
        assert out_overlap["stats"]["duplicate_export_lines"] == 2

        # Same entry content at a *different* line: a second real posting.
        doubled = lines_of(1, R1, g1)
        extra = dict(doubled[0])
        doubled.append(extra)
        for i, entry in enumerate(doubled):
            entry["line"] = i + 1
            entry["line_count"] = len(doubled)
        repost = log_path("repost.ndjson",
                          doubled + lines_of(2, R2, g2 + g5)
                          + lines_of(3, R1, g3))
        proc = run_verify([repost], 3)
        assert proc.returncode == 0, proc.stderr
        out_repost = json.loads(proc.stdout)
        g = group(out_repost, R1, ["BK-A", "BK-B"])
        assert g["status"] == "receipt_not_signable"
        assert g["reasons"][0]["reason"] == "unexpected_entry"
        assert (g["reasons"][0]["commit"], g["reasons"][0]["line"]) == (1, 5)
        print("[3] overlap at the same position deduplicated; same content "
              "at a new position is a second posting: OK")

        # --- [4] incomplete prefix never replaces the conclusion ---------
        conclusion_path = os.path.join(tmp, "conclusion.json")
        proc = run_verify([ordered], 3, out_path=conclusion_path)
        assert proc.returncode == 0, proc.stderr
        with open(conclusion_path, encoding="utf-8") as fh:
            saved = fh.read()

        broken = log_path("broken.ndjson",
                          lines_of(1, R1, g1)[:-1]  # drop the last line
                          + lines_of(2, R2, g2 + g5)
                          + lines_of(3, R1, g3))
        proc = run_verify([broken], 3, out_path=conclusion_path)
        assert proc.returncode == 2, proc
        assert "missing line" in proc.stderr, proc.stderr
        with open(conclusion_path, encoding="utf-8") as fh:
            assert fh.read() == saved
        print("[4] incomplete prefix: exit 2, earlier conclusion left "
              "untouched: OK")

    print("all receipt verification checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
