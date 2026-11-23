"""Receipt sign-off verification: approved correction packages vs posting log.

An auditor holds the *plan side* -- an ordered list of approved rounds, each
naming a ``correction_package/v1`` file produced by the existing ``approve``
command -- and the *actual side* -- a set of unordered, overlapping NDJSON
export slices of the warehouse posting log.  This module decides, for the
log prefix up to a non-negative commit cutoff, which correction groups can be
signed off by an actual transaction receipt, which never happened, and which
arrived but cannot be signed off.  It never posts anything and never builds
repair entries.

Plan side
---------
Only ``effective=true`` groups with at least one expected entry (a reversal
or a posting attributed to a member book) are signable.  Entries are
attributed to a group by ``(round, book)``; zero amounts are expected
entries like any other.  An effective group without entries needs no
receipt and is never a prerequisite.  A non-effective group must not
receive entries at all.  If the round list, group membership, or the
in-package entry attribution cannot be interpreted uniquely, the plan is
invalid.

Actual side
-----------
Each log line carries ``commit`` (transaction number, consecutive from 1 in
this log), ``line`` (1..``line_count`` inside its transaction), ``round``,
and the entry fields ``entry``/``book``/``rect``/``kind``/``new_pair``/
``amount``.  Identical records at the same ``(commit, line)`` are export
overlap and count once; conflicting content at the same position, an
inconsistent ``line_count``, or a missing commit or line make the prefix
unverifiable (an error, not a conclusion).  The same content at two
different positions is two real postings and is never deduplicated.

Sign-off
--------
A transaction signs off (taking every group it touches with it) iff all of
these hold:

* every line attributes to an effective group that has expected entries;
* each touched group's whole expected multiset appears, exactly and by
  count, inside this one transaction, and no other transaction in the
  prefix carries any entry of that group (comparison keeps entry tag,
  original rect, source kind, both new codes and the signed amount --
  never netted);
* for every touched group, every member's most recent earlier-round
  effective group with expected entries is itself signable and posted at a
  strictly smaller commit (two rounds inside one transaction do not count
  as ordered).

When any condition fails, no group of that transaction is signed off;
unrelated groups are not held back.

Resource model
--------------
Memory grows with the *plan* only (groups, expected entries, plus a fixed
sorting buffer).  The log is streamed once into a temporary external sort
and then scanned a constant number of times; memory never grows with the
number of receipt lines, the number of transactions, or the size of a
single transaction, and the original slices are never re-scanned per
transaction.
"""

from __future__ import annotations

import heapq
import json
import os
import tempfile
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

PACKAGE_FORMAT = "correction_package/v1"
PLAN_FORMAT = "verification_plan/v1"
CONCLUSION_FORMAT = "receipt_verification/v1"

LOG_FIELDS = {
    "commit", "line", "line_count", "round",
    "entry", "book", "rect", "kind", "new_pair", "amount",
}

# Group verdicts reported in the conclusion.
SIGNABLE = "signable"
NO_RECEIPT = "no_receipt"
RECEIPT_NOT_SIGNABLE = "receipt_not_signable"

_MISSING_EVIDENCE_CAP = 5


class ReceiptError(Exception):
    """Invalid plan or unverifiable log prefix (never a business finding)."""


# --------------------------------------------------------------------- helpers
def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _content_key(entry: dict) -> tuple:
    """Identity of one expected/actual entry for multiset comparison."""
    pair = entry["new_pair"]
    return (
        entry["entry"],
        entry["book"],
        entry["rect"][0],
        entry["rect"][1],
        entry["kind"],
        pair["X"] if pair else None,
        pair["Y"] if pair else None,
        entry["amount"],
    )


def _public_content(content: dict) -> dict:
    """Evidence view of a log line: entry fields without bookkeeping."""
    return {k: v for k, v in content.items() if k != "line_count"}


