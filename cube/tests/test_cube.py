"""End-to-end tests for the offline sparse cube (stdlib unittest only).

Run:  python -m unittest discover -s tests -v
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

from cube.engine import (  # noqa: E402
    ALL_NOT_APPLICABLE,
    ALL_UNKNOWN,
    FULLY_EXACT,
    PARTIAL,
    build_cube,
    query_cube,
)
from cube.loader import load_handover  # noqa: E402
from cube.model import DimTree, ModelError, subtract_rect  # noqa: E402

DATA = os.path.join(ROOT, "examples", "data")


def load_json(name: str) -> dict:
    with open(os.path.join(DATA, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def make_cube(dims_name: str, books_name: str):
    handover = load_handover(load_json(dims_name), load_json(books_name))
    return build_cube(handover)


def _expand_book_rect(book, rect):
    """Descend a fully-contained source rectangle to its terminal cells.

    This walks the book's own sparse decomposition only -- it never expands
    dimension leaves and is test-side canonicalisation, not engine behaviour.
    """
    cell = book.index.get(tuple(rect))
    if cell is None:
        return [tuple(rect)]
    if cell.split is None:
        return [tuple(rect)]
    out = []
    for ch in cell.children:
        out.extend(_expand_book_rect(book, ch.rect))
    return out


def signature(result: dict, books=()) -> dict:
    """Order/granularity-independent semantic signature of a query result.

    Exact and NA regions are canonicalised to terminal-cell rectangles of the
    sparse source decomposition, so an ancestor control and its verified
    descendants produce the same signature.  The unknown partition (status,
    reasons, unhanded rectangles) is compared as produced.
    """
    by_id = {b.id: b for b in books}

    def canonical(fs):
        rects = {}
        for f in fs:
            book = by_id.get(f.source or "")
            expanded = _expand_book_rect(book, f.rect) if book else [tuple(f.rect)]
            for r in expanded:
                rects[r] = f.kinds[0]
        return rects

    return {
        "status": result["status"],
        "sum_known": result["sum_known"],
        "exact_rects": canonical(result["exact"]),
        "na_rects": canonical(result["not_applicable"]),
        "unknown_rects": {tuple(f.rect): f.kinds[0] for f in result["unknown"]},
        "unknown_reasons": {tuple(f.rect): f.reason for f in result["unknown"]},
    }


def sources(result: dict, key: str) -> set:
    return {f.source for f in result[key]}


def reasons(result: dict, key: str) -> set:
    return {f.reason for f in result[key]}


class TestGeometry(unittest.TestCase):
    def setUp(self):
        self.tx = DimTree("X", load_json("dims.json")["dimensions"]["X"])
        self.ty = DimTree("Y", load_json("dims.json")["dimensions"]["Y"])

    def test_contains_and_intersection(self):
        self.assertTrue(self.tx.contains("X", "A1"))
        self.assertTrue(self.tx.contains("A", "A"))
        self.assertFalse(self.tx.contains("A", "B1"))
        self.assertEqual(self.tx.intersection("A", "A1"), "A1")
        self.assertEqual(self.tx.intersection("A1", "A"), "A1")
        self.assertIsNone(self.tx.intersection("A", "B"))

    def test_subtract_rect_partitions_without_leaves(self):
        # (X, Y) minus (A, P1): path X>A sibling columns B,Z keep the whole Y
        # span; inside the remaining A column, path Y>P>P1 carves out siblings
        # P2, Q and R.  No leaf Cartesian product is built.
        parts = subtract_rect(self.tx, self.ty, ("X", "Y"), ("A", "P1"))
        by_x = {}
        for x, y in parts:
            by_x.setdefault(x, []).append(y)
        self.assertEqual(sorted(by_x), ["A", "B", "Z"])
        self.assertEqual(sorted(by_x["B"]), ["Y"])
        self.assertEqual(sorted(by_x["Z"]), ["Y"])
        self.assertEqual(sorted(by_x["A"]), ["P2", "Q", "R"])
        # Parts never reference undeclared nodes.
        for x, y in parts:
            self.assertIn(x, self.tx.children)
            self.assertIn(y, self.ty.children)
        # A direct leaf-pair subtraction produces only sibling bands.
        leaf_parts = subtract_rect(
            self.tx, self.ty, ("X", "Y"), ("A1", "P1")
        )
        self.assertEqual(
            sorted(leaf_parts),
            sorted([("A2", "Y"), ("A1", "P2"), ("A1", "Q"), ("A1", "R"),
                    ("B", "Y"), ("Z", "Y")]),
        )


class TestPublishing(unittest.TestCase):
    def setUp(self):
        self.cube = make_cube("dims.json", "books.json")

    def test_conflict_and_na_control_are_withheld(self):
        self.assertEqual(set(self.cube.published), {"B1", "B2"})
        self.assertEqual(set(self.cube.withheld), {"B3", "B4"})
        b3 = " ".join(self.cube.withheld["B3"])
        self.assertIn("control 20", b3)
        self.assertIn("15", b3)  # 7 + 8 = 15 != 20
        b4 = " ".join(self.cube.withheld["B4"])
        self.assertIn("not_applicable", b4)

    def test_checkable_controls_recorded(self):
        b2 = " ".join(self.cube.checks["B2"])
        self.assertIn("verified", b2)
        # B1 root control is not checkable (A2|P2 missing) -> note only.
        b1 = " ".join(self.cube.checks["B1"])
        self.assertIn("not checkable", b1)
        self.assertIn("A|P1", b1)  # the 70 control is fully verified

    def test_withheld_book_region_is_unknown_not_unhanded(self):
        res = query_cube(self.cube, ("A", "Q"))
        self.assertEqual(res["status"], ALL_UNKNOWN)
        self.assertTrue(
            any(f.source == "unpublished:B3" for f in res["unknown"])
        )
        self.assertFalse(
            any(f.source == "unhanded" for f in res["unknown"])
        )


class TestOppositeSplitOrder(unittest.TestCase):
    TARGETS = [
        ("A", "P"),      # whole responsibility rectangle
        ("A", "P1"),     # slice along the second tree
        ("A1", "P"),     # slice along the first tree
        ("X", "Y"),      # whole reporting domain (rest is unhanded)
        ("A2", "P1"),    # single terminal exact leaf pair (negative)
        ("A2", "P2"),    # missing terminal
        ("X", "P1"),     # window crossing the rectangle boundary
        ("A1", "Y"),
    ]

    def _cube_and_books(self, books_file):
        handover = load_handover(load_json("dims.json"), load_json(books_file))
        return build_cube(handover), handover.books

    def test_opposite_split_orders_give_identical_results(self):
        c1, books1 = self._cube_and_books("books_b1_only.json")
        c2, books2 = self._cube_and_books("books_b1_mirror.json")
        for target in self.TARGETS:
            with self.subTest(target=target):
                r1 = signature(query_cube(c1, target), books1)
                r2 = signature(query_cube(c2, target), books2)
                self.assertEqual(r1, r2)

    def test_mirror_whole_cell_uses_trusted_parent_amounts_once(self):
        cube, _ = self._cube_and_books("books_b1_mirror.json")
        res = query_cube(cube, ("A", "P"))
        self.assertEqual(res["status"], FULLY_EXACT)
        # The 110 root control is used directly: exactly one exact fragment,
        # no descendant sums added on top -> no double counting.
        self.assertEqual(len(res["exact"]), 1)
        self.assertEqual(res["exact"][0].value, 110)
        self.assertEqual(res["sum_known"], 110)


class TestControlSemantics(unittest.TestCase):
    def setUp(self):
        self.cube = make_cube("dims.json", "books.json")

    def test_uncheckable_parent_control_trusted_for_whole_cell(self):
        # B1 root A|P has control 110 while A2|P2 is missing.
        res = query_cube(self.cube, ("A", "P"))
        self.assertEqual(res["status"], FULLY_EXACT)
        self.assertEqual(res["sum_known"], 110)
        self.assertEqual(
            [(f.rect, f.value, f.reason) for f in res["exact"]],
            [(("A", "P"), 110, "control_total")],
        )
        self.assertEqual(res["unknown"], [])

    def test_same_control_cannot_make_a_cut_window_exact(self):
        # Window A x P1 cuts B1's root control; the checked child control 70
        # covers A x P1 fully, so this slice is exact via the child control.
        res_p1 = query_cube(self.cube, ("A", "P1"))
        self.assertEqual(res_p1["status"], FULLY_EXACT)
        self.assertEqual(res_p1["sum_known"], 70)

        # Window A1 x P cuts the root control; inside it A1|P is fully exact
        # (100 + 40), but the missing A2|P2 is not even in this window...
        # use A x P2 instead: it contains the missing terminal, so the root
        # control may not be allocated to the invisible child.
        res_p2 = query_cube(self.cube, ("A", "P2"))
        self.assertEqual(res_p2["status"], PARTIAL)
        self.assertEqual(res_p2["sum_known"], 40)
        self.assertIn("missing_terminal", reasons(res_p2, "unknown"))
        # No control fragment must appear for a cut window.
        self.assertNotIn("control_total", reasons(res_p2, "exact"))

    def test_negative_value_is_counted(self):
        res = query_cube(self.cube, ("A2", "P1"))
        self.assertEqual(res["status"], FULLY_EXACT)
        self.assertEqual(res["sum_known"], -30)

    def test_not_applicable_is_distinct_from_zero_and_unknown(self):
        # B2: B1|Q2 is not_applicable; B1|Q1 = 25.
        res_cell = query_cube(self.cube, ("B1", "Q2"))
        self.assertEqual(res_cell["status"], ALL_NOT_APPLICABLE)
        self.assertEqual(res_cell["sum_known"], 0)
        self.assertEqual(len(res_cell["not_applicable"]), 1)
        res_col = query_cube(self.cube, ("B1", "Q"))
        self.assertEqual(res_col["status"], FULLY_EXACT)
        self.assertEqual(res_col["sum_known"], 25)  # NA contributes nothing

    def test_cut_exact_terminal_is_unknown(self):
        # Custom tiny fixture: an exact terminal sits on an internal node
        # pair, and the target cuts through that exact cell.
        dims = {
            "dimensions": {
                "X": {"root": "r", "nodes": {"r": ["a", "b"], "a": [], "b": []}},
                "Y": {"root": "s", "nodes": {"s": ["u", "v"], "u": [], "v": []}},
            }
        }
        books = {
            "books": [
                {
                    "id": "K",
                    "rect": ["r", "s"],
                    "root": {
                        "rect": ["r", "s"],
                        "split": "Y",
                        "children": [
                            {"rect": ["r", "u"], "state": "exact", "value": 50},
                            {"rect": ["r", "v"], "state": "missing"},
                        ],
                    },
                }
            ]
        }
        cube = build_cube(load_handover(dims, books))
        res = query_cube(cube, ("a", "s"))
        # The window contains no fully-contained exact material: r|u is cut
        # (unknown), r|v is missing (unknown) -> everything is unknown.
        self.assertEqual(res["status"], ALL_UNKNOWN)
        self.assertIn("cut_exact_terminal", reasons(res, "unknown"))
        self.assertIn("missing_terminal", reasons(res, "unknown"))
        self.assertEqual(res["sum_known"], 0)
        # The whole exact cell read back must still be exact.
        full = query_cube(cube, ("r", "u"))
        self.assertEqual(full["status"], FULLY_EXACT)
        self.assertEqual(full["sum_known"], 50)

    def test_exact_zero_is_exact_not_missing(self):
        dims = {
            "dimensions": {
                "X": {"root": "r", "nodes": {"r": ["a", "b"], "a": [], "b": []}},
                "Y": {"root": "s", "nodes": {"s": ["u", "v"], "u": [], "v": []}},
            }
        }
        books = {
            "books": [
                {
                    "id": "K",
                    "rect": ["r", "s"],
                    "root": {
                        "rect": ["r", "s"],
                        "split": "X",
                        "children": [
                            {"rect": ["a", "s"], "state": "exact", "value": 0},
                            {"rect": ["b", "s"], "state": "exact", "value": 5},
                        ],
                    },
                }
            ]
        }
        cube = build_cube(load_handover(dims, books))
        res = query_cube(cube, ("a", "s"))
        self.assertEqual(res["status"], FULLY_EXACT)
        self.assertEqual(res["sum_known"], 0)
        self.assertEqual(res["exact"][0].reason, "exact_terminal")
        self.assertEqual(res["unknown"], [])
        # A window cutting the zero-valued exact terminal is still unknown:
        # exactness must not be extrapolated onto an invisible subregion.
        cut = query_cube(cube, ("a", "u"))
        self.assertEqual(cut["status"], ALL_UNKNOWN)
        self.assertEqual(
            {f.reason for f in cut["unknown"]}, {"cut_exact_terminal"}
        )


class TestCrossBookAndUnhanded(unittest.TestCase):
    def setUp(self):
        self.cube = make_cube("dims.json", "books.json")

    def test_unhanded_region_is_separate_from_not_applicable(self):
        res = query_cube(self.cube, ("Z", "R1"))
        self.assertEqual(res["status"], ALL_UNKNOWN)
        self.assertEqual(sources(res, "unknown"), {"unhanded"})
        self.assertEqual(res["not_applicable"], [])

    def test_whole_domain_merges_disjoint_contributions(self):
        res = query_cube(self.cube, ("X", "Y"))
        self.assertEqual(res["status"], PARTIAL)  # missing + withheld + unhanded
        # B1 whole cell 110 + B2 whole cell 40; nothing else is countable.
        self.assertEqual(res["sum_known"], 150)
        used_sources = sources(res, "exact")
        self.assertEqual(used_sources, {"B1", "B2"})
        unhanded = [f for f in res["unknown"] if f.source == "unhanded"]
        self.assertTrue(unhanded)
        # Z/R areas must appear as unhanded rectangles (node-level, no leaves).
        unhanded_rects = {tuple(f.rect) for f in unhanded}
        self.assertTrue(
            any(x in {"Z", "Z1", "Z2"} for x, _ in unhanded_rects)
        )
        self.assertTrue(
            any(y in {"R", "R1", "R2"} for _, y in unhanded_rects)
        )

    def test_no_double_counting_when_control_and_children_both_present(self):
        # B2's root control 40 fully covers B|Q; children must not be added.
        res = query_cube(self.cube, ("B", "Q"))
        self.assertEqual(res["status"], FULLY_EXACT)
        self.assertEqual(len(res["exact"]), 1)
        self.assertEqual(res["exact"][0].value, 40)

    def test_responsibility_exterior_not_counted(self):
        # B2 only claims B x Q; a window over B x (P+Q+R) must not let B2
        # leak outside its responsibility rectangle.
        res = query_cube(self.cube, ("B", "Y"))
        exact_rects = {tuple(f.rect) for f in res["exact"]}
        self.assertEqual(exact_rects, {("B", "Q")})
        self.assertEqual(res["sum_known"], 40)
        self.assertTrue(
            any(f.source == "unpublished:B4" for f in res["unknown"])
        )
        self.assertTrue(any(f.source == "unhanded" for f in res["unknown"]))


class TestLargeSparseRegion(unittest.TestCase):
    def test_huge_implicit_leaf_space_never_materialized(self):
        n = 10_000
        x_kids = [f"x{i}" for i in range(n)]
        y_kids = [f"y{i}" for i in range(n)]
        dims = {
            "dimensions": {
                "X": {"root": "RX", "nodes": {"RX": x_kids, **{k: [] for k in x_kids}}},
                "Y": {"root": "RY", "nodes": {"RY": y_kids, **{k: [] for k in y_kids}}},
            }
        }
        books = {
            "books": [
                {
                    "id": "SPARSE",
                    "rect": ["x0", "y0"],
                    "root": {"rect": ["x0", "y0"], "state": "exact", "value": 7},
                }
            ]
        }
        cube = build_cube(load_handover(dims, books))
        # 100,000,000 implicit leaf pairs, one sparse exact source cell.
        res = query_cube(cube, ("RX", "RY"))
        self.assertEqual(res["status"], PARTIAL)
        self.assertEqual(res["sum_known"], 7)
        self.assertEqual(res["stats"]["leaf_pairs_materialized"], 0)
        # The unhanded remainder is a set of node rectangles (sibling bands),
        # O(dim size) rather than the 10^8 leaf pairs: no Cartesian product.
        unhanded = [f for f in res["unknown"] if f.source == "unhanded"]
        self.assertEqual(len(unhanded), 2 * (n - 1))
        self.assertTrue(all(isinstance(f.rect, tuple) for f in unhanded))
        # Every unhanded rectangle still uses declared dimension nodes only.
        tx = cube.tree_x
        ty = cube.tree_y
        for f in unhanded:
            self.assertIn(f.rect[0], tx.children)
            self.assertIn(f.rect[1], ty.children)
        # And the exact leaf pair itself is absent from the unhanded set.
        self.assertNotIn(("x0", "y0"), {tuple(f.rect) for f in unhanded})

        exact = query_cube(cube, ("x0", "y0"))
        self.assertEqual(exact["status"], FULLY_EXACT)
        nowhere = query_cube(cube, ("x9999", "y9999"))
        self.assertEqual(nowhere["status"], ALL_UNKNOWN)
        self.assertEqual(sources(nowhere, "unknown"), {"unhanded"})


class TestLoaderRejections(unittest.TestCase):
    def setUp(self):
        self.dims = load_json("dims.json")
        self.books = load_json("books.json")

    def _expect(self, dims=None, books=None):
        with self.assertRaises(ModelError):
            build_cube(load_handover(dims or self.dims, books or self.books))

    def test_split_must_enumerate_all_direct_children(self):
        bad = copy.deepcopy(self.books)
        del bad["books"][0]["root"]["children"][1]  # drop P2 branch
        self._expect(books=bad)

    def test_split_wrong_dimension_fixed_side(self):
        bad = copy.deepcopy(self.books)
        # B1 root splits Y, so children must keep x=A; tamper one.
        bad["books"][0]["root"]["children"][0]["rect"] = ["A1", "P1"]
        self._expect(books=bad)

    def test_overlapping_responsibility_rectangles_rejected(self):
        bad = copy.deepcopy(self.books)
        bad["books"][1]["rect"] = ["A", "P"]  # collides with B1
        bad["books"][1]["root"]["rect"] = ["A", "P"]
        self._expect(books=bad)

    def test_unknown_node_rejected(self):
        bad = copy.deepcopy(self.books)
        bad["books"][0]["rect"] = ["NOPE", "P"]
        self._expect(books=bad)

    def test_exact_requires_integer(self):
        bad = copy.deepcopy(self.books)
        bad["books"][0]["root"]["children"][0]["children"][0]["value"] = 1.5
        self._expect(books=bad)

    def test_cyclic_dimension_rejected(self):
        bad = copy.deepcopy(self.dims)
        bad["dimensions"]["X"]["nodes"]["A1"] = ["A"]
        self._expect(books=bad)

    def test_mixed_state_and_children_rejected(self):
        bad = copy.deepcopy(self.books)
        bad["books"][0]["root"]["state"] = "exact"
        bad["books"][0]["root"]["value"] = 1
        self._expect(books=bad)


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        # Invoke the script by path so it works under -I (the script's own
        # directory is put on sys.path automatically; PYTHONPATH is ignored).
        script = os.path.join(ROOT, "src", "cube", "cli.py")
        return subprocess.run(
            [sys.executable, "-I", script, *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )

    @property
    def paths(self):
        return [
            "--dims", os.path.join(DATA, "dims.json"),
            "--books", os.path.join(DATA, "books.json"),
        ]

    def test_build_and_query_json(self):
        p = self.run_cli("build", *self.paths)
        self.assertEqual(p.returncode, 0, p.stderr)
        report = json.loads(p.stdout)
        self.assertEqual(sorted(report["published_books"]), ["B1", "B2"])
        self.assertIn("B3", report["withheld_books"])

        q = self.run_cli("query", *self.paths, "--target", "A", "P", "--json")
        self.assertEqual(q.returncode, 0, q.stderr)
        payload = json.loads(q.stdout)
        self.assertEqual(payload["status"], FULLY_EXACT)
        self.assertEqual(payload["sum_known"], 110)

    def test_batch_targets_file(self):
        targets = os.path.join(DATA, "targets.txt")
        out = os.path.join(ROOT, "tests", "_batch_out.json")
        try:
            p = self.run_cli(
                "batch", *self.paths, "--targets", targets, "--out", out
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(out, encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertEqual(len(payload["results"]), 6)
            statuses = {tuple(r["target"]): r["status"] for r in payload["results"]}
            self.assertEqual(statuses[("A", "P")], FULLY_EXACT)
            self.assertEqual(statuses[("Z", "R1")], ALL_UNKNOWN)
        finally:
            if os.path.exists(out):
                os.remove(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
