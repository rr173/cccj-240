#!/usr/bin/env python3
"""Re-runnable verification of the approval correction-package workflow.

Runs entirely offline with the standard library only.  Invoke with isolated
mode so a planted ``json.py`` in the working directory cannot be imported::

    python3 -I examples/approval/verify_approval.py

The script checks, on the chain fixture shipped beside it:

1. a first approval round missing the last chain id (H4) freezes the whole
   three-link chain while the unrelated H5 group posts;
2. continuing from the package's own effective handover, the second round
   posts the chain; the cumulative ledger per new code pair (old postings +
   both correction packages) equals a fresh recode of the final effective
   handover;
3. the withheld proposed variant blocks the chain group even with every id
   approved, and never reverses an old record of that group;
4. shuffling the input write order leaves the package content unchanged.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cube.approval import build_correction_package  # noqa: E402
from cube.engine import build_cube  # noqa: E402
from cube.loader import load_handover  # noqa: E402
from cube.mapping import load_mapping  # noqa: E402
from cube.recode import convert_handover  # noqa: E402


def load(name: str) -> dict:
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def pair(rec):
    return rec["new_pair"]["X"], rec["new_pair"]["Y"]


def ledger(handover, maps):
    batch = convert_handover(handover, build_cube(handover), maps)
    out = defaultdict(int)
    for rec in batch["ingested"]:
        out[pair(rec)] += rec["amount"]
    return dict(out)


def main() -> int:
    dims = load("dims_chain.json")
    cur = load("books_current.json")
    prop = load("books_proposed.json")
    withheld = load("books_proposed_withheld.json")
    handover = load_handover(dims, cur)
    maps = load_mapping(load("mapping_chain.json"),
                        handover.tree_x, handover.tree_y)

    # --- round 1: H4 (last chain id) not approved --------------------------
    first = build_correction_package(
        dims, cur, prop, ["H1", "H2", "H3", "H5"], maps
    )
    chain = next(g for g in first["groups"] if len(g["members"]) == 4)
    assert not chain["effective"] and chain["missing_approvals"] == ["H4"]
    h5 = next(g for g in first["groups"] if g["members"] == ["H5"])
    assert h5["effective"]
    touched = {r["book"] for r in first["reversals"]}
    touched |= {r["book"] for r in first["postings"]}
    assert touched == {"H5"}, touched
    print("[1] chain frozen without H4; only isolated H5 posted: OK")

    # --- round 2: continue from the package's effective handover ----------
    mid = {"books": first["effective_handover"]["books"]}
    second = build_correction_package(
        dims, mid, prop, ["H1", "H2", "H3", "H4", "H5"], maps
    )
    assert all(g["effective"] for g in second["groups"])
    final = {"books": second["effective_handover"]["books"]}

    cumulative = defaultdict(int, ledger(handover, maps))
    for pkg in (first, second):
        for rec in pkg["reversals"]:
            cumulative[pair(rec)] += rec["amount"]
        for rec in pkg["postings"]:
            cumulative[pair(rec)] += rec["amount"]
    expected = ledger(load_handover(dims, final), maps)
    assert dict(cumulative) == expected, (dict(cumulative), expected)
    print(f"[2] cumulative ledger {dict(sorted(cumulative.items()))} "
          f"== final recode: OK")

    # --- withheld proposed book blocks its whole group --------------------
    blocked = build_correction_package(
        dims, cur, withheld, ["H1", "H2", "H3", "H4", "H5"], maps
    )
    bchain = next(g for g in blocked["groups"] if len(g["members"]) == 4)
    assert not bchain["effective"]
    assert [b["book"] for b in bchain["withheld_books"]] == ["H2"]
    touched_w = {r["book"] for r in blocked["reversals"]}
    touched_w |= {r["book"] for r in blocked["postings"]}
    assert touched_w == {"H5"}, touched_w
    print("[3] withheld H2 blocks the whole chain, no old record reversed: OK")

    # --- determinism under shuffled write order ---------------------------
    cur_s, prop_s = copy.deepcopy(cur), copy.deepcopy(prop)
    cur_s["books"].reverse()
    prop_s["books"].reverse()
    shuffled = build_correction_package(
        dims, cur_s, prop_s,
        ["H5", "H4", "H3", "H2", "H1"], maps,
    )
    full = build_correction_package(
        dims, cur, prop, ["H1", "H2", "H3", "H4", "H5"], maps
    )
    assert json.dumps(shuffled, sort_keys=True) == \
        json.dumps(full, sort_keys=True)
    print("[4] shuffled write order gives an identical package: OK")

    # --- the CLI also writes a package atomically -------------------------
    out = os.path.join(HERE, "out", "cli_round1.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cmd = [
        sys.executable, "-I",
        os.path.join(ROOT, "src", "cube", "cli.py"), "approve",
        "--dims", os.path.join(HERE, "dims_chain.json"),
        "--mapping", os.path.join(HERE, "mapping_chain.json"),
        "--current", os.path.join(HERE, "books_current.json"),
        "--proposed", os.path.join(HERE, "books_proposed.json"),
        "--approvals", os.path.join(HERE, "approvals_round1.json"),
        "--out", out,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.load(open(out, encoding="utf-8"))["format"] == \
        "correction_package/v1"
    print(f"[5] CLI wrote {os.path.relpath(out, ROOT)}: OK")
    print("all approval checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