# ------------------------------------------------------------------------ plan
class Group:
    """One effective correction group that carries expected entries."""

    __slots__ = (
        "gid", "round_id", "round_index", "members", "expected",
        "expected_list", "prereqs",
        "receipts", "first_commit", "second_commit",
        "extra_count", "first_extra", "remaining",
        "matched", "status",
    )

    def __init__(self, gid: int, round_id: str, round_index: int,
                 members: List[str], expected: Dict[tuple, int],
                 expected_list: List[dict]):
        self.gid = gid
        self.round_id = round_id
        self.round_index = round_index
        self.members = list(members)
        self.expected = dict(expected)          # content key -> count
        self.expected_list = list(expected_list)  # package order, for evidence
        self.prereqs: List[int] = []
        self.reset()

    def reset(self) -> None:
        """Clear the actual-side aggregation (a plan is reusable)."""
        self.receipts = 0
        self.first_commit: Optional[int] = None
        self.second_commit: Optional[int] = None
        self.extra_count = 0
        self.first_extra: Optional[Tuple[int, int, dict]] = None
        self.remaining: Dict[tuple, int] = dict(self.expected)
        self.matched = False
        self.status: Optional[str] = None


class Plan:
    """Parsed verification plan: rounds in approval order."""

    def __init__(self):
        self.round_ids: List[str] = []
        self.groups: List[Group] = []           # effective groups with entries
        self.attribution: Dict[Tuple[str, str], int] = {}
        self.membership: Dict[Tuple[str, str], Tuple[bool, bool]] = {}
        self.round_group_gids: List[List[int]] = []

    def finish(self) -> None:
        """Prerequisites: each member's latest earlier group with entries."""
        last: Dict[str, int] = {}
        for gids in self.round_group_gids:
            for gid in gids:
                group = self.groups[gid]
                pre = {last[m] for m in group.members if m in last}
                group.prereqs = sorted(pre)
            for gid in gids:
                for member in self.groups[gid].members:
                    last[member] = gid


def _group_ref(group: Group) -> dict:
    return {"round": group.round_id, "members": list(group.members)}


def _parse_package_entry(rec, where: str, expected_entry: str) -> dict:
    if not isinstance(rec, dict):
        raise ReceiptError(f"{where}: entry must be an object")
    if rec.get("entry") != expected_entry:
        raise ReceiptError(
            f"{where}: entry tag must be {expected_entry!r} in this list"
        )
    book = rec.get("book")
    if not isinstance(book, str) or not book:
        raise ReceiptError(f"{where}: 'book' must be a non-empty string")
    rect = rec.get("rect")
    if not (isinstance(rect, list) and len(rect) == 2
            and all(isinstance(v, str) and v for v in rect)):
        raise ReceiptError(f"{where}: 'rect' must be [x_node, y_node]")
    kind = rec.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ReceiptError(f"{where}: 'kind' must be a non-empty string")
    pair = rec.get("new_pair")
    if not (isinstance(pair, dict) and set(pair) == {"X", "Y"}
            and all(isinstance(pair[a], str) and pair[a] for a in ("X", "Y"))):
        raise ReceiptError(
            f"{where}: 'new_pair' must be an object with 'X' and 'Y' codes"
        )
    amount = rec.get("amount")
    if not _is_int(amount):
        raise ReceiptError(f"{where}: 'amount' must be an integer")
    return {
        "entry": expected_entry,
        "book": book,
        "rect": [rect[0], rect[1]],
        "kind": kind,
        "new_pair": {"X": pair["X"], "Y": pair["Y"]},
        "amount": amount,
    }


