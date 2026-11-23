"""Recode tests: mapping validation, batch semantics, determinism, scale.

Run:  python3 -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cube.engine import build_cube  # noqa: E402
from cube.loader import load_handover  # noqa: E402
from cube.mapping import load_mapping  # noqa: E402
from cube.model import ModelError  # noqa: E402
from cube.recode import convert_handover, reconcile  # noqa: E402

DATA = os.path.join(ROOT, "examples", "data")


def load_json(name: str) -> dict:
    with open(os.path.join(DATA, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_text(name: str) -> dict:
    with open(os.path.join(DATA, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def run_recode(dims, books, mapping):
    handover = load_handover(dims, books)
    cube = build_cube(handover)
    maps = load_mapping(mapping, handover.tree_x, handover.tree_y)
    batch = convert_handover(handover, cube, maps)
    return batch, cube


def strip_stats(batch: dict) -> dict:
    out = copy.deepcopy(batch)
    out.pop("stats", None)
    return out


class TestMappingValidation(unittest.TestCase):
    def setUp(self):
        self.dims = load_json("dims.json")
        self.books = load_json("books.json")
        self.good = load_json("mapping_sample.json")

    def _expect_fail(self, mapping):
        with self.assertRaises(ModelError):
            run_recode(self.dims, self.books, mapping)

    def test_unknown_node_fails(self):
        bad = copy.deepcopy(self.good)
        bad["X"].append({"old": "NOPE", "new": "N9"})
        self._expect_fail(bad)

    def test_duplicate_rule_same_axis_fails(self):
        bad = copy.deepcopy(self.good)
        bad["X"].append({"old": "A", "new": "N-OTHER"})
        self._expect_fail(bad)

    def test_empty_new_code_fails(self):
        bad = copy.deepcopy(self.good)
        bad["X"][0]["new"] = "   "
        self._expect_fail(bad)

    def test_unmapped_leaf_fails(self):
        # Rules only on the root would normally cover everything; drop the
        # root rule and map just one child's subtree, leaving Z leaves bare.
        bad = {
            "X": [{"old": "X", "new": "N-WEST"}, {"old": "A", "new": "N-A"}],
            "Y": [{"old": "Y", "new": "M-ALL"}],
        }
        # B has no deeper rule -> inherits root; Z does too.  Now remove the
        # root rule and keep only A, so B/Z leaves are unmapped.
        bad["X"] = [{"old": "A", "new": "N-A"}]
        self._expect_fail(bad)

    def test_rule_precedence_and_shared_codes(self):
        handover = load_handover(self.dims, self.books)
        maps = load_mapping(self.good, handover.tree_x, handover.tree_y)
        xmap = maps["X"]
        # Deeper rule A1 overrides A; A2 inherits A's rule.
        self.assertEqual(xmap.merge_code("A1"), "N-ALPHA")
        self.assertEqual(xmap.merge_code("A2"), "N-ALPHA")
        # No rule deeper under A1 in this sample: exactly one code.
        self.assertEqual(xmap.merge_code("A"), "N-ALPHA")
        # Root X spans B/Z (root rule) and A (deeper rule) -> ambiguous.
        self.assertIsNone(xmap.merge_code("X"))
        # Two different old leaves may share one new code.
        self.assertEqual(xmap.merge_code("B1"), "N-WEST")
        self.assertEqual(xmap.merge_code("Z2"), "N-WEST")


class TestControlAbsorbed(unittest.TestCase):
    def setUp(self):
        self.batch, self.cube = run_recode(
            load_json("dims.json"), load_json("books.json"),
            load_json("mapping_sample.json"),
        )

    def _by_book(self, key):
        return [r for r in self.batch[key] if r["book"] == "B1"]

    def test_control_landing_on_one_pair_is_booked_whole(self):
        ingested = [r for r in self.batch["ingested"] if r["book"] == "B1"]
        # A|P1 control 70 lands on one new pair; its -30/100 details must not
        # also be emitted.
        ctrl = [r for r in ingested if r["rect"] == ["A", "P1"]]
        self.assertEqual(len(ctrl), 1)
        self.assertEqual(ctrl[0]["kind"], "control_total")
        self.assertEqual(ctrl[0]["amount"], 70)
        self.assertEqual(ctrl[0]["new_pair"], {"X": "N-ALPHA",
                                               "Y": "M-PRIMARY-P1"})
        self.assertFalse(any(r["rect"] == ["A1", "P1"] for r in ingested))
        self.assertFalse(any(r["rect"] == ["A2", "P1"] for r in ingested))

    def test_root_control_cross_pair_keeps_difference_even_when_zero(self):
        parked = self._by_book("parked")
        diff = [r for r in parked if r["kind"] == "control_difference"]
        self.assertEqual(len(diff), 1)
        rec = diff[0]
        self.assertEqual(rec["rect"], ["A", "P"])
        self.assertEqual(rec["control"], 110)
        self.assertEqual(rec["deducted"], 110)
        self.assertEqual(rec["deducted_controls"], [70])
        self.assertEqual(rec["deducted_exact"], 40)
        self.assertEqual(rec["amount"], 0)
        self.assertTrue(rec["outside_missing"])
        self.assertIsNone(rec["new_pair"])

    def test_unabsorbed_missing_keeps_coordinates_without_zero(self):
        missing = [r for r in self.batch["parked"] if r["kind"] == "missing"]
        self.assertEqual([r["rect"] for r in missing], [["A2", "P2"]])
        self.assertNotIn("amount", missing[0])
        self.assertIsNone(missing[0]["new_pair"])

    def test_withheld_books_are_ids_and_reasons_only(self):
        self.assertEqual(
            [b["book"] for b in self.batch["withheld_books"]], ["B3", "B4"]
        )
        for entry in self.batch["withheld_books"]:
            self.assertEqual(set(entry), {"book", "reasons"})
            self.assertTrue(entry["reasons"])

    def test_reconciles_with_old_ledger(self):
        check = reconcile(self.cube, self.batch)
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["source_total"], 150)


class TestExceptionMappingSplitsControl(unittest.TestCase):
    """One exception rule turns the formerly whole control into
    'confirmed lower-level postings + control difference'."""

    def setUp(self):
        self.sample, cube_a = run_recode(
            load_json("dims.json"), load_json("books.json"),
            load_json("mapping_sample.json"),
        )
        self.exception, cube_b = run_recode(
            load_json("dims.json"), load_json("books.json"),
            load_json("mapping_exception.json"),
        )
        self.cube = cube_a
        self.cube_b = cube_b

    def test_same_inputs_differ_only_by_the_exception(self):
        self.assertEqual(self.sample["published_books"],
                         self.exception["published_books"])
        self.assertEqual(self.sample["withheld_books"],
                         self.exception["withheld_books"])

    def test_exception_splits_control_into_lower_postings_and_difference(self):
        b1_ing = [r for r in self.exception["ingested"] if r["book"] == "B1"]
        rects = {tuple(r["rect"]): r for r in b1_ing}
        # Lower-level exact terminals now land on confirmed pairs.
        self.assertEqual(rects[("A1", "P1")]["new_pair"],
                         {"X": "N-BETA", "Y": "M-PRIMARY-P1"})
        self.assertEqual(rects[("A2", "P1")]["new_pair"],
                         {"X": "N-ALPHA", "Y": "M-PRIMARY-P1"})
        self.assertEqual(rects[("A1", "P2")]["amount"], 40)
        # No lower-level control survived whole: A|P1 now spans two pairs.
        self.assertNotIn(("A", "P1"), rects)

        diff = [r for r in self.exception["parked"]
                if r["kind"] == "control_difference"]
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["rect"], ["A", "P"])
        self.assertEqual(diff[0]["control"], 110)
        # Only first-level money is deducted: the -30 + 100 exact terminals
        # are first-level here (no lower control remains intact in scope).
        self.assertEqual(diff[0]["deducted"], 110)
        self.assertEqual(diff[0]["amount"], 0)

    def test_exception_money_is_neither_lost_nor_double_counted(self):
        for batch, cube in ((self.sample, self.cube),
                            (self.exception, self.cube_b)):
            check = reconcile(cube, batch)
            self.assertTrue(check["ok"], check)
            self.assertEqual(check["delta"], 0)
            self.assertEqual(check["source_total"], 150)


class TestExceptionSplitsVerifiedControl(unittest.TestCase):
    """Same book, one exception rule: a control that was booked whole becomes
    'confirmed lower-level amount + control difference'."""

    DIMS = {
        "dimensions": {
            "X": {"root": "X", "nodes": {"X": ["A", "B"],
                                         "A": ["a1", "a2"], "a1": [], "a2": [],
                                         "B": []}},
            "Y": {"root": "Y", "nodes": {"Y": ["P", "Q"],
                                         "P": ["p1", "p2"], "p1": [], "p2": [],
                                         "Q": []}},
        }
    }

    def _book(self):
        # Control 100 trusted over A|P (one child missing, so the builder
        # publishes it without verification).  a1|P is exact 40.
        return {"books": [{
            "id": "E", "rect": ["X", "Y"],
            "root": {"rect": ["X", "Y"], "split": "X", "children": [
                {"rect": ["A", "Y"], "split": "Y", "control": 100, "children": [
                    {"rect": ["A", "P"], "split": "X", "children": [
                        {"rect": ["a1", "P"], "state": "exact", "value": 40},
                        {"rect": ["a2", "P"], "state": "missing"},
                    ]},
                    {"rect": ["A", "Q"], "state": "not_applicable"},
                ]},
                {"rect": ["B", "Y"], "state": "not_applicable"},
            ]},
        }]}

    def _base(self):
        return {
            "X": [{"old": "a1", "new": "N1"}, {"old": "a2", "new": "N1"},
                  {"old": "B", "new": "N2"}],
            "Y": [{"old": "p1", "new": "M1"}, {"old": "p2", "new": "M1"},
                  {"old": "Q", "new": "M2"}],
        }

    def _exception(self):
        m = self._base()
        m["X"] = [{"old": "a1", "new": "N1A"}, {"old": "a2", "new": "N1B"},
                  {"old": "B", "new": "N2"}]
        return m

    def test_base_mapping_books_control_whole(self):
        batch, cube = run_recode(self.DIMS, self._book(), self._base())
        self.assertEqual(len(batch["ingested"]), 1)
        rec = batch["ingested"][0]
        self.assertEqual(rec["rect"], ["A", "Y"])
        self.assertEqual(rec["kind"], "control_total")
        self.assertEqual(rec["amount"], 100)
        # The missing child is absorbed: no missing or difference record.
        self.assertEqual(batch["parked"], [])
        self.assertTrue(reconcile(cube, batch)["ok"])

    def test_exception_splits_into_confirmed_lower_amount_and_difference(self):
        batch, cube = run_recode(self.DIMS, self._book(), self._exception())
        ing = batch["ingested"]
        self.assertEqual(len(ing), 1)
        self.assertEqual(ing[0]["rect"], ["a1", "P"])
        self.assertEqual(ing[0]["amount"], 40)
        self.assertEqual(ing[0]["new_pair"], {"X": "N1A", "Y": "M1"})

        diff = [r for r in batch["parked"]
                if r["kind"] == "control_difference"]
        self.assertEqual(len(diff), 1)
        rec = diff[0]
        self.assertEqual(rec["rect"], ["A", "Y"])
        self.assertEqual(rec["control"], 100)
        self.assertEqual(rec["deducted"], 40)   # confirmed lower amount
        self.assertEqual(rec["amount"], 60)     # signed difference
        self.assertTrue(rec["outside_missing"])
        self.assertIsNone(rec["new_pair"])
        # The missing cell keeps its coordinates; the difference does not
        # fill it in.
        missing = [r for r in batch["parked"] if r["kind"] == "missing"]
        self.assertEqual([r["rect"] for r in missing], [["a2", "P"]])
        self.assertNotIn("amount", missing[0])

        check = reconcile(cube, batch)
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["source_total"], 100)
        self.assertEqual(check["batch_total"], 40 + 60)


class TestNestedControls(unittest.TestCase):
    """Upper control deducts the first-level control once; the lower control
    deducts its own details.  Parked lower money is still deducted, and a
    lower control is not deducted together with its details."""

    DIMS = {
        "dimensions": {
            "X": {"root": "X", "nodes": {
                "X": ["U", "W"], "U": ["u1", "U2"], "u1": [],
                "U2": ["u2a", "u2b"], "u2a": [], "u2b": [],
                "W": ["w1", "w2"], "w1": [], "w2": [],
            }},
            "Y": {"root": "Y", "nodes": {
                "Y": ["S", "T"], "S": ["s1", "s2"], "s1": [], "s2": [],
                "T": ["t1", "t2"], "t1": [], "t2": [],
            }},
        }
    }

    def _book(self):
        # Upper control 105 on X|Y.  U|Y carries lower control 60; W|Y holds
        # two plain exact terminals.  U2's terminals are coarse: U2 spans two
        # X codes (u2a / u2b), so those amounts are parked -- yet the lower
        # control must still deduct them.
        return {
            "books": [{
                "id": "N", "rect": ["X", "Y"],
                "root": {
                    "rect": ["X", "Y"], "split": "X", "control": 105,
                    "children": [
                        {"rect": ["U", "Y"], "split": "Y", "control": 60,
                         "children": [
                             {"rect": ["U", "S"], "split": "X", "children": [
                                 {"rect": ["u1", "S"], "state": "exact", "value": 10},
                                 {"rect": ["U2", "S"], "state": "exact", "value": 30},
                             ]},
                             {"rect": ["U", "T"], "split": "X", "children": [
                                 {"rect": ["u1", "T"], "state": "missing"},
                                 {"rect": ["U2", "T"], "state": "exact", "value": 20},
                             ]},
                         ]},
                        {"rect": ["W", "Y"], "split": "Y", "children": [
                            {"rect": ["W", "S"], "state": "exact", "value": 25},
                            {"rect": ["W", "T"], "state": "exact", "value": 15},
                        ]},
                    ],
                },
            }]
        }

    def _mapping(self):
        # u1 -> P1, u2a/u2b -> two different codes (U2 is coarse), W -> PW.
        # Y collapses to one code so only X drives ambiguity.
        return {
            "X": [
                {"old": "X", "new": "NX"},
                {"old": "u1", "new": "PX1"},
                {"old": "u2a", "new": "PX2"},
                {"old": "u2b", "new": "PX3"},
                {"old": "W", "new": "PW"},
            ],
            "Y": [{"old": "Y", "new": "MY"}],
        }

    def test_upper_control_deducts_lower_control_once(self):
        batch, cube = run_recode(self.DIMS, self._book(), self._mapping())
        self.assertEqual(batch["withheld_books"], [])
        diffs = {tuple(r["rect"]): r for r in batch["parked"]
                 if r["kind"] == "control_difference"}
        upper = diffs[("X", "Y")]
        # Upper deducts the lower control total 60 once, plus the first-level
        # exact terminals 25 + 15 = 40 that sit outside that control.
        self.assertEqual(upper["control"], 105)
        self.assertEqual(upper["deducted_controls"], [60])
        self.assertEqual(upper["deducted_exact"], 40)
        self.assertEqual(upper["deducted"], 100)
        self.assertEqual(upper["amount"], 5)

        lower = diffs[("U", "Y")]
        self.assertEqual(lower["control"], 60)
        # Lower scope: 10 + 30 + 20 = 60, plus one missing (u1|T) outside.
        self.assertEqual(lower["deducted"], 60)
        self.assertEqual(lower["amount"], 0)
        self.assertTrue(lower["outside_missing"])
        self.assertTrue(reconcile(cube, batch)["ok"], reconcile(cube, batch))

    def test_parked_lower_money_is_still_deducted(self):
        batch, cube = run_recode(self.DIMS, self._book(), self._mapping())
        parked_exact = {tuple(r["rect"]) for r in batch["parked"]
                        if r["kind"] == "exact_terminal"}
        # U2 terminals are parked whole because U2 spans PX2 and PX3.
        self.assertEqual(parked_exact, {("U2", "S"), ("U2", "T")})
        diffs = {tuple(r["rect"]): r for r in batch["parked"]
                 if r["kind"] == "control_difference"}
        # The 30 + 20 stays in the lower control's deduction even though it
        # could not be ingested; the upper control deducts the 60 once.
        self.assertEqual(diffs[("U", "Y")]["deducted"], 60)
        self.assertEqual(diffs[("X", "Y")]["deducted"], 100)
        check = reconcile(cube, batch)
        self.assertTrue(check["ok"], check)
        # 30 and 20 appear once as parked amounts, never twice through the
        # difference records.
        self.assertEqual(check["delta"], 0)


class TestCoarseTerminalAndZero(unittest.TestCase):
    DIMS = {
        "dimensions": {
            "X": {"root": "X", "nodes": {"X": ["A", "B"], "A": ["a1", "a2"],
                                         "a1": [], "a2": [], "B": []}},
            "Y": {"root": "Y", "nodes": {"Y": ["P", "Q"], "P": ["p1", "p2"],
                                         "p1": [], "p2": [], "Q": []}},
        }
    }

    def _book(self, coarse_value, fine_value):
        # X|Y splits into A|Y (coarse terminal) and B|Y (further split into
        # B|P and B|Q so the fine terminal sits on a single new pair).
        return {"books": [{
            "id": "C", "rect": ["X", "Y"],
            "root": {"rect": ["X", "Y"], "split": "X", "children": [
                {"rect": ["A", "Y"], "state": "exact", "value": coarse_value},
                {"rect": ["B", "Y"], "split": "Y", "children": [
                    {"rect": ["B", "P"], "state": "not_applicable"},
                    {"rect": ["B", "Q"], "state": "exact", "value": fine_value},
                ]},
            ]},
        }]}

    def test_coarse_terminal_spanning_pairs_is_parked_whole(self):
        books = self._book(50, 5)
        # a1 -> N1, a2 -> N2: the coarse A|Y terminal spans both.  B|Q needs
        # a Y rule for Q, but both p leaves map to the same code.
        mapping = {
            "X": [{"old": "a1", "new": "N1"}, {"old": "a2", "new": "N2"},
                  {"old": "B", "new": "N3"}],
            "Y": [{"old": "p1", "new": "M1"}, {"old": "p2", "new": "M1"},
                  {"old": "Q", "new": "M3"}],
        }
        batch, cube = run_recode(self.DIMS, books, mapping)
        parked = [r for r in batch["parked"] if r["kind"] == "exact_terminal"]
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]["rect"], ["A", "Y"])
        self.assertEqual(parked[0]["amount"], 50)   # whole, never split
        self.assertEqual(parked[0]["reason"], "multiple_new_code_pairs")
        self.assertIsNone(parked[0]["new_pair"])
        # The unambiguous coarse terminal is booked whole.
        ing = [r for r in batch["ingested"] if r["kind"] == "exact_terminal"]
        self.assertEqual([(r["rect"], r["amount"]) for r in ing],
                         [(["B", "Q"], 5)])
        self.assertTrue(reconcile(cube, batch)["ok"])

    def test_zero_amount_follows_the_same_rule(self):
        books = self._book(0, 0)
        mapping = {
            "X": [{"old": "a1", "new": "N1"}, {"old": "a2", "new": "N2"},
                  {"old": "B", "new": "N1"}],
            "Y": [{"old": "p1", "new": "M1"}, {"old": "p2", "new": "M1"},
                  {"old": "Q", "new": "M1"}],
        }
        batch, cube = run_recode(self.DIMS, books, mapping)
        parked = [r for r in batch["parked"] if r["kind"] == "exact_terminal"]
        self.assertEqual([(r["rect"], r["amount"]) for r in parked],
                         [(["A", "Y"], 0)])
        # B|Q is zero but unambiguous: ingested, not parked.
        ing = [r for r in batch["ingested"] if r["rect"] == ["B", "Q"]]
        self.assertEqual(ing[0]["amount"], 0)
        self.assertEqual(ing[0]["new_pair"], {"X": "N1", "Y": "M1"})


class TestNotApplicableScope(unittest.TestCase):
    DIMS = {
        "dimensions": {
            "X": {"root": "X", "nodes": {"X": ["A", "B"], "A": [], "B": []}},
            "Y": {"root": "Y", "nodes": {"Y": ["P", "Q"], "P": [], "Q": []}},
        }
    }

    def test_not_applicable_parts_do_not_break_a_single_pair(self):
        books = {"books": [{
            "id": "NA", "rect": ["X", "Y"],
            "root": {"rect": ["X", "Y"], "split": "Y", "children": [
                {"rect": ["X", "P"], "state": "not_applicable"},
                {"rect": ["X", "Q"], "split": "X", "control": 12,
                 "children": [
                     {"rect": ["A", "Q"], "state": "exact", "value": 7},
                     {"rect": ["B", "Q"], "state": "exact", "value": 5},
                 ]},
            ]},
        }]}
        mapping = {
            "X": [{"old": "X", "new": "N1"}],
            "Y": [{"old": "P", "new": "M1"}, {"old": "Q", "new": "M2"}],
        }
        batch, cube = run_recode(self.DIMS, books, mapping)
        # X|Q's applicable scope is only M2; the NA half is ignored, so the
        # control 12 is booked directly even though X|Y spans two Y codes.
        ing = batch["ingested"]
        self.assertEqual(len(ing), 1)
        self.assertEqual(ing[0]["rect"], ["X", "Q"])
        self.assertEqual(ing[0]["amount"], 12)
        self.assertEqual(ing[0]["new_pair"], {"X": "N1", "Y": "M2"})
        self.assertEqual(batch["parked"], [])
        self.assertTrue(reconcile(cube, batch)["ok"])


class TestDeterminism(unittest.TestCase):
    def setUp(self):
        self.dims = load_json("dims.json")
        self.books = load_json("books.json")
        self.mapping = load_json("mapping_exception.json")

    def test_shuffled_rules_books_and_siblings_are_identical(self):
        base, _ = run_recode(self.dims, self.books, self.mapping)
        base = strip_stats(base)

        shuffled_map = {
            "X": list(reversed(self.mapping["X"])),
            "Y": list(reversed(self.mapping["Y"])),
        }
        shuffled_books = copy.deepcopy(self.books)
        shuffled_books["books"] = list(reversed(shuffled_books["books"]))
        for book in shuffled_books["books"]:
            if "children" in book["root"]:
                book["root"]["children"] = list(
                    reversed(book["root"]["children"])
                )
        got, _ = run_recode(self.dims, shuffled_books, shuffled_map)
        self.assertEqual(base, strip_stats(got))

    def test_rule_order_within_a_node_does_not_matter(self):
        # Different nodes' rules in a different order: identical batch.
        reordered = {"X": [], "Y": []}
        for axis in ("X", "Y"):
            deeper = [r for r in self.mapping[axis]
                      if r["old"] not in ("X", "Y")]
            shallow = [r for r in self.mapping[axis]
                       if r["old"] in ("X", "Y")]
            reordered[axis] = deeper + shallow
        a, _ = run_recode(self.dims, self.books, self.mapping)
        b, _ = run_recode(self.dims, self.books, reordered)
        self.assertEqual(strip_stats(a), strip_stats(b))


class TestLargeSparseScenario(unittest.TestCase):
    """Few source cells, tens of thousands of leaves per axis.

    Only dimension nodes are enumerated for the mapping check; old leaf pairs
    and potential new-code pairs are never materialised.
    """

    N = 20_000

    def test_tens_of_thousands_of_leaves_few_cells(self):
        half = self.N // 2
        xs = [f"x{i}" for i in range(self.N)]
        ys = [f"y{i}" for i in range(self.N)]
        x_gx, x_hx = xs[:half], xs[half:]
        y_gy, y_hy = ys[:half], ys[half:]
        dims = {
            "dimensions": {
                # Two subtrees, each with half the leaves, plus two lone
                # leaves: tens of thousands of leaves, still a handful of
                # source cells in the book below.
                "X": {"root": "RX", "nodes": {
                    "RX": ["gx", "hx", "xl0", "xl1"],
                    "gx": x_gx, "hx": x_hx,
                    "xl0": [], "xl1": [],
                    **{k: [] for k in xs},
                }},
                "Y": {"root": "RY", "nodes": {
                    "RY": ["gy", "hy", "yl0", "yl1"],
                    "gy": y_gy, "hy": y_hy,
                    "yl0": [], "yl1": [],
                    **{k: [] for k in ys},
                }},
            }
        }
        # gx/gy subtrees each hold 10k leaves; a few source cells only.
        books = {"books": [{
            "id": "SPARSE",
            "rect": ["gx", "gy"],
            "root": {"rect": ["gx", "gy"], "split": "X", "control": 7,
                     "children": [
                         {"rect": [x_gx[0], "gy"], "state": "exact",
                          "value": 3},
                         {"rect": [x_gx[1], "gy"], "state": "missing"},
                     ] + [
                         {"rect": [x, "gy"], "state": "not_applicable"}
                         for x in x_gx[2:]
                     ]},
        }, {
            # A second book whose sparse exact cells land on distinct pairs,
            # so its coarse ancestors still resolve node-by-node.
            "id": "SPARSE2",
            "rect": ["xl0", "yl0"],
            "root": {"rect": ["xl0", "yl0"], "state": "exact", "value": 11},
        }]}
        mapping = {
            "X": [{"old": "RX", "new": "NX"}],
            "Y": [{"old": "RY", "new": "NY"}],
        }
        handover = load_handover(dims, books)
        cube = build_cube(handover)
        maps = load_mapping(mapping, handover.tree_x, handover.tree_y)
        batch = convert_handover(handover, cube, maps)
        self.assertEqual(batch["stats"]["leaf_pairs_materialized"], 0)
        self.assertEqual(len(batch["ingested"]), 2)
        self.assertEqual(len(batch["parked"]), 0)
        # The control's applicable scope includes the missing cell, all on
        # the single new pair, so the control is booked whole and the missing
        # below is absorbed (no separate missing record).
        by_rect = {tuple(r["rect"]): r for r in batch["ingested"]}
        self.assertEqual(by_rect[("gx", "gy")]["amount"], 7)
        self.assertEqual(by_rect[("xl0", "yl0")]["amount"], 11)
        self.assertTrue(reconcile(cube, batch)["ok"])
        # Mapping validation touched dimension nodes, not leaf pairs: the
        # axis has N leaves + 5 internal nodes, far below the leaf-pair
        # product, and no terminal rectangles were ever enumerated.
        self.assertEqual(maps["X"].tree.node_count(), self.N + 5)


class TestCliRecode(unittest.TestCase):
    def run_cli(self, *args):
        script = os.path.join(ROOT, "src", "cube", "cli.py")
        return subprocess.run(
            [sys.executable, "-I", script, *args],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )

    def test_recode_writes_batch_and_refuses_to_overwrite_on_bad_mapping(self):
        out = os.path.join(ROOT, "tests", "_recode_out.json")
        try:
            p = self.run_cli(
                "recode", "--dims", os.path.join(DATA, "dims.json"),
                "--books", os.path.join(DATA, "books.json"),
                "--mapping", os.path.join(DATA, "mapping_sample.json"),
                "--out", out,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(out, encoding="utf-8") as fh:
                first = fh.read()
            payload = json.loads(first)
            self.assertEqual(payload["format"], "recode_batch/v1")
            self.assertEqual(payload["withheld_books"][0]["book"], "B3")

            bad = os.path.join(ROOT, "tests", "_bad_mapping.json")
            with open(bad, "w", encoding="utf-8") as fh:
                json.dump({"X": [{"old": "NOPE", "new": "N1"}],
                           "Y": [{"old": "Y", "new": "M1"}]}, fh)
            try:
                p2 = self.run_cli(
                    "recode", "--dims", os.path.join(DATA, "dims.json"),
                    "--books", os.path.join(DATA, "books.json"),
                    "--mapping", bad, "--out", out,
                )
                self.assertNotEqual(p2.returncode, 0)
                with open(out, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), first)  # never overwritten
            finally:
                os.remove(bad)
        finally:
            if os.path.exists(out):
                os.remove(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
