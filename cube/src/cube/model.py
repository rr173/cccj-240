"""Dimension tree model and rectangle (subtree x subtree) helpers.

All reasoning is done on *node identities* and ancestry.  Leaves are never
enumerated, so the size of the implicit leaf Cartesian product is irrelevant.
A dimension tree is finite and acyclic; its leaves are mutually exclusive and
cover the root.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


class ModelError(ValueError):
    """Raised on malformed handover definitions."""


class DimTree:
    """A rooted dimension tree with O(1) ancestry/intersection queries.

    Nodes are arbitrary strings.  Children are ordered (the handover requires
    splitting into *all* direct children, in tree order).
    """

    def __init__(self, name: str, raw: dict):
        self.name = name
        self.children: Dict[str, List[str]] = {}
        self.parent: Dict[str, Optional[str]] = {}
        self._parse(name, raw)
        self.root: str = next(iter(self.children))
        self.depth: Dict[str, int] = {}
        # DFS entry/exit "times": a contains b  <=>  tin[a] <= tin[b] <= tout[a]
        self.tin: Dict[str, int] = {}
        self.tout: Dict[str, int] = {}
        self._number()
        self._leaves = frozenset(
            n for n, ch in self.children.items() if not ch
        )

    # ------------------------------------------------------------------ parse
    def _parse(self, name: str, raw: dict) -> None:
        if not isinstance(raw, dict) or "root" not in raw or "nodes" not in raw:
            raise ModelError(f"dimension {name!r} needs 'root' and 'nodes'")
        nodes = raw["nodes"]
        if not isinstance(nodes, dict):
            raise ModelError(f"dimension {name!r}: nodes must be an object")
        seen: set = set()
        for node, kids in nodes.items():
            if not isinstance(node, str) or not node:
                raise ModelError(f"dimension {name!r}: bad node id {node!r}")
            if node in seen:
                raise ModelError(f"dimension {name!r}: duplicate node {node!r}")
            seen.add(node)
            if kids is None:
                kids = []
            if not isinstance(kids, list) or not all(isinstance(k, str) for k in kids):
                raise ModelError(
                    f"dimension {name!r}: children of {node!r} must be a list of ids"
                )
            if len(kids) != len(set(kids)):
                raise ModelError(
                    f"dimension {name!r}: duplicate children under {node!r}"
                )
            self.children[node] = list(kids)
        root = raw["root"]
        if root not in self.children:
            raise ModelError(f"dimension {name!r}: root {root!r} is not a node")
        for node, kids in self.children.items():
            for k in kids:
                if k not in self.children:
                    raise ModelError(
                        f"dimension {name!r}: unknown child {k!r} under {node!r}"
                    )
                if k in self.parent:
                    raise ModelError(
                        f"dimension {name!r}: node {k!r} has two parents"
                    )
                self.parent[k] = node
        if self.parent.get(root) is not None:
            raise ModelError(f"dimension {name!r}: root has a parent")
        # Reachability from root must cover every node exactly once (acyclic).
        stack = [root]
        reached: set = set()
        while stack:
            n = stack.pop()
            if n in reached:
                raise ModelError(f"dimension {name!r}: cycle or merge at {n!r}")
            reached.add(n)
            stack.extend(reversed(self.children[n]))
        if reached != seen:
            missing = sorted(seen - reached)
            raise ModelError(
                f"dimension {name!r}: unreachable nodes {missing}"
            )

    def _number(self) -> None:
        timer = 0
        stack: List[Tuple[str, int]] = [(self.root, 0)]
        self.depth[self.root] = 0
        while stack:
            n, state = stack.pop()
            if state == 0:
                self.tin[n] = timer
                timer += 1
                stack.append((n, 1))
                for k in reversed(self.children[n]):
                    self.depth[k] = self.depth[n] + 1
                    stack.append((k, 0))
            else:
                self.tout[n] = timer - 1

    # ----------------------------------------------------------------- queries
    def contains(self, ancestor: str, node: str) -> bool:
        """True iff `ancestor`'s subtree contains `node` (equal counts)."""
        return self.tin[ancestor] <= self.tin[node] <= self.tout[ancestor]

    def is_leaf(self, node: str) -> bool:
        return node in self._leaves

    def intersection(self, a: str, b: str) -> Optional[str]:
        """Intersection of two subtrees: the deeper node, or None."""
        if self.contains(a, b):
            return b
        if self.contains(b, a):
            return a
        return None

    def require_node(self, node: str) -> None:
        if node not in self.children:
            raise ModelError(f"dimension {self.name!r} has no node {node!r}")

    def node_count(self) -> int:
        return len(self.children)


# A rectangle is a node pair (x_node, y_node): the Cartesian product of the
# two subtrees.  All geometry below is expressed as such pairs -- never as
# leaf enumerations.
Rect = Tuple[str, str]


def rect_contains(tree_x: DimTree, tree_y: DimTree, outer: Rect, inner: Rect) -> bool:
    return tree_x.contains(outer[0], inner[0]) and tree_y.contains(outer[1], inner[1])


def rect_disjoint(tree_x: DimTree, tree_y: DimTree, a: Rect, b: Rect) -> bool:
    return (
        tree_x.intersection(a[0], b[0]) is None
        or tree_y.intersection(a[1], b[1]) is None
    )


def rect_intersection(
    tree_x: DimTree, tree_y: DimTree, a: Rect, b: Rect
) -> Optional[Rect]:
    ix = tree_x.intersection(a[0], b[0])
    iy = tree_y.intersection(a[1], b[1])
    if ix is None or iy is None:
        return None
    return ix, iy


def subtract_rect(
    tree_x: DimTree, tree_y: DimTree, outer: Rect, inner: Rect
) -> List[Rect]:
    """Partition `outer` minus `inner` into rectangles, without leaf expansion.

    Precondition: `inner` is fully contained in `outer`.  At most
    (depth + siblings) rectangles are produced; complexity is independent of
    the number of leaves inside either subtree.
    """
    ox, oy = outer
    ix, iy = inner
    if not (tree_x.contains(ox, ix) and tree_y.contains(oy, iy)):
        raise ModelError("subtract_rect: inner rectangle not contained in outer")
    out: List[Rect] = []

    # Carve along X: the sibling subtrees of the path from ox down to ix,
    # keeping the whole Y span of outer.
    chain_x: List[str] = []
    cur = ix
    while cur != ox:
        chain_x.append(cur)
        cur = tree_x.parent[cur]  # type: ignore[assignment]
    for node in chain_x:
        p = tree_x.parent[node]
        assert p is not None
        for sib in tree_x.children[p]:
            if sib != node:
                out.append((sib, oy))
    # Carve along Y inside the remaining X column, similarly.
    chain_y: List[str] = []
    cur = iy
    while cur != oy:
        chain_y.append(cur)
        cur = tree_y.parent[cur]  # type: ignore[assignment]
    for node in chain_y:
        p = tree_y.parent[node]
        assert p is not None
        for sib in tree_y.children[p]:
            if sib != node:
                out.append((ix, sib))
    return out
