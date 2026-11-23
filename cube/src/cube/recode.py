"""Offline recode: old handover -> importable new-code JSON batch.

The old handover files cannot be resent, so this module converts the already
validated material to the new dimension codes.  It leaves the original files
untouched and adds no new-code query API.

Batch shape (version ``recode_batch/v1``)::

    {
      "ingested": [ {book, rect, kind, amount, new_pair, ...}, ...],
      "parked":   [ {book, rect, kind, amount?, reason, ...}, ...],
      "withheld_books": [ {book, reasons}, ...],
      "totals": {...}, "stats": {...}
    }

Rules implemented (see README's acceptance scenarios)
----------------------------------------------------
* Only books published by the existing builder participate.  A withheld book
  contributes an id + reasons entry only: no amounts, no missing records.
* The *applicable* scope of an amount is judged on its applicable leaf pairs:
  not-applicable parts are ignored; missing parts still count as applicable.
* An exact terminal is booked whole when its applicable scope lands on one
  new (x, y) pair, otherwise it is parked whole.  There is no proportional
  split and no "first matching pair" fallback.  Zero is handled identically.
* A control total whose applicable scope lands on one new pair is booked
  directly and its children are not visited at all, so no duplicate amount
  or missing record is generated below it.
* Across pairs, the lower levels are converted normally and the control is
  reconciled here: control - (first-level descendant control totals) - (exact
  terminals not covered by those controls).  The walk stops at every control,
  so a descendant control is deducted once as a total and its details are
  never deducted twice.  A non-zero residual, or any missing terminal outside
  the deduction scope, parks a signed difference record on the control cell.
  The record is kept when the residual is zero if missing material lies
  outside.  A difference never assigns a value to a missing cell.  Internal
  cells without a control only organise their children and own no amount.
* Every record names the old book, the old cell rectangle, and whether its
  source was an exact terminal, a control total, a control difference, or a
  missing cell.  A difference record also carries the control total and the
  deducted total.  Ingested records carry the full new code pair; parked
  records never guess a target.
* Records are never merged, even when sources happen to share a new pair.
  Output is sorted by a geometric key (old tree order), so shuffling mapping
  rules, books, or sibling cell write order yields identical content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .engine import Cube
from .loader import EXACT, MISSING, NOT_APPLICABLE, Book, Cell, Handover
from .mapping import CodeMap
from .model import DimTree

KIND_EXACT = "exact_terminal"
KIND_CONTROL = "control_total"
KIND_DIFF = "control_difference"
KIND_MISSING = "missing"

WHY_AMBIGUOUS = "multiple_new_code_pairs"
WHY_MISSING = "missing_not_absorbed"
WHY_RESIDUAL = "control_residual"

Pair = Tuple[str, str]


def _scope_pair(nodes_x: set, nodes_y: set, maps: Dict[str, CodeMap]) -> Optional[Pair]:
    """New pair for a union of old *leaf* regions, or None if ambiguous.

    A region is a set of old leaves; its scope is the region's X leaves times
    its Y leaves.  For a single rectangle that is just both subtrees.  For a
    control, the applicable region is a union of terminal rectangles, so the
    union of each axis' leaf codes is taken before pairing.  Landing on one
    pair requires each axis' region to resolve to exactly one new code (a new
    pair means the whole axis, not a range sharing a parent).
    """
    x_codes: set = set()
    y_codes: set = set()
    for node in nodes_x:
        x_codes |= maps["X"].node_codes(node)
    for node in nodes_y:
        y_codes |= maps["Y"].node_codes(node)
    if len(x_codes) == 1 and len(y_codes) == 1:
        return (next(iter(x_codes)), next(iter(y_codes)))
    return None


def _pair(maps: Dict[str, CodeMap], x: str, y: str) -> Optional[Pair]:
    """New pair for the leaf pairs of rectangle ``x`` x ``y``, else None."""
    return _scope_pair({x}, {y}, maps)


def _applicable_scope(
    root: Cell, maps: Dict[str, CodeMap]
) -> Tuple[str, Optional[Pair]]:
    """Scope of an amount on ``root`` over applicable leaf pairs only.

    Returns ``("unique", pair)`` when every applicable terminal below resolves
    to the same new pair, ``("multiple", None)`` otherwise, and
    ``("empty", None)`` when there is no applicable terminal at all.
    """
    pairs: set = set()
    has_applicable = False
    x_nodes: set = set()
    y_nodes: set = set()
    stack = [root]
    while stack:
        cell = stack.pop()
        if cell.split is not None:
            stack.extend(cell.children)
            continue
        if cell.state == NOT_APPLICABLE:
            continue
        has_applicable = True
        pair = _pair(maps, cell.rect[0], cell.rect[1])
        if pair is None:
            # A single applicable terminal already spans several pairs: no
            # union of the rest can bring it back to one.
            return "multiple", None
        pairs.add(pair)
        x_nodes.add(cell.rect[0])
        y_nodes.add(cell.rect[1])
    if not has_applicable:
        return "empty", None
    if len(pairs) > 1:
        return "multiple", None
    pair = _scope_pair(x_nodes, y_nodes, maps)
    if pair is None:
        return "multiple", None
    return "unique", pair


def _deduction_scope(control: Cell) -> Tuple[List[Cell], int, List[Cell]]:
    """First-level deductions below a cross-pair control.

    Returns ``(first-level control cells, exact sum outside them, missing
    cells outside them)``.  The walk stops at every control met, so a
    descendant control total is deducted once and its details never count
    again here.  The control's own total is the starting point, not a
    deduction.
    """
    controls: List[Cell] = []
    exact_sum = 0
    missing: List[Cell] = []
    stack = list(reversed(control.children))
    while stack:
        cell = stack.pop()
        if cell.split is None:
            if cell.control is not None:
                # A terminal control is trusted as a control total by a
                # whole-cell read (the builder guarantees it equals the value,
                # or leaves it on a missing cell); it is covered by a control,
                # so deduct the control rather than counting it as an exact
                # terminal or as a missing cell.
                controls.append(cell)
            elif cell.state == EXACT:
                exact_sum += cell.value if cell.value is not None else 0
            elif cell.state == MISSING:
                missing.append(cell)
            continue
        if cell.control is not None:
            controls.append(cell)
            continue
        stack.extend(reversed(cell.children))
    return controls, exact_sum, missing


@dataclass
class _Converter:
    tree_x: DimTree
    tree_y: DimTree
    maps: Dict[str, CodeMap]
    ingested: List[dict] = field(default_factory=list)
    parked: List[dict] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)

    # ---------------------------------------------------------------- records
    @staticmethod
    def _base(book: Book, cell: Cell, kind: str) -> dict:
        return {"book": book.id, "rect": [cell.rect[0], cell.rect[1]], "kind": kind}

    def _ingest(self, book: Book, cell: Cell, kind: str, amount: int,
                pair: Pair, control: Optional[int] = None,
                deducted: Optional[int] = None) -> None:
        rec = self._base(book, cell, kind)
        rec["amount"] = amount
        if control is not None:
            rec["control"] = control
            rec["deducted"] = deducted if deducted is not None else 0
        rec["new_pair"] = {"X": pair[0], "Y": pair[1]}
        self.ingested.append(rec)
        self.stats["records_ingested"] += 1

    def _park(self, book: Book, cell: Cell, kind: str, reason: str,
              amount: Optional[int] = None) -> dict:
        rec = self._base(book, cell, kind)
        if amount is not None:
            rec["amount"] = amount
        rec["reason"] = reason
        rec["new_pair"] = None
        self.parked.append(rec)
        self.stats["records_parked"] += 1
        return rec

    # -------------------------------------------------------------- traversal
    def process(self, book: Book, cell: Cell) -> None:
        """Convert one cell that owns (or organises) an amount."""
        self.stats["cells_walked"] += 1

        if cell.split is None:
            self._terminal(book, cell)
            return

        if cell.control is not None:
            scope, pair = _applicable_scope(cell, self.maps)
            if scope == "unique" and pair is not None:
                # Directly bookable control: do not visit children, so the
                # same money and its missing cells are not emitted again.
                self._ingest(
                    book, cell, KIND_CONTROL, cell.control, pair,
                    control=cell.control, deducted=0,
                )
                return
            self._cross_pair_control(book, cell)
            return

        for ch in cell.children:
            self.process(book, ch)

    def _terminal(self, book: Book, cell: Cell) -> None:
        if cell.state == NOT_APPLICABLE:
            return
        if cell.control is not None:
            # The builder only publishes a terminal control that agrees with
            # its exact value; a whole-cell read trusts the control, so recode
            # books it as a control total rather than the underlying value.
            pair = _pair(self.maps, cell.rect[0], cell.rect[1])
            if pair is None:
                self._park(book, cell, KIND_CONTROL, WHY_AMBIGUOUS,
                           amount=cell.control)
            else:
                self._ingest(book, cell, KIND_CONTROL, cell.control, pair,
                             control=cell.control, deducted=0)
            return
        if cell.state == MISSING:
            # Missing material keeps its coordinates; no zero value is made.
            self._park(book, cell, KIND_MISSING, WHY_MISSING)
            return
        value = cell.value if cell.value is not None else 0
        pair = _pair(self.maps, cell.rect[0], cell.rect[1])
        if pair is None:
            self._park(book, cell, KIND_EXACT, WHY_AMBIGUOUS, amount=value)
        else:
            self._ingest(book, cell, KIND_EXACT, value, pair)

    def _cross_pair_control(self, book: Book, cell: Cell) -> None:
        assert cell.control is not None
        controls, exact_sum, missing = _deduction_scope(cell)
        # Convert the lower levels first; insertion order is irrelevant after
        # the final geometric sort.
        for ch in cell.children:
            self.process(book, ch)

        deducted_controls = [
            c.control if c.control is not None else 0 for c in controls
        ]
        deducted = sum(deducted_controls) + exact_sum
        residual = cell.control - deducted
        if residual != 0 or missing:
            rec = self._park(book, cell, KIND_DIFF, WHY_RESIDUAL, amount=residual)
            rec["control"] = cell.control
            rec["deducted"] = deducted
            rec["deducted_controls"] = sorted(deducted_controls)
            rec["deducted_exact"] = exact_sum
            rec["outside_missing"] = bool(missing)


def _sort_key(rec: dict, tree_x: DimTree, tree_y: DimTree) -> tuple:
    x, y = rec["rect"]
    return (
        rec["book"],
        tree_x.tin[x],
        tree_y.tin[y],
        rec["kind"],
        rec.get("amount", 0),
        rec.get("reason", ""),
    )


def convert_handover(
    handover: Handover, cube: Cube, maps: Dict[str, CodeMap]
) -> dict:
    """Build the new-warehouse batch dict from published books only."""
    stats = {
        "books_seen": len(handover.books),
        "books_published": len(cube.published),
        "books_withheld": len(cube.withheld),
        "cells_walked": 0,
        "records_ingested": 0,
        "records_parked": 0,
        "leaf_pairs_materialized": 0,
    }
    conv = _Converter(
        tree_x=handover.tree_x,
        tree_y=handover.tree_y,
        maps=maps,
        stats=stats,
    )
    for book_id in sorted(cube.published):
        book = cube.published[book_id]
        conv.process(book, book.root)

    ingested = sorted(
        conv.ingested, key=lambda r: _sort_key(r, handover.tree_x, handover.tree_y)
    )
    parked = sorted(
        conv.parked, key=lambda r: _sort_key(r, handover.tree_x, handover.tree_y)
    )
    withheld = [
        {"book": bid, "reasons": list(cube.withheld[bid])}
        for bid in sorted(cube.withheld)
    ]

    def total(records: List[dict]) -> int:
        return sum(r["amount"] for r in records if "amount" in r)

    return {
        "format": "recode_batch/v1",
        "published_books": sorted(cube.published),
        "withheld_books": withheld,
        "ingested": ingested,
        "parked": parked,
        "totals": {
            "ingested": total(ingested),
            "parked": total(parked),
            "all_sources": total(ingested) + total(parked),
        },
        "stats": stats,
    }


def source_amounts(cube: Cube) -> Dict[str, int]:
    """Per published book: exact terminals plus trusted control totals.

    Mirrors a whole-cell read: an ancestor control replaces descent, so its
    money is never added together with its children's details.
    """
    out: Dict[str, int] = {}
    for bid, book in cube.published.items():
        total = 0

        def walk(cell: Cell) -> None:
            nonlocal total
            if cell.control is not None:
                total += cell.control
                return
            if cell.split is None:
                if cell.state == EXACT:
                    total += cell.value if cell.value is not None else 0
                return
            for ch in cell.children:
                walk(ch)

        walk(book.root)
        out[bid] = total
    return out


def reconcile(cube: Cube, batch: dict) -> dict:
    """Prove the batch neither loses nor double counts old-account money.

    Included on both sides: every ingested amount, every parked amount (a
    coarse exact/control parked as ambiguous still holds old money, just not
    importable), and every control difference (which closes the gap left by
    lower levels).  Missing records carry no amount.  The identity is::

        sum(batch amounts) == sum(published source amounts)

    A non-zero delta means the conversion lost or duplicated money.
    """
    by_book: Dict[str, int] = {}
    for rec in batch["ingested"]:
        by_book[rec["book"]] = by_book.get(rec["book"], 0) + rec.get("amount", 0)
    for rec in batch["parked"]:
        by_book[rec["book"]] = by_book.get(rec["book"], 0) + rec.get("amount", 0)
    expected = source_amounts(cube)
    books = sorted(set(expected) | set(by_book))
    rows = []
    unbalanced: List[str] = []
    for bid in books:
        exp = expected.get(bid, 0)
        got = by_book.get(bid, 0)
        rows.append({"book": bid, "source": exp, "batch": got, "delta": got - exp})
        if exp != got:
            unbalanced.append(bid)
    return {
        "format": "recode_reconcile/v1",
        "books": rows,
        "source_total": sum(expected.values()),
        "batch_total": sum(by_book.values()),
        "delta": sum(by_book.values()) - sum(expected.values()),
        "unbalanced_books": unbalanced,
        "ok": not unbalanced,
    }
