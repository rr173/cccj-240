"""Old-tree node -> new-code mapping (the "recode" rules).

Mapping file shape::

    {
      "X": [ {"old": "A", "new": "N1"}, {"old": "A1", "new": "N2"} ],
      "Y": [ {"old": "P", "new": "M1"} ]
    }

* A rule applies to the whole old subtree rooted at ``old``.
* A rule closer to the leaves overrides any ancestor rule ("most specific
  wins"); a node covered only by an ancestor rule inherits that rule.
* Several different old leaves may map to the same new code.

Validation only inspects *dimension nodes* (one top-down pass per axis), never
leaf pairs: unknown old nodes, a second rule on the same node, an empty new
code, or an old leaf with no rule at all all reject the whole conversion.
New-code sets are computed lazily for the subtrees a conversion actually
touches, so a handful of source cells stays cheap even when each axis has
tens of thousands of leaves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .model import DimTree, ModelError

AXES = ("X", "Y")


@dataclass(frozen=True)
class Rule:
    axis: str
    old: str
    new: str


class CodeMap:
    """Resolved recode rules for one axis.

    ``own[node]`` is the code of the nearest rule on node's ancestor-or-self
    chain (``None`` when no rule covers it).  ``merge_code(node)`` returns the
    single new code the *whole* old subtree resolves to, or ``None`` when the
    subtree spans several codes (ambiguous as a whole).
    """

    def __init__(self, axis: str, tree: DimTree, rules: List[Rule]):
        self.axis = axis
        self.tree = tree
        direct: Dict[str, str] = {r.old: r.new for r in rules}

        # One top-down pass: nearest rule per node.  O(nodes), no leaf pairs.
        own: Dict[str, Optional[str]] = {}
        stack: List[tuple] = [(tree.root, None)]
        while stack:
            node, inherited = stack.pop()
            cur = direct.get(node, inherited)
            own[node] = cur
            for child in reversed(tree.children[node]):
                stack.append((child, cur))
        self.own = own
        self._codes: Dict[str, frozenset] = {}

    # ------------------------------------------------------------------ lookup
    def node_codes(self, node: str) -> frozenset:
        """Distinct new codes over all old leaves under ``node``."""
        cached = self._codes.get(node)
        if cached is not None:
            return cached
        children = self.tree.children
        stack: List[tuple] = [(node, False)]
        while stack:
            cur, done = stack.pop()
            if cur in self._codes:
                continue
            if not done:
                stack.append((cur, True))
                for child in children[cur]:
                    if child not in self._codes:
                        stack.append((child, False))
                continue
            if not children[cur]:
                code = self.own[cur]
                self._codes[cur] = frozenset((code,)) if code is not None else frozenset()
            else:
                merged: set = set()
                for child in children[cur]:
                    merged |= self._codes[child]
                self._codes[cur] = frozenset(merged)
        return self._codes[node]

    def merge_code(self, node: str) -> Optional[str]:
        """The one new code for ``node``'s subtree, or None when ambiguous."""
        if node not in self.tree.children:
            self.tree.require_node(node)  # raises with a clear message
        codes = self.node_codes(node)
        if len(codes) == 1:
            return next(iter(codes))
        return None

    def unmapped_leaves(self) -> List[str]:
        return sorted(leaf for leaf in self.tree._leaves if self.own[leaf] is None)


def _parse_rules(raw: dict, axis: str) -> List[Rule]:
    if axis not in raw:
        raise ModelError(f"mapping file has no {axis!r} rule list")
    entries = raw[axis]
    if not isinstance(entries, list):
        raise ModelError(f"mapping {axis!r} must be a list of rules")
    if not entries:
        raise ModelError(f"mapping {axis!r} has no rules")
    rules: List[Rule] = []
    seen: Dict[str, str] = {}
    for i, entry in enumerate(entries):
        where = f"mapping {axis}[{i}]"
        if not isinstance(entry, dict):
            raise ModelError(f"{where}: rule must be an object")
        old = entry.get("old")
        new = entry.get("new")
        if not isinstance(old, str) or not old:
            raise ModelError(f"{where}: 'old' must be a non-empty node id")
        if not isinstance(new, str) or not new.strip():
            raise ModelError(f"{where}: 'new' must be a non-empty code")
        if old in seen:
            raise ModelError(
                f"mapping {axis!r}: duplicate rule for old node {old!r} "
                f"(already maps to {seen[old]!r})"
            )
        seen[old] = new
        rules.append(Rule(axis=axis, old=old, new=new))
    return rules


def load_mapping(raw: dict, tree_x: DimTree, tree_y: DimTree) -> Dict[str, CodeMap]:
    """Parse + validate rules; every old leaf of both axes must resolve."""
    if not isinstance(raw, dict):
        raise ModelError("mapping file must be a JSON object")
    trees = {"X": tree_x, "Y": tree_y}
    maps: Dict[str, CodeMap] = {}
    for axis in AXES:
        tree = trees[axis]
        rules = _parse_rules(raw, axis)
        for r in rules:
            tree.require_node(r.old)
        cmap = CodeMap(axis, tree, rules)
        missing = cmap.unmapped_leaves()
        if missing:
            shown = ", ".join(missing[:8])
            more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
            raise ModelError(
                f"mapping {axis!r}: old leaves with no rule: {shown}{more}"
            )
        maps[axis] = cmap
    return maps