def _parse_package(plan: Plan, raw, path: str, round_id: str,
                   round_index: int) -> None:
    where = f"round {round_id!r} package {path}"
    if not isinstance(raw, dict) or raw.get("format") != PACKAGE_FORMAT:
        raise ReceiptError(f"{where}: not a {PACKAGE_FORMAT} file")
    groups_raw = raw.get("groups")
    reversals = raw.get("reversals")
    postings = raw.get("postings")
    if not (isinstance(groups_raw, list) and isinstance(reversals, list)
            and isinstance(postings, list)):
        raise ReceiptError(
            f"{where}: needs 'groups', 'reversals' and 'postings' lists"
        )

    round_groups: List[Tuple[List[str], bool]] = []
    member_owner: Dict[str, int] = {}
    for gidx, graw in enumerate(groups_raw):
        gwhere = f"{where}: group #{gidx + 1}"
        if not isinstance(graw, dict):
            raise ReceiptError(f"{gwhere}: must be an object")
        members = graw.get("members")
        if not (isinstance(members, list) and members
                and all(isinstance(m, str) and m for m in members)):
            raise ReceiptError(
                f"{gwhere}: 'members' must be a non-empty list of book ids"
            )
        if len(set(members)) != len(members):
            raise ReceiptError(f"{gwhere}: duplicate members")
        effective = graw.get("effective")
        if not isinstance(effective, bool):
            raise ReceiptError(f"{gwhere}: 'effective' must be a boolean")
        for member in members:
            if member in member_owner:
                raise ReceiptError(
                    f"{where}: book {member!r} appears in two groups; "
                    "entry attribution is ambiguous"
                )
            member_owner[member] = gidx
        round_groups.append((list(members), effective))

    expected: List[Dict[tuple, int]] = [dict() for _ in round_groups]
    expected_list: List[List[dict]] = [[] for _ in round_groups]
    for which, records, tag in (
        ("reversals", reversals, "reversal"),
        ("postings", postings, "posting"),
    ):
        for ridx, rec in enumerate(records):
            ewhere = f"{where}: {which}[{ridx}]"
            entry = _parse_package_entry(rec, ewhere, tag)
            owner = member_owner.get(entry["book"])
            if owner is None:
                raise ReceiptError(
                    f"{ewhere}: book {entry['book']!r} is not a member of "
                    "any group in this package"
                )
            if not round_groups[owner][1]:
                raise ReceiptError(
                    f"{ewhere}: book {entry['book']!r} belongs to a "
                    "non-effective group; a held group must not carry entries"
                )
            key = _content_key(entry)
            expected[owner][key] = expected[owner].get(key, 0) + 1
            expected_list[owner].append(entry)

    gids_this_round: List[int] = []
    for gidx, (members, effective) in enumerate(round_groups):
        has_entries = bool(expected[gidx])
        for member in members:
            plan.membership[(round_id, member)] = (effective, has_entries)
        if not (effective and has_entries):
            continue
        group = Group(
            gid=len(plan.groups),
            round_id=round_id,
            round_index=round_index,
            members=members,
            expected=expected[gidx],
            expected_list=expected_list[gidx],
        )
        plan.groups.append(group)
        gids_this_round.append(group.gid)
        for member in members:
            plan.attribution[(round_id, member)] = group.gid
    plan.round_group_gids.append(gids_this_round)


def load_plan(raw, base_dir: str) -> Plan:
    """Validate a plan document; ``ReceiptError`` when not interpretable."""
    if isinstance(raw, dict):
        fmt = raw.get("format")
        if fmt is not None and fmt != PLAN_FORMAT:
            raise ReceiptError(
                f"plan format must be {PLAN_FORMAT!r}, got {fmt!r}"
            )
        rounds_raw = raw.get("rounds")
    elif isinstance(raw, list):
        rounds_raw = raw
    else:
        raise ReceiptError(
            "plan must be a list of rounds or an object with 'rounds'"
        )
    if not isinstance(rounds_raw, list):
        raise ReceiptError("plan 'rounds' must be a list")

    plan = Plan()
    seen_rounds = set()
    for rindex, item in enumerate(rounds_raw):
        rwhere = f"plan round #{rindex + 1}"
        if not isinstance(item, dict):
            raise ReceiptError(f"{rwhere}: must be an object")
        round_id = item.get("round")
        if not isinstance(round_id, str) or not round_id:
            raise ReceiptError(f"{rwhere}: 'round' must be a non-empty string")
        if round_id in seen_rounds:
            raise ReceiptError(f"duplicate round id {round_id!r}")
        seen_rounds.add(round_id)
        pkg_rel = item.get("package")
        if not isinstance(pkg_rel, str) or not pkg_rel:
            raise ReceiptError(
                f"plan round {round_id!r}: 'package' must be a non-empty string"
            )
        pkg_path = pkg_rel if os.path.isabs(pkg_rel) else os.path.join(
            base_dir, pkg_rel
        )
        try:
            with open(pkg_path, "r", encoding="utf-8") as fh:
                pkg_raw = json.load(fh)
        except FileNotFoundError:
            raise ReceiptError(f"plan round {round_id!r}: package file "
                               f"not found: {pkg_path}")
        except json.JSONDecodeError as exc:
            raise ReceiptError(
                f"plan round {round_id!r}: package {pkg_path}: "
                f"invalid JSON: {exc}"
            )
        _parse_package(plan, pkg_raw, pkg_path, round_id, rindex)
        plan.round_ids.append(round_id)
    plan.finish()
    return plan


