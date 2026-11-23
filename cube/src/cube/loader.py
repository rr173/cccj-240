"""Loading and structural validation of book handover material.

Input JSON shape
----------------
dimensions file::

    {
      "dimensions": {
        "X": {"root": "x0", "nodes": {"x0": ["x1", "x2"], "x1": [], ...}},
        "Y": {...}
      }
    }

books file::

    {
      "books": [
        {
          "id": "B1",
          "rect": ["x1", "y1"],                 # responsibility rectangle
          "root": {"rect": ["x1", "y1"], "split": "X",
                   "control": 10,               # optional exact control total
                   "children": [
                       {"rect": ["x2", "y1"], "state": "exact", "value": 4},
                       {"rect": ["x3", "y1"], "state": "missing"},
                       {"rect": ["x3", "y1"], "state": "not_applicable"}
                   ]}
        }
      ]
    }

Rules enforced here (structural only; control checks live in engine.py):

* every non-terminal cell splits along exactly one dimension into ALL direct
  children of that dimension's node, keeping the other dimension fixed;
* cells form a tree rooted at the responsibility rectangle;
* the decomposition covers the responsibility rectangle (siblings partition
  their parent's span on the split dimension);
* terminal cells carry exactly one of exact/missing/not_applicable;
* responsibility rectangles of different books are leaf-pair disjoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .model import DimTree, ModelError, Rect, rect_disjoint

EXACT = "exact"
MISSING = "missing"
NOT_APPLICABLE = "not_applicable"
STATES = (EXACT, MISSING, NOT_APPLICABLE)


@dataclass(eq=False)
class Cell:
    rect: Rect
    split: Optional[str]              # "X" / "Y" for non-terminal, else None
    children: List["Cell"] = field(default_factory=list)
    state: Optional[str] = None       # terminal state
    value: Optional[int] = None       # exact terminal value
    control: Optional[int] = None     # optional exact control on any cell


@dataclass
class Book:
    id: str
    rect: Rect
    root: Cell
    # index of every cell by rectangle (a rectangle appears at most once)
    index: Dict[Rect, Cell] = field(default_factory=dict)


@dataclass
class Handover:
    tree_x: DimTree
    tree_y: DimTree
    books: List[Book]


def _as_int(v: Any, where: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ModelError(f"{where}: integer required, got {v!r}")
    return v


def _parse_cell(
    raw: dict,
    book_id: str,
    tree_x: DimTree,
    tree_y: DimTree,
    index: Dict[Rect, Cell],
) -> Cell:
    if not isinstance(raw, dict) or "rect" not in raw:
        raise ModelError(f"book {book_id}: every cell needs a rect")
    rect = raw["rect"]
    if not (isinstance(rect, list) and len(rect) == 2):
        raise ModelError(f"book {book_id}: rect must be [x_node, y_node]")
    x, y = rect
    tree_x.require_node(x)
    tree_y.require_node(y)
    if (x, y) in index:
        raise ModelError(f"book {book_id}: rectangle {x}|{y} appears twice")

    control = _as_int(raw["control"], f"book {book_id} cell {x}|{y} control") \
        if "control" in raw and raw["control"] is not None else None

    if "children" in raw:
        split = raw.get("split")
        if split not in ("X", "Y"):
            raise ModelError(
                f"book {book_id} cell {x}|{y}: non-terminal needs split X|Y"
            )
        if "state" in raw or "value" in raw:
            raise ModelError(
                f"book {book_id} cell {x}|{y}: cannot mix children with state"
            )
        cell = Cell(rect=(x, y), split=split, control=control)
        index[cell.rect] = cell
        cell.children = [
            _parse_cell(c, book_id, tree_x, tree_y, index) for c in raw["children"]
        ]
        _validate_split(cell, book_id, tree_x, tree_y)
        return cell

    state = raw.get("state")
    if state not in STATES:
        raise ModelError(
            f"book {book_id} cell {x}|{y}: terminal state must be one of {STATES}"
        )
    value = None
    if state == EXACT:
        if "value" not in raw:
            raise ModelError(
                f"book {book_id} cell {x}|{y}: exact cell needs an integer value"
            )
        value = _as_int(raw["value"], f"book {book_id} cell {x}|{y} value")
    elif "value" in raw and raw["value"] is not None:
        raise ModelError(
            f"book {book_id} cell {x}|{y}: only exact cells carry values"
        )
    cell = Cell(rect=(x, y), split=None, state=state, value=value, control=control)
    index[cell.rect] = cell
    return cell


def _validate_split(
    cell: Cell, book_id: str, tree_x: DimTree, tree_y: DimTree
) -> None:
    x, y = cell.rect
    assert cell.split is not None
    tree = tree_x if cell.split == "X" else tree_y
    split_node = x if cell.split == "X" else y
    direct = tree.children[split_node]
    if not direct:
        raise ModelError(
            f"book {book_id} cell {x}|{y}: cannot split a leaf dimension node"
        )
    fixed = y if cell.split == "X" else x
    seen = [c.rect for c in cell.children]
    if len(seen) != len(set(seen)):
        raise ModelError(f"book {book_id} cell {x}|{y}: duplicate child rectangles")
    expected: List[Rect]
    if cell.split == "X":
        expected = [(k, fixed) for k in direct]
    else:
        expected = [(fixed, k) for k in direct]
    if sorted(seen) != sorted(expected):
        raise ModelError(
            f"book {book_id} cell {x}|{y}: split must enumerate ALL direct "
            f"children {expected} of the split dimension, got {seen}"
        )
    # Children must be contained in the parent rectangle.
    for c in cell.children:
        cx, cy = c.rect
        if not tree_x.contains(x, cx) or not tree_y.contains(y, cy):
            raise ModelError(
                f"book {book_id} cell {x}|{y}: child {cx}|{cy} escapes the cell"
            )


def load_handover(dims_raw: dict, books_raw: dict, *, allow_empty: bool = False) -> Handover:
    try:
        dims = dims_raw["dimensions"]
    except (TypeError, KeyError):
        raise ModelError("dimensions file needs a 'dimensions' object")
    if set(dims) != {"X", "Y"}:
        raise ModelError("dimensions file must define exactly 'X' and 'Y'")
    tree_x = DimTree("X", dims["X"])
    tree_y = DimTree("Y", dims["Y"])

    if not isinstance(books_raw, dict) or not isinstance(
        books_raw.get("books"), list
    ) or (not books_raw["books"] and not allow_empty):
        raise ModelError("books file needs a non-empty 'books' list")

    books: List[Book] = []
    seen_ids: set = set()
    for raw in books_raw["books"]:
        if not isinstance(raw, dict) or "id" not in raw or "root" not in raw:
            raise ModelError("each book needs id/rect/root")
        bid = raw["id"]
        if bid in seen_ids:
            raise ModelError(f"duplicate book id {bid!r}")
        seen_ids.add(bid)
        if "rect" not in raw or not (
            isinstance(raw["rect"], list) and len(raw["rect"]) == 2
        ):
            raise ModelError(f"book {bid}: rect must be [x_node, y_node]")
        rx, ry = raw["rect"]
        tree_x.require_node(rx)
        tree_y.require_node(ry)
        index: Dict[Rect, Cell] = {}
        root = _parse_cell(raw["root"], bid, tree_x, tree_y, index)
        if root.rect != (rx, ry):
            raise ModelError(
                f"book {bid}: decomposition root {root.rect} must equal "
                f"responsibility rect {(rx, ry)}"
            )
        books.append(Book(id=bid, rect=(rx, ry), root=root, index=index))

    # Responsibility rectangles must be leaf-pair disjoint across books.
    for i in range(len(books)):
        for j in range(i + 1, len(books)):
            a, b = books[i].rect, books[j].rect
            if not rect_disjoint(tree_x, tree_y, a, b):
                raise ModelError(
                    f"responsibility rectangles of {books[i].id} and "
                    f"{books[j].id} overlap on leaf pairs"
                )
    return Handover(tree_x=tree_x, tree_y=tree_y, books=books)
