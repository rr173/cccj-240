"""Offline cube construction and cell retrieval.

Geometry is always expressed as rectangle pairs of *dimension nodes*.  The
engine never enumerates dimension leaves and never builds any leaf Cartesian
product: see ``stats["leaf_pairs_materialized"]``, which stays 0 even for
dimension trees with millions of implicit leaf pairs.

Per-book rules
--------------
* A non-terminal cell may carry an independent exact ``control`` total.
* The control can only be *checked* when every applicable descendant is
  precisely obtainable (exact terminals, or checked controls).
* A checkable but unequal control is a contradiction: the whole book is
  withheld (not published).
* A control on a cell whose whole subtree is not_applicable is also a
  contradiction.
* A control that cannot yet be checked is still trusted for **whole-cell**
  reads, but its difference must never be pushed down to an invisible child
  (a partial window therefore stays unknown).

Query rules
-----------
* Fully contained exact material (a trusted ancestor control, or disjoint
  exact descendants) is summed once -- an ancestor control replaces descent,
  so amounts are never double counted.
* A terminal exact cell that is only *partially* intersected contributes
  nothing exact: the cut region is reported as unknown.
* Missing and cut regions are listed as unknown; not_applicable regions are
  listed separately.
* Across books, contributions are disjoint; target area claimed by no
  published book is carved out as "unhanded" (never called not_applicable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .loader import NOT_APPLICABLE, Book, Cell, Handover
from .model import (
    DimTree,
    Rect,
    rect_disjoint,
    rect_intersection,
    subtract_rect,
)

# Result statuses
FULLY_EXACT = "fully_exact"
PARTIAL = "partial"
ALL_UNKNOWN = "all_unknown"
ALL_NOT_APPLICABLE = "not_applicable"


@dataclass
class Fragment:
    rect: Rect
    kinds: Tuple[str, ...]          # exact / not_applicable / unknown
    value: Optional[int] = None
    source: Optional[str] = None    # book id / "unhanded" / "unpublished:<id>"
    cell: Optional[Rect] = None     # originating source cell
    reason: Optional[str] = None


@dataclass
class BookResult:
    book_id: str
    status: str
    sum_known: int
    exact: List[Fragment] = field(default_factory=list)
    not_applicable: List[Fragment] = field(default_factory=list)
    unknown: List[Fragment] = field(default_factory=list)


@dataclass
class Cube:
    tree_x: DimTree
    tree_y: DimTree
    published: Dict[str, Book]
    withheld: Dict[str, List[str]]          # book id -> contradiction reasons
    withheld_rects: Dict[str, Rect]         # book id -> responsibility rect
    checks: Dict[str, List[str]]            # book id -> control check notes
    stats: Dict[str, int]


# --------------------------------------------------------------------- building
def _postorder(root: Cell) -> List[Cell]:
    order: List[Cell] = []
    stack: List[Tuple[Cell, bool]] = [(root, False)]
    while stack:
        cell, done = stack.pop()
        if done:
            order.append(cell)
        else:
            stack.append((cell, True))
            for ch in reversed(cell.children):
                stack.append((ch, False))
    return order


def _rect_str(r: Rect) -> str:
    return f"{r[0]}|{r[1]}"


def build_cube(handover: Handover) -> Cube:
    stats = {
        "books_seen": len(handover.books),
        "cells_visited": 0,
        "rectangle_partitions": 0,
        "leaf_pairs_materialized": 0,
        "max_carve_parts": 0,
    }
    published: Dict[str, Book] = {}
    withheld: Dict[str, List[str]] = {}
    withheld_rects: Dict[str, Rect] = {}
    checks: Dict[str, List[str]] = {}

    for book in handover.books:
        notes: List[str] = []
        contradictions: List[str] = []
        order = _postorder(book.root)
        # precise[cell]: every applicable descendant is exact-obtainable.
        precise: Dict[Cell, bool] = {}
        # has_app[cell]: any applicable (non-NA) terminal descendant.
        has_app: Dict[Cell, bool] = {}
        # exact_sum[cell]: sum of exact-obtainable applicable material.
        exact_sum: Dict[Cell, int] = {}

        for cell in order:
            stats["cells_visited"] += 1
            r = _rect_str(cell.rect)
            if cell.split is None:
                has_app[cell] = cell.state != NOT_APPLICABLE
                if cell.state == "exact":
                    precise[cell] = True
                    exact_sum[cell] = cell.value if cell.value is not None else 0
                else:
                    precise[cell] = False
                    exact_sum[cell] = 0
                if cell.control is not None:
                    if not has_app[cell]:
                        contradictions.append(
                            f"{r}: exact control {cell.control} on a fully "
                            f"not_applicable cell"
                        )
                    elif cell.state == "exact":
                        if cell.control == cell.value:
                            notes.append(
                                f"{r}: terminal control {cell.control} agrees "
                                f"with the exact value"
                            )
                        else:
                            contradictions.append(
                                f"{r}: terminal control {cell.control} != "
                                f"exact value {cell.value}"
                            )
                continue

            has_app[cell] = any(has_app[c] for c in cell.children)
            exact_sum[cell] = sum(
                exact_sum[c] for c in cell.children if precise[c]
            )
            precise[cell] = all(
                (not has_app[c]) or precise[c] for c in cell.children
            )

            if cell.control is not None:
                if not has_app[cell]:
                    contradictions.append(
                        f"{r}: exact control {cell.control} although the cell "
                        f"and every descendant are not_applicable"
                    )
                elif precise[cell]:
                    if cell.control == exact_sum[cell]:
                        notes.append(
                            f"{r}: control {cell.control} verified against all "
                            f"applicable children"
                        )
                    else:
                        contradictions.append(
                            f"{r}: control {cell.control} != sum of applicable "
                            f"children {exact_sum[cell]}; book withheld"
                        )
                else:
                    notes.append(
                        f"{r}: control {cell.control} not checkable yet "
                        f"(applicable descendant missing); trusted for "
                        f"whole-cell reads only"
                    )

        checks[book.id] = notes
        if contradictions:
            withheld[book.id] = contradictions
            withheld_rects[book.id] = book.rect
        else:
            published[book.id] = book

    return Cube(
        tree_x=handover.tree_x,
        tree_y=handover.tree_y,
        published=published,
        withheld=withheld,
        withheld_rects=withheld_rects,
        checks=checks,
        stats=stats,
    )


# ---------------------------------------------------------------------- queries
def _evaluate_book(
    book: Book,
    target: Rect,
    tree_x: DimTree,
    tree_y: DimTree,
    stats: Dict[str, int],
) -> Optional[BookResult]:
    """Contribution of one published book to `target`, or None if disjoint."""
    if rect_disjoint(tree_x, tree_y, book.rect, target):
        return None
    res = BookResult(book_id=book.id, status=ALL_UNKNOWN, sum_known=0)

    def add_exact(cell: Cell, rect: Rect, reason: str) -> None:
        res.exact.append(
            Fragment(
                rect=rect,
                kinds=("exact",),
                value=cell.control if reason == "control_total" else cell.value,
                source=book.id,
                cell=cell.rect,
                reason=reason,
            )
        )
        res.sum_known += res.exact[-1].value if res.exact[-1].value is not None else 0

    def walk(cell: Cell) -> None:
        stats["cells_visited"] += 1
        overlap = rect_intersection(tree_x, tree_y, cell.rect, target)
        if overlap is None:
            return

        if overlap == cell.rect:
            # The whole source cell lies inside the target window.
            if cell.control is not None:
                # Trusted ancestor amount: take it and do NOT descend, so the
                # same money is never counted twice through its children.
                add_exact(cell, cell.rect, "control_total")
                return
            if cell.split is None:
                if cell.state == "exact":
                    add_exact(cell, cell.rect, "exact_terminal")
                elif cell.state == NOT_APPLICABLE:
                    res.not_applicable.append(
                        Fragment(
                            rect=cell.rect,
                            kinds=("not_applicable",),
                            source=book.id,
                            cell=cell.rect,
                            reason="not_applicable_terminal",
                        )
                    )
                else:
                    res.unknown.append(
                        Fragment(
                            rect=cell.rect,
                            kinds=("unknown",),
                            source=book.id,
                            cell=cell.rect,
                            reason="missing_terminal",
                        )
                    )
                return
            # Internal cell without control: descend into disjoint children.
            for ch in cell.children:
                walk(ch)
            return

        # The window cuts through this cell.
        if cell.split is None:
            if cell.state == "exact":
                # A cut exact terminal cannot be split numerically.
                reason = "cut_exact_terminal"
            elif cell.state == NOT_APPLICABLE:
                reason = "not_applicable_terminal"
            else:
                reason = "missing_terminal"
            kind = (
                "not_applicable"
                if reason == "not_applicable_terminal"
                else "unknown"
            )
            frag = Fragment(
                rect=overlap,
                kinds=(kind,),
                source=book.id,
                cell=cell.rect,
                reason=reason,
            )
            if kind == "not_applicable":
                res.not_applicable.append(frag)
            else:
                res.unknown.append(frag)
            return

        # Internal cell cut by the window: even with a control, the difference
        # must never be allocated to an invisible subcell, so descend.
        for ch in cell.children:
            walk(ch)

    walk(book.root)

    if res.unknown:
        res.status = PARTIAL if res.exact else ALL_UNKNOWN
    elif res.exact:
        res.status = FULLY_EXACT
    else:
        res.status = ALL_NOT_APPLICABLE
    return res


def _carve_uncovered(
    target: Rect,
    blockers: List[Tuple[str, Rect, str]],
    tree_x: DimTree,
    tree_y: DimTree,
    stats: Dict[str, int],
) -> List[Fragment]:
    """Subtract every blocker rectangle from target; return the remainder.

    The remainder stays a small list of node rectangles; leaves are never
    enumerated.  Blockers may themselves partially overlap earlier pieces;
    each blocker only removes its intersection with what remains.
    """
    remaining: List[Rect] = [target]
    for _label, brect, _reason in blockers:
        nxt: List[Rect] = []
        for part in remaining:
            inter = rect_intersection(tree_x, tree_y, part, brect)
            if inter is None:
                nxt.append(part)
                continue
            stats["rectangle_partitions"] += 1
            nxt.extend(subtract_rect(tree_x, tree_y, part, inter))
        remaining = nxt
        stats["max_carve_parts"] = max(stats["max_carve_parts"], len(remaining))
    return [
        Fragment(
            rect=rect,
            kinds=("unknown",),
            source="unhanded",
            reason="no_book_declares_this_region",
        )
        for rect in remaining
    ]


def query_cube(cube: Cube, target: Rect) -> dict:
    tree_x, tree_y = cube.tree_x, cube.tree_y
    tree_x.require_node(target[0])
    tree_y.require_node(target[1])

    book_results: List[BookResult] = []
    # (label, rectangle, reason): every rectangle a book is responsible for.
    blockers: List[Tuple[str, Rect, str]] = []
    for book in cube.published.values():
        br = _evaluate_book(book, target, tree_x, tree_y, cube.stats)
        if br is not None:
            book_results.append(br)
            blockers.append((book.id, book.rect, "book_responsibility"))
    withheld_fragments: List[Fragment] = []
    for bid, brect in cube.withheld_rects.items():
        inter = rect_intersection(tree_x, tree_y, brect, target)
        if inter is not None:
            blockers.append((f"unpublished:{bid}", brect, "book_withheld"))
            withheld_fragments.append(
                Fragment(
                    rect=inter,
                    kinds=("unknown",),
                    source=f"unpublished:{bid}",
                    cell=brect,
                    reason="book_withheld",
                )
            )

    unhanded = _carve_uncovered(target, blockers, tree_x, tree_y, cube.stats)

    exact: List[Fragment] = []
    na: List[Fragment] = []
    unknown: List[Fragment] = []
    sum_known = 0
    for br in book_results:
        exact.extend(br.exact)
        na.extend(br.not_applicable)
        unknown.extend(br.unknown)
        sum_known += br.sum_known
    unknown.extend(withheld_fragments)
    unknown.extend(unhanded)

    if not exact and not unknown:
        status = ALL_NOT_APPLICABLE if na else ALL_UNKNOWN
    elif unknown:
        status = PARTIAL if exact else ALL_UNKNOWN
    else:
        status = FULLY_EXACT

    return {
        "target": list(target),
        "status": status,
        "sum_known": sum_known,
        "book_results": book_results,
        "exact": exact,
        "not_applicable": na,
        "unknown": unknown,
        "stats": dict(cube.stats),
    }