def load_plan_file(path: str) -> Plan:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ReceiptError(f"plan file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"plan file {path}: invalid JSON: {exc}")
    base_dir = os.path.dirname(os.path.abspath(path))
    return load_plan(raw, base_dir)


# -------------------------------------------------------------- log record I/O
def _normalize_log_record(rec, cutoff: int, where: str) -> Optional[list]:
    """Validate one export line; None when beyond the cutoff (not evidence)."""
    if not isinstance(rec, dict):
        raise ReceiptError(f"{where}: log line must be a JSON object")
    commit = rec.get("commit")
    if not _is_int(commit) or commit < 1:
        raise ReceiptError(f"{where}: commit must be a positive integer")
    if commit > cutoff:
        return None
    keys = set(rec)
    if keys != LOG_FIELDS:
        missing = sorted(LOG_FIELDS - keys)
        extra = sorted(keys - LOG_FIELDS)
        raise ReceiptError(
            f"{where}: log line fields mismatch "
            f"(missing {missing}, extra {extra})"
        )
    line = rec["line"]
    line_count = rec["line_count"]
    if not _is_int(line) or line < 1:
        raise ReceiptError(f"{where}: line must be a positive integer")
    if not _is_int(line_count) or line_count < 1:
        raise ReceiptError(f"{where}: line_count must be a positive integer")
    if line > line_count:
        raise ReceiptError(f"{where}: line {line} exceeds line_count "
                           f"{line_count}")
    for field in ("round", "entry", "book", "kind"):
        if not isinstance(rec[field], str) or not rec[field]:
            raise ReceiptError(f"{where}: {field!r} must be a non-empty string")
    rect = rec["rect"]
    if not (isinstance(rect, list) and len(rect) == 2
            and all(isinstance(v, str) and v for v in rect)):
        raise ReceiptError(f"{where}: 'rect' must be [x_node, y_node]")
    pair = rec["new_pair"]
    if pair is not None and not (
        isinstance(pair, dict) and set(pair) == {"X", "Y"}
        and all(isinstance(pair[a], str) and pair[a] for a in ("X", "Y"))
    ):
        raise ReceiptError(
            f"{where}: 'new_pair' must be null or an object with 'X' and 'Y'"
        )
    amount = rec["amount"]
    if not _is_int(amount):
        raise ReceiptError(f"{where}: 'amount' must be an integer")
    content = {
        "line_count": line_count,
        "round": rec["round"],
        "entry": rec["entry"],
        "book": rec["book"],
        "rect": [rect[0], rect[1]],
        "kind": rec["kind"],
        "new_pair": pair,
        "amount": amount,
    }
    canonical = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return [commit, line, canonical]


# --------------------------------------------------------------- external sort
class _ExternalSorter:
    """Chunked external sort with bounded memory and bounded open files."""

    def __init__(self, workdir: str, chunk_size: int, fanin: int):
        self.workdir = workdir
        self.chunk_size = max(2, chunk_size)
        self.fanin = max(2, fanin)
        self._seq = 0

    def sort(self, records: Iterable[list]) -> str:
        runs: List[str] = []
        buf: List[list] = []
        for rec in records:
            buf.append(rec)
            if len(buf) >= self.chunk_size:
                runs.append(self._dump(buf))
                buf = []
        if buf or not runs:
            runs.append(self._dump(buf))
        while len(runs) > 1:
            merged: List[str] = []
            for i in range(0, len(runs), self.fanin):
                batch = runs[i:i + self.fanin]
                if len(batch) == 1:
                    merged.append(batch[0])
                    continue
                merged.append(self._merge(batch))
                for path in batch:
                    os.unlink(path)
            runs = merged
        return runs[0]

    def _dump(self, buf: List[list]) -> str:
        buf.sort()
        path = self._next_path()
        with open(path, "w", encoding="utf-8") as fh:
            for rec in buf:
                fh.write(json.dumps(rec, ensure_ascii=False,
                                    separators=(",", ":")))
                fh.write("\n")
        return path

    def _merge(self, paths: List[str]) -> str:
        out = self._next_path()
        with open(out, "w", encoding="utf-8") as fh:
            for rec in heapq.merge(*(_iter_run(p) for p in paths)):
                fh.write(json.dumps(rec, ensure_ascii=False,
                                    separators=(",", ":")))
                fh.write("\n")
        return out

    def _next_path(self) -> str:
        self._seq += 1
        return os.path.join(self.workdir, f"run-{self._seq:06d}.ndjson")


def _iter_run(path: str) -> Iterator[list]:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)


