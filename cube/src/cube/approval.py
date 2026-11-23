"""Approval of a responsibility handover: build the postable correction package.

Two validated handover versions (the already-posted *current* books and the
*proposed* books) are compared.  Only ids that newly appear, disappear, or
whose content changed are correction candidates; merely reordering the write
order of a book is not a change.

Candidates are grouped by the handover itself, not one id at a time: two
candidates are bound when one's *old* responsibility rectangle and the
other's *new* responsibility rectangle cover the same place (a non-empty
rectangle intersection on both trees).  The relation is followed
transitively, so an indirect chain of handovers stays one atomic group.
Every intersection test is a node-subtree test using the DFS intervals of
the dimension trees; leaf pairs are never enumerated.

A group becomes effective only when every member id is approved and every
member that exists in the proposed version is publishable there.  A proposed
book withheld by the existing builder (e.g. a control-total conflict) blocks
its whole group but is reported inside the package, never as input
corruption.  For an effective group the old, already-posted ingested records
of its members are reversed one by one (opposite sign, same book / cell /
source kind / full new code pair) and the new ingested records are posted one
by one.  Reversals and postings are never netted.

The package also carries the *effective handover* in the existing books
format: effective groups wholesale replaced by their proposed version
(including deletions and additions), everything else kept from the current
version.  It is directly usable as the next run's current books.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Dict, List, Optional

from .engine import build_cube
from .loader import Handover, load_handover
from .mapping import CodeMap
from .model import ModelError, Rect, rect_intersection
from .recode import convert_handover

PACKAGE_FORMAT = "correction_package/v1"
HANDOVER_FORMAT = "handover_books/v1"


# --------------------------------------------------------------------- diffing
def _canon_cell(raw: dict) -> dict:
    """Order-independent canonical form of one decomposition cell."""
    if not isinstance(raw, dict) or "rect" not in raw:
        raise ModelError("a cell needs a rect")
    out: Dict[str, object] = {"rect": list(raw["rect"])}
    if "children" in raw:
        out["split"] = raw["split"]
        if raw.get("control") is not None:
            out["control"] = raw["control"]
        out["children"] = [_canon_cell(c) for c in raw["children"]]
        out["children"].sort(key=lambda c: tuple(c["rect"]))
    else:
        out["state"] = raw["state"]
        if "value" in raw and raw["value"] is not None:
            out["value"] = raw["value"]
        if raw.get("control") is not None:
            out["control"] = raw["control"]
    return out


def book_signature(raw_book: dict) -> str:
    """Stable signature covering rect, decomposition, states, values, controls.

    Two books with the same signature differ only in sibling write order.
    """
    canon = {
        "id": raw_book["id"],
        "rect": list(raw_book["rect"]),
        "root": _canon_cell(raw_book["root"]),
    }
    return json.dumps(canon, sort_keys=True, ensure_ascii=False)


def _raw_book(raw_books: List[dict], bid: str) -> Optional[dict]:
    for raw in raw_books:
        if raw["id"] == bid:
            return raw
    return None


# ------------------------------------------------------------- union-find graph
class _DSU:
    def __init__(self, ids: List[str]):
        self.parent = {bid: bid for bid in ids}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic representative: the lexicographically smaller id.
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


@dataclass
class Candidate:
    bid: str
    kind: str                       # "added" / "removed" / "changed"
    old_rect: Optional[Rect] = None
    new_rect: Optional[Rect] = None


def _overlaps(
    handover: Handover, a: Rect, b: Rect
) -> bool:
    """Node-level rectangle intersection; never expands leaves."""
    return rect_intersection(handover.tree_x, handover.tree_y, a, b) is not None


def build_groups(
    candidates: Dict[str, Candidate], handover: Handover
) -> List[List[str]]:
    """Connected components of the handover binding graph.

    An edge joins two candidates when one's *old* responsibility rectangle and
    the other's *new* responsibility rectangle cover the same place (a
    non-empty intersection on both trees).  Both directions are tried, so a
    handover binds its predecessor and its successor; edges are then followed
    transitively, keeping an indirect chain one atomic group.  Every test is a
    node-subtree intersection using the DFS intervals -- leaf pairs are never
    enumerated.
    """
    ids = sorted(candidates)
    dsu = _DSU(ids)
    olds = [
        (bid, candidates[bid].old_rect)
        for bid in ids
        if candidates[bid].old_rect is not None
    ]
    news = [
        (bid, candidates[bid].new_rect)
        for bid in ids
        if candidates[bid].new_rect is not None
    ]
    for id_old, old_rect in olds:
        for id_new, new_rect in news:
            if id_old == id_new:
                continue
            if dsu.find(id_old) == dsu.find(id_new):
                continue
            if _overlaps(handover, old_rect, new_rect):
                dsu.union(id_old, id_new)
    groups: Dict[str, List[str]] = {}
    for bid in ids:
        groups.setdefault(dsu.find(bid), []).append(bid)
    return [sorted(members) for members in groups.values()]


# --------------------------------------------------------------- recode records
def _ingested_by_book(batch: dict) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for rec in batch["ingested"]:
        out.setdefault(rec["book"], []).append(rec)
    return out


def _reversal(rec: dict) -> dict:
    """Reverse one posted ingested record: opposite sign, everything else kept.

    Negative and zero source amounts are reversed identically; parked and
    missing records never reach here because they were never posted.
    """
    out = copy.deepcopy(rec)
    out["entry"] = "reversal"
    out["amount"] = -rec["amount"]
    return out


def _posting(rec: dict) -> dict:
    out = copy.deepcopy(rec)
    out["entry"] = "posting"
    return out


# ------------------------------------------------------------- package assembly
def _rect_sort_key(handover: Handover, rect: Rect) -> tuple:
    return (handover.tree_x.tin[rect[0]], handover.tree_y.tin[rect[1]])


def _candidate_rect(cand: Candidate) -> Rect:
    # Every candidate has at least one side; removed books fall back to old.
    rect = cand.new_rect if cand.new_rect is not None else cand.old_rect
    assert rect is not None
    return rect


def _record_sort_key(rec: dict, handover: Handover) -> tuple:
    x, y = rec["rect"]
    return (
        rec["book"],
        handover.tree_x.tin[x],
        handover.tree_y.tin[y],
        rec["kind"],
        rec.get("amount", 0),
        rec.get("reason", ""),
    )


def build_correction_package(
    dims_raw: dict,
    current_raw: dict,
    proposed_raw: dict,
    approvals: List[str],
    maps: Dict[str, CodeMap],
) -> dict:
    """Build the postable correction package, or raise without side effects.

    Structural validation failures and unknown approval ids raise
    ``ModelError``; the CLI layer turns those into an error exit and never
    touches an existing package file.
    """
    # --- load both versions (empty book lists are allowed) ----------------
    current = load_handover(dims_raw, current_raw, allow_empty=True)
    proposed = load_handover(dims_raw, proposed_raw, allow_empty=True)
    current_cube = build_cube(current)
    proposed_cube = build_cube(proposed)

    # The currently posted version must be fully publishable; otherwise there
    # is nothing coherent to reverse.
    if current_cube.withheld:
        bad = ", ".join(sorted(current_cube.withheld))
        raise ModelError(
            f"current handover must be fully publishable; withheld books: {bad}"
        )

    cur_raw_books = list(current_raw.get("books", []))
    new_raw_books = list(proposed_raw.get("books", []))
    cur_ids = {b["id"] for b in cur_raw_books}
    new_ids = {b["id"] for b in new_raw_books}

    # --- approval list sanity ---------------------------------------------
    if not all(isinstance(a, str) and a for a in approvals):
        raise ModelError("approval list must contain non-empty book id strings")
    approved = set(approvals)
    unknown = sorted(approved - cur_ids - new_ids)
    if unknown:
        raise ModelError(
            "approved ids absent from both handover versions: "
            + ", ".join(unknown)
        )

    # --- candidates: added / removed / content changed --------------------
    sig_cur = {b["id"]: book_signature(b) for b in cur_raw_books}
    sig_new = {b["id"]: book_signature(b) for b in new_raw_books}
    candidates: Dict[str, Candidate] = {}
    cur_books = {b.id: b for b in current.books}
    new_books = {b.id: b for b in proposed.books}
    for bid in sorted(cur_ids | new_ids):
        if bid not in cur_ids:
            candidates[bid] = Candidate(
                bid, "added", new_rect=new_books[bid].rect
            )
        elif bid not in new_ids:
            candidates[bid] = Candidate(
                bid, "removed", old_rect=cur_books[bid].rect
            )
        elif sig_cur[bid] != sig_new[bid]:
            candidates[bid] = Candidate(
                bid, "changed",
                old_rect=cur_books[bid].rect,
                new_rect=new_books[bid].rect,
            )
        # present and equal in both -> no correction, no entries

    # --- irreducible handover groups --------------------------------------
    member_groups = build_groups(candidates, current) if candidates else []

    # --- recode both versions once; ingested records drive the entries ----
    current_batch = convert_handover(current, current_cube, maps)
    proposed_batch = convert_handover(proposed, proposed_cube, maps)
    cur_ingested = _ingested_by_book(current_batch)
    new_ingested = _ingested_by_book(proposed_batch)

    reversals: List[dict] = []
    postings: List[dict] = []
    group_reports: List[dict] = []
    effective_ids: set = set()

    for members in member_groups:
        missing = sorted(b for b in members if b not in approved)
        withheld = [
            {"book": bid, "reasons": list(proposed_cube.withheld[bid])}
            for bid in members
            if bid in proposed_cube.withheld
        ]
        effective = not missing and not withheld
        report = {
            "members": list(members),
            "effective": effective,
            "missing_approvals": missing,
            "withheld_books": withheld,
        }
        if effective:
            effective_ids.update(members)
            for bid in members:
                for rec in cur_ingested.get(bid, ()):
                    reversals.append(_reversal(rec))
                for rec in new_ingested.get(bid, ()):
                    postings.append(_posting(rec))
        group_reports.append(report)

    # Deterministic order, independent of input write order.
    group_reports.sort(
        key=lambda g: (
            min(_rect_sort_key(current, _candidate_rect(candidates[b]))
                for b in g["members"]),
            g["members"],
        )
    )
    reversals.sort(key=lambda r: _record_sort_key(r, current))
    postings.sort(key=lambda r: _record_sort_key(r, proposed))

    # --- effective handover: effective groups -> proposed, rest -> current
    effective_books_raw: List[dict] = []
    for bid in sorted(cur_ids | new_ids):
        if bid in effective_ids:
            raw = _raw_book(new_raw_books, bid)
        else:
            raw = _raw_book(cur_raw_books, bid)
        if raw is not None:
            effective_books_raw.append(raw)
    effective_handover = {
        "format": HANDOVER_FORMAT,
        "books": [_canon_book(raw) for raw in effective_books_raw],
    }
    effective_handover["books"].sort(
        key=lambda rb: (
            current.tree_x.tin[rb["rect"][0]],
            current.tree_y.tin[rb["rect"][1]],
            rb["id"],
        )
    )

    # Internal invariant: the next current handover must load, and every one
    # of its books must publish.  A failure here must never leave a package.
    check_handover = load_handover(
        dims_raw, {"books": effective_handover["books"]}, allow_empty=True
    )
    check_cube = build_cube(check_handover)
    if check_cube.withheld:
        raise ModelError(
            "internal error: effective handover contains withheld books "
            + ", ".join(sorted(check_cube.withheld))
        )

    def _total(records: List[dict]) -> int:
        return sum(r["amount"] for r in records)

    stats = {
        "candidate_books": len(candidates),
        "groups_total": len(group_reports),
        "groups_effective": sum(1 for g in group_reports if g["effective"]),
        "groups_held": sum(1 for g in group_reports if not g["effective"]),
        "reversal_records": len(reversals),
        "posting_records": len(postings),
        "leaf_pairs_materialized": 0,
    }
    return {
        "format": PACKAGE_FORMAT,
        "approved_ids": sorted(approved),
        "groups": group_reports,
        "reversals": reversals,
        "postings": postings,
        "totals": {
            "reversals": _total(reversals),
            "postings": _total(postings),
        },
        "effective_handover": effective_handover,
        "stats": stats,
    }


def _canon_book(raw: dict) -> dict:
    return {
        "id": raw["id"],
        "rect": list(raw["rect"]),
        "root": _canon_cell(raw["root"]),
    }
