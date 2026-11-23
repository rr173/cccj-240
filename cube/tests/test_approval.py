"""Approval tests: correction groups, effectiveness, entries, ledger closure.

Covers the acceptance rules of the ``approve`` command:

* a three-handover chain is one irreducible group; missing the last id's
  approval freezes the whole chain while an unrelated group still posts;
* a withheld proposed book blocks its whole group even with all approvals,
  and no old record of the group is reversed;
* the cumulative ledger (old postings plus two correction packages) equals a
  fresh recode of the final effective handover, per new code pair;
* reversals keep book/cell/kind/new_pair and only flip the sign, zero and
  negative amounts included; reversals and postings are never netted;
* added/removed books, empty lists, unknown approvals, determinism.

Run:  python3 -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cube.approval import (  # noqa: E402
    build_correction_package,
    book_signature,
)
from cube.engine import build_cube  # noqa: E402
from cube.loader import load_handover  # noqa: E402
from cube.mapping import load_mapping  # noqa: E402
from cube.model import ModelError  # noqa: E402
from cube.recode import convert_handover  # noqa: E402

APPROVAL_DIR = os.path.join(ROOT, "examples", "approval")


def _load(name: str) -> dict:
    with open(os.path.join(APPROVAL_DIR, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _maps(dims, cur):
    handover = load_handover(dims, cur)
    return load_mapping(_load("mapping_chain.json"), handover.tree_x,
                        handover.tree_y)


def _pair(rec):
    return rec["new_pair"]["X"], rec["new_pair"]["Y"]


def _ingested_ledger(batch):
    out = defaultdict(int)
    for rec in batch["ingested"]:
        out[_pair(rec)] += rec["amount"]
    return dict(out)


class TestChainGrouping(unittest.TestCase):
    def setUp(self):
        self.dims = _load("dims_chain.json")
        self.cur = _load("books_current.json")
        self.prop = _load("books_proposed.json")
        self.maps = _maps(self.dims, self.cur)

    def test_three_link_chain_is_one_group_and_h5_is_isolated(self):
        pkg = build_correction_package(
            self.dims, self.cur, self.prop,
            ["H1", "H2", "H3", "H4", "H5"], self.maps,
        )
        groups = {tuple(g["members"]): g for g in pkg["groups"]}
        self.assertIn(("H1", "H2", "H3", "H4"), groups)
        self.assertIn(("H5",), groups)
        self.assertTrue(groups[("H1", "H2", "H3", "H4")]["effective"])
        self.assertTrue(groups[("H5",)]["effective"])

    def test_missing_last_chain_id_freezes_whole_chain_but_not_h5(self):
        pkg = build_correction_package(
            self.dims, self.cur, self.prop,
            ["H1", "H2", "H3", "H5"], self.maps,
        )
        chain = next(g for g in pkg["groups"]
                     if tuple(g["members"]) == ("H1", "H2", "H3", "H4"))
        self.assertFalse(chain["effective"])
        self.assertEqual(chain["missing_approvals"], ["H4"])
        self.assertEqual(chain["withheld_books"], [])
        h5 = next(g for g in pkg["groups"] if g["members"] == ["H5"])
        self.assertTrue(h5["effective"])
        # No chain book produces any entry; only H5 posts.
        touched = {r["book"] for r in pkg["reversals"]}
        touched |= {r["book"] for r in pkg["postings"]}
        self.assertEqual(touched, {"H5"})
        # The frozen chain keeps every old book in the effective handover.
        ids = {b["id"] for b in pkg["effective_handover"]["books"]}
        self.assertEqual(ids, {"H1", "H2", "H3", "H4", "H5"})
        h3 = next(b for b in pkg["effective_handover"]["books"]
                  if b["id"] == "H3")
        self.assertEqual(h3["rect"], ["x2", "y2"])  # old rect, not proposed

    def test_full_approval_effective_handover_equals_proposed_content(self):
        pkg = build_correction_package(
            self.dims, self.cur, self.prop,
            ["H1", "H2", "H3", "H4", "H5"], self.maps,
        )
        got = {b["id"]: b for b in pkg["effective_handover"]["books"]}
        want = {b["id"]: b for b in self.prop["books"]}
        self.assertEqual(set(got), set(want))
        for bid, book in want.items():
            self.assertEqual(book_signature(got[bid]), book_signature(book))
        # It reloads as a valid, fully publishable handover.
        cube = build_cube(load_handover(
            self.dims, {"books": pkg["effective_handover"]["books"]}
        ))
        self.assertEqual(cube.withheld, {})


class TestReversalAndPostingEntries(unittest.TestCase):
    def setUp(self):
        self.dims = _load("dims_chain.json")
        self.cur = _load("books_current.json")
        self.prop = _load("books_proposed.json")
        self.maps = _maps(self.dims, self.cur)
        self.pkg = build_correction_package(
            self.dims, self.cur, self.prop,
            ["H1", "H2", "H3", "H4", "H5"], self.maps,
        )
        handover = load_handover(self.dims, self.cur)
        self.old_batch = convert_handover(
            handover, build_cube(handover), self.maps
        )

    def test_reversals_flip_sign_but_keep_identity_fields(self):
        old = {r["book"]: r for r in self.old_batch["ingested"]}
        self.assertEqual(
            sorted(r["book"] for r in self.pkg["reversals"]),
            sorted(old),
        )
        for rev in self.pkg["reversals"]:
            src = old[rev["book"]]
            self.assertEqual(rev["entry"], "reversal")
            self.assertEqual(rev["amount"], -src["amount"])
            self.assertEqual(rev["rect"], src["rect"])
            self.assertEqual(rev["kind"], src["kind"])
            self.assertEqual(rev["new_pair"], src["new_pair"])
        # H4 was negative -> its reversal is positive; H3 new value is zero
        # and still produces a zero posting (no amount dropped).
        rev_h4 = next(r for r in self.pkg["reversals"] if r["book"] == "H4")
        self.assertEqual(rev_h4["amount"], 7)
        post_h3 = [r for r in self.pkg["postings"] if r["book"] == "H3"]
        self.assertEqual(len(post_h3), 1)
        self.assertEqual(post_h3[0]["amount"], 0)

    def test_reversals_and_postings_are_kept_separate_never_netted(self):
        # H1 reversal and posting both land on (NX, NY12); they remain two
        # records with opposite entry tags rather than one netted record.
        rev_h1 = [r for r in self.pkg["reversals"] if r["book"] == "H1"]
        post_h1 = [r for r in self.pkg["postings"] if r["book"] == "H1"]
        self.assertEqual(len(rev_h1), 1)
        self.assertEqual(len(post_h1), 1)
        self.assertEqual(rev_h1[0]["new_pair"], post_h1[0]["new_pair"])
        self.assertEqual({r["entry"] for r in rev_h1 + post_h1},
                         {"reversal", "posting"})
        for rec in self.pkg["postings"]:
            self.assertEqual(rec["entry"], "posting")

    def test_parked_and_missing_material_never_creates_entries(self):
        # Every chain book is an exact terminal here, but guard the contract:
        # entries only ever carry recode ingested records.
        for rec in self.pkg["reversals"] + self.pkg["postings"]:
            self.assertIn("new_pair", rec)
            self.assertIsNotNone(rec["new_pair"])
            self.assertNotIn("reason", rec)


class TestWithheldProposedBook(unittest.TestCase):
    def setUp(self):
        self.dims = _load("dims_chain.json")
        self.cur = _load("books_current.json")
        self.wh = _load("books_proposed_withheld.json")
        self.maps = _maps(self.dims, self.cur)

    def test_withheld_member_blocks_whole_group_even_fully_approved(self):
        pkg = build_correction_package(
            self.dims, self.cur, self.wh,
            ["H1", "H2", "H3", "H4", "H5"], self.maps,
        )
        chain = next(g for g in pkg["groups"]
                     if tuple(g["members"]) == ("H1", "H2", "H3", "H4"))
        self.assertFalse(chain["effective"])
        self.assertEqual(chain["missing_approvals"], [])
        self.assertEqual([b["book"] for b in chain["withheld_books"]], ["H2"])
        self.assertTrue(chain["withheld_books"][0]["reasons"])
        # No old record of the blocked group may be reversed or posted.
        touched = {r["book"] for r in pkg["reversals"]}
        touched |= {r["book"] for r in pkg["postings"]}
        self.assertEqual(touched, {"H5"})
        # The whole group stays old in the effective handover.
        h2 = next(b for b in pkg["effective_handover"]["books"]
                  if b["id"] == "H2")
        self.assertEqual(h2["rect"], ["x2", "y1"])  # current rect
        self.assertNotIn("control", h2["root"])

    def test_withholding_is_reported_not_input_corruption(self):
        # build_correction_package returns normally; only structure errors raise.
        pkg = build_correction_package(
            self.dims, self.cur, self.wh,
            ["H1", "H2", "H3", "H4", "H5"], self.maps,
        )
        self.assertEqual(pkg["format"], "correction_package/v1")


class TestCumulativeLedgerClosure(unittest.TestCase):
    def test_two_round_cumulative_ledger_equals_final_recode(self):
        dims = _load("dims_chain.json")
        cur = _load("books_current.json")
        prop = _load("books_proposed.json")
        maps = _maps(dims, cur)

        first = build_correction_package(
            dims, cur, prop, ["H1", "H2", "H3", "H5"], maps
        )
        mid = {"books": first["effective_handover"]["books"]}
        second = build_correction_package(
            dims, mid, prop, ["H1", "H2", "H3", "H4", "H5"], maps
        )
        final = {"books": second["effective_handover"]["books"]}

        handover0 = load_handover(dims, cur)
        cumulative = defaultdict(int)
        cumulative.update(_ingested_ledger(
            convert_handover(handover0, build_cube(handover0), maps)
        ))
        for pkg in (first, second):
            for rec in pkg["reversals"]:
                cumulative[_pair(rec)] += rec["amount"]
            for rec in pkg["postings"]:
                cumulative[_pair(rec)] += rec["amount"]

        handover_final = load_handover(dims, final)
        expected = _ingested_ledger(
            convert_handover(handover_final, build_cube(handover_final), maps)
        )
        self.assertEqual(dict(cumulative), expected)
        # After round one H5 is already identical to proposed, so round two's
        # candidate set only contains the four chain members.
        self.assertEqual(second["stats"]["candidate_books"], 4)
        self.assertEqual(second["groups"][0]["members"],
                         ["H1", "H2", "H3", "H4"])


class TestAddedRemovedAndUnchanged(unittest.TestCase):
    DIMS = {
        "dimensions": {
            "X": {"root": "X", "nodes": {"X": ["a", "b"], "a": [], "b": []}},
            "Y": {"root": "Y", "nodes": {"Y": ["p", "q"], "p": [], "q": []}},
        }
    }
    MAPS = {
        "X": [{"old": "X", "new": "NX"}],
        "Y": [{"old": "Y", "new": "NY"}],
    }

    def _book(self, bid, rect, value):
        return {"id": bid, "rect": list(rect),
                "root": {"rect": list(rect), "state": "exact", "value": value}}

    def test_removed_book_is_reversed_without_posting(self):
        cur = {"books": [self._book("R", ("a", "p"), 9)]}
        prop = {"books": []}
        h = load_handover(self.DIMS, cur)
        maps = load_mapping(self.MAPS, h.tree_x, h.tree_y)
        pkg = build_correction_package(self.DIMS, cur, prop, ["R"], maps)
        self.assertEqual(pkg["groups"][0]["members"], ["R"])
        self.assertTrue(pkg["groups"][0]["effective"])
        self.assertEqual(len(pkg["reversals"]), 1)
        self.assertEqual(pkg["reversals"][0]["amount"], -9)
        self.assertEqual(pkg["postings"], [])
        self.assertEqual(pkg["effective_handover"]["books"], [])

    def test_added_book_is_posted_without_reversal(self):
        cur = {"books": []}
        prop = {"books": [self._book("N", ("b", "q"), 4)]}
        h = load_handover(self.DIMS, prop)
        maps = load_mapping(self.MAPS, h.tree_x, h.tree_y)
        pkg = build_correction_package(self.DIMS, cur, prop, ["N"], maps)
        self.assertEqual(pkg["reversals"], [])
        self.assertEqual(len(pkg["postings"]), 1)
        self.assertEqual(pkg["postings"][0]["amount"], 4)
        self.assertEqual(
            [b["id"] for b in pkg["effective_handover"]["books"]], ["N"]
        )

    def test_unchanged_book_produces_no_entries_even_when_approved(self):
        cur = {"books": [self._book("U", ("a", "p"), 3)]}
        prop = copy.deepcopy(cur)
        h = load_handover(self.DIMS, cur)
        maps = load_mapping(self.MAPS, h.tree_x, h.tree_y)
        pkg = build_correction_package(self.DIMS, cur, prop, ["U"], maps)
        self.assertEqual(pkg["groups"], [])
        self.assertEqual(pkg["reversals"], [])
        self.assertEqual(pkg["postings"], [])
        self.assertEqual(
            [b["id"] for b in pkg["effective_handover"]["books"]], ["U"]
        )

    def test_sibling_write_order_is_not_a_change(self):
        base = {"books": [{
            "id": "S", "rect": ["X", "Y"],
            "root": {"rect": ["X", "Y"], "split": "X", "children": [
                {"rect": ["a", "Y"], "state": "exact", "value": 1},
                {"rect": ["b", "Y"], "state": "exact", "value": 2},
            ]},
        }]}
        shuffled = copy.deepcopy(base)
        shuffled["books"][0]["root"]["children"].reverse()
        self.assertEqual(
            book_signature(base["books"][0]),
            book_signature(shuffled["books"][0]),
        )
        h = load_handover(self.DIMS, base)
        maps = load_mapping(self.MAPS, h.tree_x, h.tree_y)
        pkg = build_correction_package(
            self.DIMS, base, shuffled, ["S"], maps
        )
        self.assertEqual(pkg["stats"]["candidate_books"], 0)

    def test_unknown_approval_id_is_an_error(self):
        cur = {"books": [self._book("U", ("a", "p"), 3)]}
        h = load_handover(self.DIMS, cur)
        maps = load_mapping(self.MAPS, h.tree_x, h.tree_y)
        with self.assertRaises(ModelError):
            build_correction_package(
                self.DIMS, cur, {"books": []}, ["GHOST"], maps
            )

    def test_empty_book_lists_on_both_sides_are_valid(self):
        empty = {"books": []}
        h = load_handover(self.DIMS, empty, allow_empty=True)
        maps = load_mapping(self.MAPS, h.tree_x, h.tree_y)
        pkg = build_correction_package(self.DIMS, empty, empty, [], maps)
        self.assertEqual(pkg["groups"], [])
        self.assertEqual(pkg["reversals"], [])
        self.assertEqual(pkg["postings"], [])
        self.assertEqual(pkg["effective_handover"]["books"], [])
        self.assertEqual(pkg["stats"]["candidate_books"], 0)

    def test_unapproved_added_book_is_held_without_posting(self):
        cur = {"books": []}
        prop = {"books": [self._book("N", ("b", "q"), 4)]}
        h = load_handover(self.DIMS, prop)
        maps = load_mapping(self.MAPS, h.tree_x, h.tree_y)
        pkg = build_correction_package(self.DIMS, cur, prop, [], maps)
        self.assertFalse(pkg["groups"][0]["effective"])
        self.assertEqual(pkg["groups"][0]["missing_approvals"], ["N"])
        self.assertEqual(pkg["postings"], [])
        self.assertEqual(pkg["effective_handover"]["books"], [])

    def test_same_id_withdraw_and_post_are_atomic_even_disjoint_rects(self):
        # One id moves to a rectangle with no leaf pair in common with its old
        # responsibility; the withdrawal and posting must still be one group.
        cur = {"books": [self._book("M", ("a", "p"), 5)]}
        prop = {"books": [self._book("M", ("b", "q"), 9)]}
        h = load_handover(self.DIMS, cur)
        maps = load_mapping(self.MAPS, h.tree_x, h.tree_y)
        pkg = build_correction_package(self.DIMS, cur, prop, ["M"], maps)
        self.assertEqual(pkg["groups"][0]["members"], ["M"])
        self.assertEqual([r["rect"] for r in pkg["reversals"]], [["a", "p"]])
        self.assertEqual([r["rect"] for r in pkg["postings"]], [["b", "q"]])
        held = build_correction_package(self.DIMS, cur, prop, [], maps)
        self.assertFalse(held["groups"][0]["effective"])
        self.assertEqual(held["reversals"], [])
        self.assertEqual(held["postings"], [])
        self.assertEqual(
            held["effective_handover"]["books"][0]["rect"], ["a", "p"]
        )


class TestDeterminism(unittest.TestCase):
    def test_shuffled_books_and_approvals_identical_package(self):
        dims = _load("dims_chain.json")
        cur = _load("books_current.json")
        prop = _load("books_proposed.json")
        maps = _maps(dims, cur)
        approvals = ["H1", "H2", "H3", "H4", "H5"]
        base = build_correction_package(dims, cur, prop, approvals, maps)

        cur_s = copy.deepcopy(cur)
        prop_s = copy.deepcopy(prop)
        cur_s["books"].reverse()
        prop_s["books"].reverse()
        shuffled = build_correction_package(
            dims, cur_s, prop_s, list(reversed(approvals)), maps
        )
        self.assertEqual(
            json.dumps(base, sort_keys=True),
            json.dumps(shuffled, sort_keys=True),
        )


class TestCurrentMustPublish(unittest.TestCase):
    DIMS = {
        "dimensions": {
            "X": {"root": "X", "nodes": {"X": ["a"], "a": []}},
            "Y": {"root": "Y", "nodes": {"Y": ["p"], "p": []}},
        }
    }
    MAPS = {
        "X": [{"old": "a", "new": "NX"}],
        "Y": [{"old": "p", "new": "NY"}],
    }

    def test_withheld_current_book_is_rejected(self):
        bad_current = {"books": [{
            "id": "C", "rect": ["a", "p"],
            "root": {"rect": ["a", "p"], "state": "exact",
                     "value": 1, "control": 2},
        }]}
        good_proposed = {"books": [{
            "id": "C", "rect": ["a", "p"],
            "root": {"rect": ["a", "p"], "state": "exact", "value": 3},
        }]}
        h = load_handover(self.DIMS, bad_current)
        maps = load_mapping(self.MAPS, h.tree_x, h.tree_y)
        with self.assertRaises(ModelError):
            build_correction_package(
                self.DIMS, bad_current, good_proposed, ["C"], maps
            )


class TestCliApprove(unittest.TestCase):
    def run_cli(self, *args):
        script = os.path.join(ROOT, "src", "cube", "cli.py")
        return subprocess.run(
            [sys.executable, "-I", script, *args],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )

    def _arg(self, name):
        return os.path.join(APPROVAL_DIR, name)

    def test_round1_then_round2_and_withheld_via_cli(self):
        out1 = os.path.join(ROOT, "tests", "_approve_r1.json")
        out2 = os.path.join(ROOT, "tests", "_approve_r2.json")
        mid = os.path.join(ROOT, "tests", "_approve_mid.json")
        try:
            p = self.run_cli(
                "approve", "--dims", self._arg("dims_chain.json"),
                "--mapping", self._arg("mapping_chain.json"),
                "--current", self._arg("books_current.json"),
                "--proposed", self._arg("books_proposed.json"),
                "--approvals", self._arg("approvals_round1.json"),
                "--out", out1,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            pkg1 = json.load(open(out1, encoding="utf-8"))
            self.assertEqual(pkg1["format"], "correction_package/v1")
            chain = next(g for g in pkg1["groups"] if len(g["members"]) == 4)
            self.assertFalse(chain["effective"])
            json.dump(pkg1["effective_handover"], open(mid, "w", encoding="utf-8"))

            p2 = self.run_cli(
                "approve", "--dims", self._arg("dims_chain.json"),
                "--mapping", self._arg("mapping_chain.json"),
                "--current", mid,
                "--proposed", self._arg("books_proposed.json"),
                "--approvals", self._arg("approvals_full.json"),
                "--out", out2,
            )
            self.assertEqual(p2.returncode, 0, p2.stderr)
            pkg2 = json.load(open(out2, encoding="utf-8"))
            self.assertTrue(pkg2["groups"][0]["effective"])

            # Withheld variant: full approvals, chain must still not reverse.
            outw = os.path.join(ROOT, "tests", "_approve_w.json")
            try:
                pw = self.run_cli(
                    "approve", "--dims", self._arg("dims_chain.json"),
                    "--mapping", self._arg("mapping_chain.json"),
                    "--current", self._arg("books_current.json"),
                    "--proposed", self._arg("books_proposed_withheld.json"),
                    "--approvals", self._arg("approvals_full.json"),
                    "--out", outw,
                )
                self.assertEqual(pw.returncode, 0, pw.stderr)
                pkgw = json.load(open(outw, encoding="utf-8"))
                chain_w = next(g for g in pkgw["groups"]
                               if len(g["members"]) == 4)
                self.assertFalse(chain_w["effective"])
                self.assertEqual(
                    [b["book"] for b in chain_w["withheld_books"]], ["H2"]
                )
                self.assertEqual(
                    [r["book"] for r in pkgw["reversals"]], ["H5"]
                )
            finally:
                if os.path.exists(outw):
                    os.remove(outw)
        finally:
            for path in (out1, out2, mid):
                if os.path.exists(path):
                    os.remove(path)

    def test_bad_input_never_overwrites_existing_package(self):
        out = os.path.join(ROOT, "tests", "_approve_keep.json")
        bad_approvals = os.path.join(ROOT, "tests", "_bad_approvals.json")
        try:
            p = self.run_cli(
                "approve", "--dims", self._arg("dims_chain.json"),
                "--mapping", self._arg("mapping_chain.json"),
                "--current", self._arg("books_current.json"),
                "--proposed", self._arg("books_proposed.json"),
                "--approvals", self._arg("approvals_full.json"),
                "--out", out,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            original = open(out, encoding="utf-8").read()
            json.dump(["GHOST"], open(bad_approvals, "w", encoding="utf-8"))
            p2 = self.run_cli(
                "approve", "--dims", self._arg("dims_chain.json"),
                "--mapping", self._arg("mapping_chain.json"),
                "--current", self._arg("books_current.json"),
                "--proposed", self._arg("books_proposed.json"),
                "--approvals", bad_approvals, "--out", out,
            )
            self.assertNotEqual(p2.returncode, 0)
            self.assertEqual(open(out, encoding="utf-8").read(), original)
        finally:
            for path in (out, bad_approvals):
                if os.path.exists(path):
                    os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