# -------------------------------------------------------------------- verifier
class Verifier:
    """Runs the verification phases; holds only plan-sized state."""

    def __init__(self, plan: Plan, log_paths: List[str], cutoff: int, *,
                 sort_chunk: int = 65536, sort_fanin: int = 64):
        if not _is_int(cutoff) or cutoff < 0:
            raise ReceiptError("cutoff must be a non-negative integer")
        self.plan = plan
        self.log_paths = list(log_paths)
        self.cutoff = cutoff
        self.sort_chunk = sort_chunk
        self.sort_fanin = sort_fanin
        self._tmpdir = tempfile.TemporaryDirectory(prefix="cube-receipt-")
        self._sorted_path: Optional[str] = None
        self._lines = 0
        self._duplicates = 0
        self._interesting: List[int] = []
        self._interesting_set = set()
        self._groups_of: Dict[int, set] = {}
        self._c1_of: Dict[int, Tuple[int, dict]] = {}
        self._signable: Dict[int, bool] = {}
        self._reasons: Dict[int, List[dict]] = {}
        self._tx_signable: Optional[int] = None
        self._tx_failed: Optional[int] = None
        self._ran = False

    # ------------------------------------------------------------- lifecycle
    def __enter__(self) -> "Verifier":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    # ---------------------------------------------------------------- phases
    def run(self) -> None:
        """Ingest, check and evaluate; raises ReceiptError if unverifiable."""
        if self._ran:
            raise ReceiptError("verifier already ran")
        self._ran = True
        for group in self.plan.groups:
            group.reset()
        sorter = _ExternalSorter(
            self._tmpdir.name, self.sort_chunk, self.sort_fanin
        )
        self._sorted_path = sorter.sort(self._ingest_records())
        self._analyze()
        self._compute_matched()
        self._scan_interesting()
        self._evaluate()
        self._compute_reasons()

    def _ingest_records(self) -> Iterator[list]:
        for path in self.log_paths:
            try:
                fh = open(path, "r", encoding="utf-8")
            except FileNotFoundError:
                raise ReceiptError(f"log slice not found: {path}")
            with fh:
                for lineno, raw in enumerate(fh, 1):
                    text = raw.strip()
                    if not text:
                        continue
                    try:
                        rec = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise ReceiptError(
                            f"{path}:{lineno}: invalid JSON line: {exc}"
                        ) from exc
                    norm = _normalize_log_record(
                        rec, self.cutoff, f"{path}:{lineno}"
                    )
                    if norm is not None:
                        yield norm

    def _iter_sorted(self) -> Iterator[Tuple[int, int, str]]:
        assert self._sorted_path is not None
        with open(self._sorted_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                commit, line, canonical = json.loads(raw)
                yield commit, line, canonical

    def _iter_records(self) -> Iterator[Tuple[int, int, dict]]:
        """Deduplicated records in (commit, line) order."""
        prev_pos = None
        for commit, line, canonical in self._iter_sorted():
            pos = (commit, line)
            if pos == prev_pos:
                continue
            prev_pos = pos
            yield commit, line, json.loads(canonical)

    def _analyze(self) -> None:
        """Prefix integrity checks plus per-group receipt aggregation."""
        expected_commit = 1
        cur_commit: Optional[int] = None
        cur_line_count = 0
        next_line = 1
        prev_pos = None
        prev_canonical = None
        for commit, line, canonical in self._iter_sorted():
            pos = (commit, line)
            if pos == prev_pos:
                if canonical != prev_canonical:
                    raise ReceiptError(
                        f"conflicting records at commit {commit} line {line}"
                    )
                self._duplicates += 1
                continue
            if commit != cur_commit:
                if cur_commit is not None and next_line != cur_line_count + 1:
                    raise ReceiptError(
                        f"commit {cur_commit}: missing line(s); "
                        f"line_count is {cur_line_count}"
                    )
                if commit != expected_commit:
                    raise ReceiptError(
                        f"missing commit {expected_commit} in the prefix "
                        f"(next recorded commit is {commit})"
                    )
                expected_commit += 1
                cur_commit = commit
                cur_line_count = -1
                next_line = 1
            content = json.loads(canonical)
            line_count = content["line_count"]
            if cur_line_count == -1:
                cur_line_count = line_count
            elif line_count != cur_line_count:
                raise ReceiptError(
                    f"commit {commit}: inconsistent line_count "
                    f"{line_count} != {cur_line_count}"
                )
            if line != next_line:
                raise ReceiptError(
                    f"commit {commit}: missing line {next_line} "
                    f"(recorded line is {line})"
                )
            next_line += 1
            prev_pos, prev_canonical = pos, canonical
            self._lines += 1
            gid = self.plan.attribution.get((content["round"], content["book"]))
            if gid is not None:
                self._absorb(self.plan.groups[gid], commit, line, content)
        if cur_commit is None:
            if self.cutoff != 0:
                raise ReceiptError(
                    f"missing commits 1..{self.cutoff}: the prefix is empty"
                )
            return
        if next_line != cur_line_count + 1:
            raise ReceiptError(
                f"commit {cur_commit}: missing line(s); "
                f"line_count is {cur_line_count}"
            )
        if expected_commit != self.cutoff + 1:
            raise ReceiptError(
                f"missing commits {expected_commit}..{self.cutoff} "
                "in the prefix"
            )

    @staticmethod
    def _absorb(group: Group, commit: int, line: int, content: dict) -> None:
        group.receipts += 1
        if group.first_commit is None:
            group.first_commit = commit
        elif commit != group.first_commit and group.second_commit is None:
            group.second_commit = commit
        key = _content_key(content)
        if group.remaining.get(key, 0) > 0:
            group.remaining[key] -= 1
        else:
            group.extra_count += 1
            if group.first_extra is None:
                group.first_extra = (commit, line, content)

    def _compute_matched(self) -> None:
        for group in self.plan.groups:
            group.matched = (
                group.receipts > 0
                and group.second_commit is None
                and group.extra_count == 0
                and not any(group.remaining.values())
            )
        self._interesting = sorted(
            {g.first_commit for g in self.plan.groups if g.matched}
        )
        self._interesting_set = set(self._interesting)

    def _scan_interesting(self) -> None:
        """Group membership and first unattributable line per candidate."""
        if not self._interesting:
            return
        attribution = self.plan.attribution
        for commit, line, content in self._iter_records():
            if commit not in self._interesting_set:
                continue
            gid = attribution.get((content["round"], content["book"]))
            if gid is None:
                if commit not in self._c1_of:
                    self._c1_of[commit] = (line, content)
            else:
                self._groups_of.setdefault(commit, set()).add(gid)
        for commit in self._interesting:
            self._groups_of.setdefault(commit, set())

    def _prereq_unmet(self, prereq: Group, commit: int) -> bool:
        if not prereq.matched:
            return True
        if prereq.first_commit >= commit:
            return True
        return not self._signable[prereq.gid]

    def _evaluate(self) -> None:
        groups = self.plan.groups
        self._signable = {g.gid: False for g in groups}
        matched = sorted(
            (g for g in groups if g.matched), key=lambda g: g.first_commit
        )
        for group in matched:
            commit = group.first_commit
            ok = self._c1_of.get(commit) is None
            if ok:
                for gid2 in sorted(self._groups_of.get(commit, ())):
                    other = groups[gid2]
                    if not other.matched:
                        ok = False
                        break
                    if any(self._prereq_unmet(groups[p], commit)
                           for p in other.prereqs):
                        ok = False
                        break
            self._signable[group.gid] = ok

    def _compute_reasons(self) -> None:
        for group in self.plan.groups:
            if group.receipts == 0:
                group.status = NO_RECEIPT
            elif self._signable[group.gid]:
                group.status = SIGNABLE
            else:
                group.status = RECEIPT_NOT_SIGNABLE
        for group in self.plan.groups:
            if group.status == RECEIPT_NOT_SIGNABLE:
                self._reasons[group.gid] = self._group_reasons(group)

    # ---------------------------------------------------------------- reasons
    def _group_reasons(self, group: Group) -> List[dict]:
        ref = _group_ref(group)
        commit = group.first_commit
        if group.second_commit is not None:
            return [{
                "reason": "group_entries_in_multiple_commits",
                "group": ref,
                "commits": [group.first_commit, group.second_commit],
            }]
        reasons: List[dict] = []
        if group.extra_count:
            x_commit, x_line, x_content = group.first_extra
            reasons.append({
                "reason": "unexpected_entry",
                "group": ref,
                "commit": x_commit,
                "line": x_line,
                "content": _public_content(x_content),
            })
        missing = [
            (entry, group.remaining[_content_key(entry)])
            for entry in group.expected_list
            if group.remaining[_content_key(entry)] > 0
        ]
        if missing:
            reason = {
                "reason": "missing_entries",
                "group": ref,
                "missing_count": sum(n for _, n in missing),
                "entries": [
                    {"entry": entry, "missing": count}
                    for entry, count in missing[:_MISSING_EVIDENCE_CAP]
                ],
            }
            if len(missing) > _MISSING_EVIDENCE_CAP:
                reason["truncated"] = True
            reasons.append(reason)
        if reasons:
            return reasons
        c1 = self._c1_of.get(commit)
        if c1 is not None:
            line, content = c1
            return [{
                "reason": "transaction_has_unattributable_line",
                "group": ref,
                "commit": commit,
                "line": line,
                "content": _public_content(content),
            }]
        for p in group.prereqs:
            prereq = self.plan.groups[p]
            if self._prereq_unmet(prereq, commit):
                if prereq.matched and prereq.first_commit >= commit:
                    return [{
                        "reason": "prerequisite_commit_not_earlier",
                        "group": ref,
                        "prerequisite": _group_ref(prereq),
                        "prerequisite_commit": prereq.first_commit,
                        "commit": commit,
                    }]
                return [{
                    "reason": "prerequisite_not_signable",
                    "group": ref,
                    "prerequisite": _group_ref(prereq),
                    "prerequisite_status": prereq.status,
                }]
        for gid2 in sorted(self._groups_of.get(commit, ())):
            other = self.plan.groups[gid2]
            if not other.matched:
                return [{
                    "reason": "co_transaction_group_not_matched",
                    "group": ref,
                    "commit": commit,
                    "co_group": _group_ref(other),
                }]
        for gid2 in sorted(self._groups_of.get(commit, ())):
            other = self.plan.groups[gid2]
            for p in other.prereqs:
                prereq = self.plan.groups[p]
                if self._prereq_unmet(prereq, commit):
                    return [{
                        "reason": "co_transaction_group_prerequisite_unmet",
                        "group": ref,
                        "commit": commit,
                        "co_group": _group_ref(other),
                        "prerequisite": _group_ref(prereq),
                    }]
        raise AssertionError(
            "non-signable group without an explainable cause"
        )

    def _attribution_detail(self, round_id: str, book: str) -> str:
        if round_id not in set(self.plan.round_ids):
            return "round not in plan"
        info = self.plan.membership.get((round_id, book))
        if info is None:
            return "book is not a member of any group in this round"
        effective, has_entries = info
        if not effective:
            return "the book's group is not effective"
        if not has_entries:
            return "the book's group has no expected entries"
        return "line is not attributable"

    # ---------------------------------------------------------------- output
    def groups_conclusion(self) -> List[dict]:
        out = []
        for group in self.plan.groups:
            entry = {
                "round": group.round_id,
                "members": list(group.members),
                "status": group.status,
                "expected_entries": sum(group.expected.values()),
            }
            if group.status == SIGNABLE:
                entry["commit"] = group.first_commit
            elif group.status == RECEIPT_NOT_SIGNABLE:
                entry["reasons"] = self._reasons[group.gid]
            out.append(entry)
        return out

    def iter_failed_transactions(self) -> Iterator[dict]:
        """One entry per failed transaction, in commit order (streaming)."""
        tx_signable = 0
        tx_failed = 0
        cur_commit: Optional[int] = None
        cur_gids: set = set()
        cur_c1: Optional[Tuple[int, dict]] = None

        def flush() -> Optional[dict]:
            nonlocal tx_signable, tx_failed
            if cur_commit is None:
                return None
            ok = (
                cur_commit in self._interesting_set
                and cur_c1 is None
                and all(self._signable[gid] for gid in cur_gids)
            )
            if ok:
                tx_signable += 1
                return None
            tx_failed += 1
            reasons: List[dict] = []
            if cur_c1 is not None:
                line, content = cur_c1
                reasons.append({
                    "reason": "line_not_attributable",
                    "line": line,
                    "detail": self._attribution_detail(
                        content["round"], content["book"]
                    ),
                    "content": _public_content(content),
                })
            for gid in sorted(cur_gids):
                reasons.extend(self._reasons.get(gid, []))
            return {"commit": cur_commit, "reasons": reasons}

        for commit, line, content in self._iter_records():
            if commit != cur_commit:
                emitted = flush()
                if emitted is not None:
                    yield emitted
                cur_commit = commit
                cur_gids = set()
                cur_c1 = None
            gid = self.plan.attribution.get((content["round"], content["book"]))
            if gid is None:
                if cur_c1 is None:
                    cur_c1 = (line, content)
            else:
                cur_gids.add(gid)
        emitted = flush()
        if emitted is not None:
            yield emitted
        self._tx_signable = tx_signable
        self._tx_failed = tx_failed

    def stats(self) -> dict:
        groups = self.plan.groups
        return {
            "cutoff": self.cutoff,
            "commits_in_prefix": self.cutoff,
            "receipt_lines": self._lines,
            "duplicate_export_lines": self._duplicates,
            "groups_with_expected_entries": len(groups),
            "groups_signable": sum(1 for g in groups if g.status == SIGNABLE),
            "groups_no_receipt": sum(1 for g in groups if g.status == NO_RECEIPT),
            "groups_receipt_not_signable": sum(
                1 for g in groups if g.status == RECEIPT_NOT_SIGNABLE
            ),
            "transactions_signable": self._tx_signable,
            "transactions_failed": self._tx_failed,
        }


def verify_receipts(plan: Plan, log_paths: List[str], cutoff: int, **kw) -> dict:
    """Materialized conclusion (for tests and small inputs)."""
    with Verifier(plan, log_paths, cutoff, **kw) as verifier:
        verifier.run()
        groups = verifier.groups_conclusion()
        failed = list(verifier.iter_failed_transactions())
        return {
            "format": CONCLUSION_FORMAT,
            "cutoff": cutoff,
            "plan_rounds": list(verifier.plan.round_ids),
            "groups": groups,
            "failed_transactions": failed,
            "stats": verifier.stats(),
        }


def write_conclusion(verifier: Verifier, fh) -> None:
    """Stream the conclusion JSON; failed transactions never sit in memory."""

    def dump(obj) -> str:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)

    fh.write("{\n")
    fh.write(f'  "format": {dump(CONCLUSION_FORMAT)},\n')
    fh.write(f'  "cutoff": {verifier.cutoff},\n')
    fh.write(f'  "plan_rounds": {dump(verifier.plan.round_ids)},\n')
    groups = verifier.groups_conclusion()
    fh.write('  "groups": [')
    for index, entry in enumerate(groups):
        fh.write(",\n" if index else "\n")
        fh.write("    " + dump(entry))
    fh.write("\n  ],\n" if groups else "],\n")
    fh.write('  "failed_transactions": [')
    first = True
    for failed in verifier.iter_failed_transactions():
        fh.write(",\n" if not first else "\n")
        fh.write("    " + dump(failed))
        first = False
    fh.write("\n  ],\n" if not first else "],\n")
    fh.write("  \"stats\": ")
    fh.write(json.dumps(verifier.stats(), ensure_ascii=False,
                        sort_keys=True, indent=2))
    fh.write("\n}\n")
