"""Receipt sign-off verification tests (the ``verify`` command).

Covers the acceptance rules:

* a group whose entries are split across two transactions can never be
  signed off, even when amounts and entries align in total;
* prerequisite groups (each member's most recent earlier-round group with
  entries) must be signable and posted at a strictly smaller commit --
  moving the earlier-round group's commit flips the later-round dependent
  transaction, including the other groups sharing that transaction, while
  a genuinely independent group is unaffected;
* export overlap (identical record at the same commit/line) is not a
  duplicate posting, but the same content at a different position is;
* an incomplete or conflicting prefix is an error (exit 2) and never
  replaces an existing conclusion; business discrepancies still deliver
  one;
* zero amounts are expected entries; effective-but-empty groups need no
  receipt and are no prerequisites; held groups must not receive entries;
* determinism under re-sliced / reordered / overlap-duplicated exports;
* large generated logs are verified with a bounded-memory external sort.

Run:  python3 -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cube.approval import build_correction_package  # noqa: E402
from cube.loader import load_handover  # noqa: E402
from cube.mapping import load_mapping  # noqa: E402
from cube.receipt import (  # noqa: E402
    CONCLUSION_FORMAT,
    ReceiptError,
    load_plan,
    verify_receipts,
)

APPROVAL_DIR = os.path.join(ROOT, "examples", "approval")


def ent(book, amount, entry, x="x1", y="y1", kind="exact_terminal",
        nx="NX", ny="NY1"):
    return {
        "entry": entry,
        "book": book,
        "rect": [x, y],
        "kind": kind,
        "new_pair": {"X": nx, "Y": ny},
        "amount": amount,
    }


def pkg(groups, entries):
    """groups: list of (label, members, effective); entries: list of ent().

    The label is only a test-side hint; the package itself carries just
    members and the effective flag.
    """
    return {
        "format": "correction_package/v1",
        "groups": [
            {
                "members": list(members),
                "effective": effective,
                "missing_approvals": [] if effective else ["X"],
                "withheld_books": [],
            }
            for _label, members, effective in groups
        ],
        "reversals": [e for e in entries if e["entry"] == "reversal"],
        "postings": [e for e in entries if e["entry"] == "posting"],
        "totals": {
            "reversals": sum(e["amount"] for e in entries
                             if e["entry"] == "reversal"),
            "postings": sum(e["amount"] for e in entries
                            if e["entry"] == "posting"),
        },
    }


def line(commit, ln, lc, rnd, e):
    return {
        "commit": commit,
        "line": ln,
        "line_count": lc,
        "round": rnd,
        "entry": e["entry"],
        "book": e["book"],
        "rect": list(e["rect"]),
        "kind": e["kind"],
        "new_pair": e["new_pair"],
        "amount": e["amount"],
    }


def lines_of(commit, rnd, entries):
    return [line(commit, i + 1, len(entries), rnd, e)
            for i, e in enumerate(entries)]


class ReceiptTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cube-receipt-test-")
        self.dir = self._tmp.name
        self._count = 0

    def tearDown(self):
        self._tmp.cleanup()

    def _path(self, name):
        self._count += 1
        return os.path.join(self.dir, f"{self._count:03d}-{name}")

    def write_json(self, name, obj):
        path = self._path(name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path

    def write_log(self, name, lines):
        path = self._path(name)
        with open(path, "w", encoding="utf-8") as fh:
            for entry in lines:
                fh.write(json.dumps(entry) + "\n")
        return path

    def make_plan(self, rounds):
        """rounds: list of (round_id, package object) -> plan file path."""
        items = []
        for round_id, package in rounds:
            pkg_path = self.write_json("pkg.json", package)
            items.append({"round": round_id,
                          "package": os.path.basename(pkg_path)})
        plan_dir = os.path.dirname(items[0]["package"]) if items else self.dir
        # packages and plan share one directory so relative paths resolve
        plan = {"format": "verification_plan/v1", "rounds": items}
        plan_path = os.path.join(plan_dir or self.dir, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        return plan_path

    def verify(self, plan_path, log_paths, cutoff, **kw):
        from cube.receipt import load_plan_file
        return verify_receipts(load_plan_file(plan_path), log_paths,
                               cutoff, **kw)

    @staticmethod
    def group(conclusion, round_id, members):
        members = sorted(members)
        for g in conclusion["groups"]:
            if g["round"] == round_id and sorted(g["members"]) == members:
                return g
        raise AssertionError(f"group {round_id}/{members} not in conclusion")

    @staticmethod
    def failed(conclusion, commit):
        for tx in conclusion["failed_transactions"]:
            if tx["commit"] == commit:
                return tx
        raise AssertionError(f"transaction {commit} not failed")


# A reusable two-round plan: G1 {A,B} in R1, G3 {D} in R1 (independent
# control), G2 {B,C} in R2 (prerequisite G1 via B), held group {E} in R2.
G1 = [ent("A", -100, "reversal"), ent("A", 120, "posting"),
      ent("B", 30, "reversal", x="x2"), ent("B", -25, "posting", x="x2")]
G3 = [ent("D", 7, "posting", y="y3", ny="NY3")]
G2 = [ent("B", 25, "reversal", x="x2"), ent("B", -20, "posting", x="x2"),
      ent("C", 40, "posting", x="x2", y="y2", ny="NY2")]
G5 = [ent("F", 9, "posting", y="y4", ny="NY4")]


class TestBasicSignoff(ReceiptTestCase):
    def test_all_groups_sign_off_with_exact_entries(self):
        plan = self.make_plan([
            ("R1", pkg([("AB", ["A", "B"], True), ("D", ["D"], True)],
                       G1 + G3)),
            ("R2", pkg([("BC", ["B", "C"], True), ("E", ["E"], False)], G2)),
        ])
        log = self.write_log("log.ndjson",
                             lines_of(1, "R1", G1)
                             + lines_of(2, "R2", G2)
                             + lines_of(3, "R1", G3))
        out = self.verify(plan, [log], 3)
        self.assertEqual(out["format"], CONCLUSION_FORMAT)
        self.assertEqual(out["failed_transactions"], [])
        self.assertEqual(self.group(out, "R1", ["A", "B"])["status"],
                         "signable")
        self.assertEqual(self.group(out, "R1", ["A", "B"])["commit"], 1)
        self.assertEqual(self.group(out, "R2", ["B", "C"])["commit"], 2)
        self.assertEqual(self.group(out, "R1", ["D"])["commit"], 3)
        stats = out["stats"]
        self.assertEqual(stats["groups_signable"], 3)
        self.assertEqual(stats["transactions_signable"], 3)
        self.assertEqual(stats["transactions_failed"], 0)
        self.assertEqual(stats["receipt_lines"], 8)

    def test_zero_amount_entry_is_expected(self):
        entries = [ent("A", 0, "posting")]
        plan = self.make_plan([("R1", pkg([("A", ["A"], True)], entries))])
        missing = self.verify(plan, [self.write_log("l0", [])], 0)
        # cutoff 0: no receipt at all
        self.assertEqual(self.group(missing, "R1", ["A"])["status"],
                         "no_receipt")
        log = self.write_log("l1", lines_of(1, "R1", entries))
        out = self.verify(plan, [log], 1)
        self.assertEqual(self.group(out, "R1", ["A"])["status"], "signable")

    def test_no_receipt_and_receipt_only_beyond_cutoff(self):
        groups = [("A", ["A"], True), ("B", ["B"], True), ("C", ["C"], True),
                  ("D", ["D"], True), ("E", ["E"], True)]
        entries = [ent("A", 1, "posting"), ent("B", 2, "posting"),
                   ent("C", 3, "posting"), ent("D", 4, "posting"),
                   ent("E", 5, "posting")]
        plan = self.make_plan([("R1", pkg(groups, entries))])
        # The log is consecutive from 1; B's only receipt sits at commit 5.
        log = self.write_log("l", lines_of(1, "R1", [entries[0]])
                             + lines_of(2, "R1", [entries[2]])
                             + lines_of(3, "R1", [entries[3]])
                             + lines_of(4, "R1", [entries[4]])
                             + lines_of(5, "R1", [entries[1]]))
        out = self.verify(plan, [log], 3)  # cutoff below B's only receipt
        self.assertEqual(self.group(out, "R1", ["B"])["status"], "no_receipt")
        self.assertEqual(self.group(out, "R1", ["A"])["status"], "signable")
        self.assertEqual(out["failed_transactions"], [])
        out = self.verify(plan, [log], 5)
        self.assertEqual(self.group(out, "R1", ["B"])["status"], "signable")
        self.assertEqual(out["stats"]["groups_signable"], 5)

    def test_plan_object_is_reusable_across_runs(self):
        from cube.receipt import load_plan_file
        plan_path = self.make_plan([("R1", pkg([("A", ["A"], True)],
                                               [ent("A", 1, "posting")]))])
        plan = load_plan_file(plan_path)
        log = self.write_log("l", lines_of(1, "R1", [ent("A", 1, "posting")]))
        first = verify_receipts(plan, [log], 1)
        second = verify_receipts(plan, [log], 1)
        self.assertEqual(first, second)
        self.assertEqual(first["groups"][0]["status"], "signable")


class TestSplitAcrossTransactions(ReceiptTestCase):
    def test_split_group_never_signs_off_even_when_totals_align(self):
        plan = self.make_plan([
            ("R1", pkg([("AB", ["A", "B"], True), ("D", ["D"], True)],
                       G1 + G3)),
            ("R2", pkg([("BC", ["B", "C"], True)], G2)),
        ])
        # Every expected entry of G1 appears exactly once, totals align --
        # but split across commits 1 and 2.
        log = self.write_log("split", lines_of(1, "R1", G1[:2])
                             + lines_of(2, "R1", G1[2:])
                             + lines_of(3, "R2", G2)
                             + lines_of(4, "R1", G3))
        out = self.verify(plan, [log], 4)
        g1 = self.group(out, "R1", ["A", "B"])
        self.assertEqual(g1["status"], "receipt_not_signable")
        self.assertEqual(g1["reasons"][0]["reason"],
                         "group_entries_in_multiple_commits")
        self.assertEqual(g1["reasons"][0]["commits"], [1, 2])
        # The dependent round-2 group is held back through its prerequisite.
        g2 = self.group(out, "R2", ["B", "C"])
        self.assertEqual(g2["status"], "receipt_not_signable")
        self.assertEqual(g2["reasons"][0]["reason"],
                         "prerequisite_not_signable")
        # The independent control group is unaffected.
        self.assertEqual(self.group(out, "R1", ["D"])["status"], "signable")
        # Both halves of the split are failed transactions with evidence.
        for commit in (1, 2):
            tx = self.failed(out, commit)
            self.assertEqual(tx["reasons"][0]["reason"],
                             "group_entries_in_multiple_commits")
        self.failed(out, 3)  # dependent transaction failed as well
        self.assertEqual(len(out["failed_transactions"]), 3)

    def test_group_in_three_commits_reports_first_two(self):
        entries = [ent("A", 1, "posting"), ent("A", 2, "posting"),
                   ent("A", 3, "posting")]
        plan = self.make_plan([("R1", pkg([("A", ["A"], True)], entries))])
        log = self.write_log("l", lines_of(1, "R1", entries[:1])
                             + lines_of(2, "R1", entries[1:2])
                             + lines_of(3, "R1", entries[2:]))
        out = self.verify(plan, [log], 3)
        g = self.group(out, "R1", ["A"])
        self.assertEqual(g["reasons"][0]["commits"], [1, 2])
        self.assertEqual(len(out["failed_transactions"]), 3)


class TestPrerequisiteOrdering(ReceiptTestCase):
    def _plan(self, with_co_group=True):
        r2_groups = [("BC", ["B", "C"], True)]
        r2_entries = list(G2)
        if with_co_group:
            r2_groups.append(("F", ["F"], True))
            r2_entries += G5
        return self.make_plan([
            ("R1", pkg([("AB", ["A", "B"], True), ("D", ["D"], True)],
                       G1 + G3)),
            ("R2", pkg(r2_groups, r2_entries)),
        ])

    def test_ordered_commits_sign_off(self):
        plan = self._plan()
        log = self.write_log("l", lines_of(1, "R1", G1)
                             + lines_of(2, "R2", G2 + G5)
                             + lines_of(3, "R1", G3))
        out = self.verify(plan, [log], 3)
        for round_id, members in (("R1", ["A", "B"]), ("R2", ["B", "C"]),
                                  ("R2", ["F"]), ("R1", ["D"])):
            self.assertEqual(self.group(out, round_id, members)["status"],
                             "signable", (round_id, members))

    def test_moving_earlier_round_commit_flips_dependent_transaction(self):
        plan = self._plan()
        # Baseline: G1 at commit 1, the round-2 transaction at 2, control at 3.
        good = self.write_log("good", lines_of(1, "R1", G1)
                              + lines_of(2, "R2", G2 + G5)
                              + lines_of(3, "R1", G3))
        out = self.verify(plan, [good], 3)
        self.assertEqual(out["stats"]["groups_signable"], 4)

        # Now G1's actual commit moves to 4, *after* the round-2 transaction.
        # Commits must stay consecutive, so renumber: R2 tx at 1, control at
        # 2, G1 at 3.
        late = self.write_log("late", lines_of(1, "R2", G2 + G5)
                              + lines_of(2, "R1", G3)
                              + lines_of(3, "R1", G1))
        out = self.verify(plan, [late], 3)
        # G1 itself is fine (posted correctly, just late).
        self.assertEqual(self.group(out, "R1", ["A", "B"])["status"],
                         "signable")
        self.assertEqual(self.group(out, "R1", ["A", "B"])["commit"], 3)
        # The dependent round-2 group fails: its prerequisite posted later.
        g2 = self.group(out, "R2", ["B", "C"])
        self.assertEqual(g2["status"], "receipt_not_signable")
        reason = g2["reasons"][0]
        self.assertEqual(reason["reason"], "prerequisite_commit_not_earlier")
        self.assertEqual(reason["prerequisite_commit"], 3)
        self.assertEqual(reason["commit"], 1)
        # The co-transaction group F is sunk together with it.
        g5 = self.group(out, "R2", ["F"])
        self.assertEqual(g5["status"], "receipt_not_signable")
        self.assertEqual(g5["reasons"][0]["reason"],
                         "co_transaction_group_prerequisite_unmet")
        # The genuinely independent control group still signs off.
        self.assertEqual(self.group(out, "R1", ["D"])["status"], "signable")
        # Exactly one failed transaction: the round-2 one.
        self.assertEqual([tx["commit"] for tx in out["failed_transactions"]],
                         [1])
        reasons = self.failed(out, 1)["reasons"]
        self.assertEqual({r["reason"] for r in reasons},
                         {"prerequisite_commit_not_earlier",
                          "co_transaction_group_prerequisite_unmet"})

    def test_same_transaction_two_rounds_is_not_ordered(self):
        plan = self._plan(with_co_group=False)
        # Commit 1 carries all of G1 (round R1) and all of G2 (round R2):
        # one transaction, two rounds -- not an ordered posting.
        both = [line(1, i + 1, 7, "R1", e) for i, e in enumerate(G1)]
        both += [line(1, i + 5, 7, "R2", e) for i, e in enumerate(G2)]
        log = self.write_log("l", both + lines_of(2, "R1", G3))
        out = self.verify(plan, [log], 2)
        g2 = self.group(out, "R2", ["B", "C"])
        self.assertEqual(g2["status"], "receipt_not_signable")
        self.assertEqual(g2["reasons"][0]["reason"],
                         "prerequisite_commit_not_earlier")
        self.assertEqual(g2["reasons"][0]["prerequisite_commit"], 1)
        # G1's own conditions held, but its transaction failed.
        g1 = self.group(out, "R1", ["A", "B"])
        self.assertEqual(g1["status"], "receipt_not_signable")
        self.assertEqual(g1["reasons"][0]["reason"],
                         "co_transaction_group_prerequisite_unmet")

    def test_missing_prerequisite_receipt_blocks_dependent(self):
        plan = self._plan(with_co_group=False)
        log = self.write_log("l", lines_of(1, "R2", G2)
                             + lines_of(2, "R1", G3))
        out = self.verify(plan, [log], 2)
        g1 = self.group(out, "R1", ["A", "B"])
        self.assertEqual(g1["status"], "no_receipt")
        g2 = self.group(out, "R2", ["B", "C"])
        self.assertEqual(g2["reasons"][0]["reason"],
                         "prerequisite_not_signable")
        self.assertEqual(g2["reasons"][0]["prerequisite_status"],
                         "no_receipt")

    def test_unrelated_group_not_blocked_by_missing_earlier_round(self):
        # G2 depends on G1 (via B); G3 (D) shares no members with anyone.
        plan = self._plan(with_co_group=False)
        log = self.write_log("l", lines_of(1, "R2", G2)
                             + lines_of(2, "R1", G3))
        out = self.verify(plan, [log], 2)
        self.assertEqual(self.group(out, "R1", ["D"])["status"], "signable")


class TestEmptyAndHeldGroups(ReceiptTestCase):
    def test_effective_empty_group_needs_no_receipt_and_is_no_prerequisite(self):
        # R1: G1 {A} with entries. R2: {A} effective but entry-free.
        # R3: {A} with entries -> its prerequisite must be R1's group,
        # not the empty R2 group.
        plan = self.make_plan([
            ("R1", pkg([("A", ["A"], True)], [ent("A", 1, "posting")])),
            ("R2", pkg([("A", ["A"], True)], [])),
            ("R3", pkg([("A", ["A"], True)], [ent("A", 2, "posting")])),
        ])
        out = self.verify(plan, [self.write_log("l", [])], 0)
        # Only groups with expected entries are listed.
        self.assertEqual([(g["round"], g["members"]) for g in out["groups"]],
                         [("R1", ["A"]), ("R3", ["A"])])
        self.assertTrue(all(g["status"] == "no_receipt"
                            for g in out["groups"]))
        # R3's group posts while R1's group never did: the prerequisite
        # named in the finding must be R1's group, not the empty R2 group.
        log = self.write_log("l2", lines_of(1, "R3", [ent("A", 2, "posting")]))
        out = self.verify(plan, [log], 1)
        g3 = self.group(out, "R3", ["A"])
        self.assertEqual(g3["status"], "receipt_not_signable")
        self.assertEqual(g3["reasons"][0]["reason"],
                         "prerequisite_not_signable")
        self.assertEqual(g3["reasons"][0]["prerequisite"]["round"], "R1")

    def test_held_group_receiving_entries_fails_the_transaction(self):
        plan = self.make_plan([
            ("R1", pkg([("A", ["A"], True), ("E", ["E"], False)],
                       [ent("A", 1, "posting")])),
        ])
        bad = ent("E", 5, "posting")
        log = self.write_log("l", lines_of(1, "R1", [ent("A", 1, "posting"),
                                                   bad]))
        out = self.verify(plan, [log], 1)
        g = self.group(out, "R1", ["A"])
        self.assertEqual(g["status"], "receipt_not_signable")
        self.assertEqual(g["reasons"][0]["reason"],
                         "transaction_has_unattributable_line")
        tx = self.failed(out, 1)
        self.assertEqual(tx["reasons"][0]["reason"], "line_not_attributable")
        self.assertEqual(tx["reasons"][0]["line"], 2)
        self.assertEqual(tx["reasons"][0]["detail"],
                         "the book's group is not effective")
        self.assertEqual(tx["reasons"][0]["content"]["book"], "E")


class TestExportOverlap(ReceiptTestCase):
    def test_identical_same_position_records_count_once(self):
        plan = self.make_plan([("R1", pkg([("A", ["A"], True)],
                                          [ent("A", 1, "posting")]))])
        good = line(1, 1, 1, "R1", ent("A", 1, "posting"))
        # The same record exported in three overlapping slices.
        logs = [self.write_log(f"s{i}", [good]) for i in range(3)]
        out = self.verify(plan, logs, 1)
        self.assertEqual(self.group(out, "R1", ["A"])["status"], "signable")
        self.assertEqual(out["stats"]["duplicate_export_lines"], 2)
        self.assertEqual(out["stats"]["receipt_lines"], 1)

    def test_same_content_at_different_position_is_a_second_posting(self):
        plan = self.make_plan([("R1", pkg([("A", ["A"], True)],
                                          [ent("A", 1, "posting")]))])
        dup = [line(1, 1, 2, "R1", ent("A", 1, "posting")),
               line(1, 2, 2, "R1", ent("A", 1, "posting"))]
        out = self.verify(plan, [self.write_log("l", dup)], 1)
        g = self.group(out, "R1", ["A"])
        self.assertEqual(g["status"], "receipt_not_signable")
        reason = g["reasons"][0]
        self.assertEqual(reason["reason"], "unexpected_entry")
        self.assertEqual((reason["commit"], reason["line"]), (1, 2))

    def test_overlap_spanning_slices_and_partial_transactions(self):
        plan = self.make_plan([("R1", pkg([("AB", ["A", "B"], True)], G1))])
        full = lines_of(1, "R1", G1)
        # Slice 1 has lines 1-2, slice 2 has lines 2-4 (overlap at line 2).
        s1 = self.write_log("s1", full[:2])
        s2 = self.write_log("s2", full[1:])
        out = self.verify(plan, [s1, s2], 1)
        self.assertEqual(self.group(out, "R1", ["A", "B"])["status"],
                         "signable")
        self.assertEqual(out["stats"]["duplicate_export_lines"], 1)


class TestUnverifiablePrefix(ReceiptTestCase):
    def setUp(self):
        super().setUp()
        self.plan = self.make_plan([("R1", pkg([("A", ["A"], True)],
                                               [ent("A", 1, "posting")]))])

    def assert_unverifiable(self, logs, cutoff=1):
        with self.assertRaises(ReceiptError):
            self.verify(self.plan, logs, cutoff)

    def test_conflicting_content_at_same_position(self):
        a = line(1, 1, 1, "R1", ent("A", 1, "posting"))
        b = line(1, 1, 1, "R1", ent("A", 2, "posting"))
        self.assert_unverifiable([self.write_log("l", [a, b])])

    def test_inconsistent_line_count(self):
        a = line(1, 1, 2, "R1", ent("A", 1, "posting"))
        b = line(1, 2, 3, "R1", ent("A", 1, "posting"))
        self.assert_unverifiable([self.write_log("l", [a, b])])

    def test_missing_line(self):
        a = line(1, 1, 2, "R1", ent("A", 1, "posting"))
        self.assert_unverifiable([self.write_log("l", [a])])

    def test_missing_commit(self):
        a = line(2, 1, 1, "R1", ent("A", 1, "posting"))
        self.assert_unverifiable([self.write_log("l", [a])], cutoff=2)

    def test_empty_prefix_with_nonzero_cutoff(self):
        self.assert_unverifiable([self.write_log("l", [])], cutoff=2)

    def test_malformed_json_line(self):
        path = self._path("bad.ndjson")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"commit": 1, "line": 1, "line_count": 1, ')
        self.assert_unverifiable([path])

    def test_structurally_invalid_line(self):
        bad = {"commit": 1, "line": 1, "line_count": 1, "round": "R1",
               "entry": "posting", "book": "A", "rect": ["x1"],
               "kind": "exact_terminal", "new_pair": {"X": "NX", "Y": "NY1"},
               "amount": 1}
        self.assert_unverifiable([self.write_log("l", [bad])])
        extra = line(1, 1, 1, "R1", ent("A", 1, "posting"))
        extra["surprise"] = True
        self.assert_unverifiable([self.write_log("l", [extra])])

    def test_lines_beyond_cutoff_are_not_validated(self):
        good = line(1, 1, 1, "R1", ent("A", 1, "posting"))
        junk = {"commit": 99, "garbage": True}
        out = self.verify(self.plan, [self.write_log("l", [good, junk])], 1)
        self.assertEqual(self.group(out, "R1", ["A"])["status"], "signable")


class TestMissingAndExtraEntries(ReceiptTestCase):
    def test_missing_entry_is_reported_with_content(self):
        plan = self.make_plan([("R1", pkg([("AB", ["A", "B"], True)], G1))])
        log = self.write_log("l", lines_of(1, "R1", G1[:3]))
        out = self.verify(plan, [log], 1)
        g = self.group(out, "R1", ["A", "B"])
        self.assertEqual(g["status"], "receipt_not_signable")
        reason = g["reasons"][0]
        self.assertEqual(reason["reason"], "missing_entries")
        self.assertEqual(reason["missing_count"], 1)
        self.assertEqual(reason["entries"][0]["entry"]["amount"], -25)

    def test_wrong_amount_is_extra_plus_missing_not_a_net(self):
        plan = self.make_plan([("R1", pkg([("A", ["A"], True)],
                                          [ent("A", 5, "posting")]))])
        log = self.write_log("l", lines_of(1, "R1", [ent("A", 6, "posting")]))
        out = self.verify(plan, [log], 1)
        reasons = {r["reason"] for r in self.group(out, "R1", ["A"])["reasons"]}
        self.assertEqual(reasons, {"unexpected_entry", "missing_entries"})

    def test_null_new_pair_never_matches(self):
        plan = self.make_plan([("R1", pkg([("A", ["A"], True)],
                                          [ent("A", 5, "posting")]))])
        bad = ent("A", 5, "posting")
        bad["new_pair"] = None
        log = self.write_log("l", lines_of(1, "R1", [bad]))
        out = self.verify(plan, [log], 1)
        self.assertEqual(self.group(out, "R1", ["A"])["status"],
                         "receipt_not_signable")

    def test_unattributable_line_unknown_round_and_book(self):
        plan = self.make_plan([("R1", pkg([("A", ["A"], True)],
                                          [ent("A", 1, "posting")]))])
        ghost = ent("ZZ", 1, "posting")
        log = self.write_log("l", lines_of(1, "R1", [ent("A", 1, "posting")])
                             + lines_of(2, "R9", [ghost]))
        out = self.verify(plan, [log], 2)
        tx = self.failed(out, 2)
        self.assertEqual(tx["reasons"][0]["reason"], "line_not_attributable")
        self.assertEqual(tx["reasons"][0]["detail"], "round not in plan")
        self.assertEqual(self.group(out, "R1", ["A"])["status"], "signable")


class TestCoTransactionFailures(ReceiptTestCase):
    def test_co_transaction_group_not_matched(self):
        entries_b = [ent("B", 1, "posting"), ent("B", 2, "posting")]
        plan = self.make_plan([("R1", pkg([("A", ["A"], True),
                                           ("B", ["B"], True)],
                                          [ent("A", 1, "posting")]
                                          + entries_b))])
        # Commit 1: A complete, but B misses its second entry.
        log = self.write_log("l", lines_of(1, "R1", [ent("A", 1, "posting"),
                                                     entries_b[0]]))
        out = self.verify(plan, [log], 1)
        ga = self.group(out, "R1", ["A"])
        self.assertEqual(ga["status"], "receipt_not_signable")
        self.assertEqual(ga["reasons"][0]["reason"],
                         "co_transaction_group_not_matched")
        gb = self.group(out, "R1", ["B"])
        self.assertEqual(gb["reasons"][0]["reason"], "missing_entries")
        tx = self.failed(out, 1)
        self.assertEqual({r["reason"] for r in tx["reasons"]},
                         {"co_transaction_group_not_matched",
                          "missing_entries"})

    def test_multi_commit_group_sinks_clean_co_transaction_group(self):
        entries_a = [ent("A", 1, "posting")]
        entries_b = [ent("B", 1, "posting"), ent("B", 2, "posting")]
        plan = self.make_plan([("R1", pkg([("A", ["A"], True),
                                           ("B", ["B"], True)],
                                          entries_a + entries_b))])
        # Commit 1 carries all of A plus half of B; B's rest sits in commit 2.
        log = self.write_log("l", lines_of(1, "R1", entries_a + entries_b[:1])
                             + lines_of(2, "R1", entries_b[1:]))
        out = self.verify(plan, [log], 2)
        ga = self.group(out, "R1", ["A"])
        self.assertEqual(ga["status"], "receipt_not_signable")
        self.assertEqual(ga["reasons"][0]["reason"],
                         "co_transaction_group_not_matched")
        gb = self.group(out, "R1", ["B"])
        self.assertEqual(gb["reasons"][0]["reason"],
                         "group_entries_in_multiple_commits")
        self.assertEqual([tx["commit"] for tx in out["failed_transactions"]],
                         [1, 2])


class TestDeterminism(ReceiptTestCase):
    def test_resliced_reordered_and_duplicated_exports_same_conclusion(self):
        plan = self.make_plan([
            ("R1", pkg([("AB", ["A", "B"], True), ("D", ["D"], True)],
                       G1 + G3)),
            ("R2", pkg([("BC", ["B", "C"], True)], G2)),
        ])
        base = (lines_of(1, "R1", G1) + lines_of(2, "R2", G2)
                + lines_of(3, "R1", G3))
        one = self.write_log("one", base)
        out1 = self.verify(plan, [one], 3)

        # Same evidence: shuffled across three slices with overlap duplicates.
        shuffled = [base[5], base[0], base[7], base[2], base[1],
                    base[6], base[3], base[4]]
        s1 = self.write_log("s1", shuffled[:3] + [shuffled[0]])
        s2 = self.write_log("s2", shuffled[3:6] + [shuffled[0], shuffled[4]])
        s3 = self.write_log("s3", shuffled[6:])
        out2 = self.verify(plan, [s3, s1, s2], 3)
        # The verdict (groups, failed transactions) is identical; only the
        # export-overlap counter reflects the duplicated records.
        self.assertEqual(out1["groups"], out2["groups"])
        self.assertEqual(out1["failed_transactions"],
                         out2["failed_transactions"])
        self.assertEqual(out1["stats"]["receipt_lines"],
                         out2["stats"]["receipt_lines"])
        self.assertEqual(out2["stats"]["duplicate_export_lines"], 3)


class TestPlanValidation(ReceiptTestCase):
    def test_duplicate_round_id_rejected(self):
        p = pkg([("A", ["A"], True)], [ent("A", 1, "posting")])
        with self.assertRaises(ReceiptError):
            load_plan({"rounds": [{"round": "R1", "package": "a.json"},
                                  {"round": "R1", "package": "b.json"}]},
                      self.dir)

    def test_entry_for_non_effective_group_rejected(self):
        bad = pkg([("A", ["A"], False)], [ent("A", 1, "posting")])
        path = self.write_json("pkg.json", bad)
        with self.assertRaises(ReceiptError):
            load_plan({"rounds": [{"round": "R1",
                                   "package": os.path.basename(path)}]},
                      os.path.dirname(path))

    def test_entry_without_group_rejected(self):
        bad = pkg([("A", ["A"], True)], [ent("A", 1, "posting"),
                                         ent("GHOST", 1, "posting")])
        path = self.write_json("pkg.json", bad)
        with self.assertRaises(ReceiptError):
            load_plan({"rounds": [{"round": "R1",
                                   "package": os.path.basename(path)}]},
                      os.path.dirname(path))

    def test_overlapping_group_members_rejected(self):
        bad = pkg([("A", ["A", "B"], True), ("B2", ["B"], True)], [])
        path = self.write_json("pkg.json", bad)
        with self.assertRaises(ReceiptError):
            load_plan({"rounds": [{"round": "R1",
                                   "package": os.path.basename(path)}]},
                      os.path.dirname(path))

    def test_wrong_package_format_rejected(self):
        path = self.write_json("pkg.json", {"format": "recode_batch/v1"})
        with self.assertRaises(ReceiptError):
            load_plan({"rounds": [{"round": "R1",
                                   "package": os.path.basename(path)}]},
                      os.path.dirname(path))

    def test_missing_package_file_rejected(self):
        with self.assertRaises(ReceiptError):
            load_plan({"rounds": [{"round": "R1", "package": "nope.json"}]},
                      self.dir)

    def test_bare_list_plan_and_empty_plan(self):
        plan = load_plan([], self.dir)
        out = verify_receipts(plan, [], 0)
        self.assertEqual(out["groups"], [])
        self.assertEqual(out["failed_transactions"], [])


class TestCutoff(ReceiptTestCase):
    def test_cutoff_zero_and_partial_prefix(self):
        plan = self.make_plan([("R1", pkg([("A", ["A"], True)],
                                          [ent("A", 1, "posting")]))])
        log = self.write_log("l", lines_of(1, "R1", [ent("A", 1, "posting")])
                             + lines_of(2, "R1", [ent("A", 1, "posting")]))
        out = self.verify(plan, [log], 0)
        self.assertEqual(self.group(out, "R1", ["A"])["status"], "no_receipt")
        self.assertEqual(out["failed_transactions"], [])
        # Cutoff 1: the first posting signs the group off; commit 2 is not
        # evidence, so the group is *not* flagged as multi-commit.
        out = self.verify(plan, [log], 1)
        self.assertEqual(self.group(out, "R1", ["A"])["status"], "signable")
        # Cutoff 2: the second identical posting is an extra.
        out = self.verify(plan, [log], 2)
        self.assertEqual(self.group(out, "R1", ["A"])["status"],
                         "receipt_not_signable")


class TestScale(ReceiptTestCase):
    def test_large_log_many_transactions_and_one_huge_transaction(self):
        # Plan: 3 rounds x 10 chained groups (R{i} book B{j} depends on
        # R{i-1}'s group for the same book) + one big group.
        rounds = []
        big_entries = [ent(f"BIG-{k}", 1, "posting", y=f"y{k % 7}",
                           ny=f"NY{k % 7}") for k in range(2000)]
        for r in range(3):
            entries = []
            groups = []
            for j in range(10):
                book = f"B{j}"
                groups.append((book, [book], True))
                entries.append(ent(book, r * 10 + j, "posting",
                                   y=f"y{j}", ny=f"NY{j}"))
            rounds.append((f"R{r + 1}", pkg(groups, entries)))
        rounds.append(("R4", pkg([("BIG", [f"BIG-{k}" for k in range(2000)],
                                   True)], big_entries)))
        plan = self.make_plan(rounds)

        lines = []
        commit = 0
        # 3 rounds x 10 groups, each in its own transaction, in order.
        for r in range(3):
            for j in range(10):
                commit += 1
                lines += lines_of(commit, f"R{r + 1}",
                                  [ent(f"B{j}", r * 10 + j, "posting",
                                       y=f"y{j}", ny=f"NY{j}")])
        # One huge transaction carrying the 2000-entry group.
        commit += 1
        lines += lines_of(commit, "R4", big_entries)
        # Overlap: re-export the first 500 lines in a second slice.
        log1 = self.write_log("big1", lines)
        log2 = self.write_log("big2", lines[:500])
        out = self.verify(plan, [log1, log2], commit,
                          sort_chunk=64, sort_fanin=4)
        self.assertEqual(out["stats"]["groups_signable"], 31)
        self.assertEqual(out["stats"]["transactions_signable"], commit)
        self.assertEqual(out["failed_transactions"], [])
        self.assertEqual(out["stats"]["duplicate_export_lines"], 500)

    def test_many_transactions_streaming(self):
        # 5000 single-line transactions, alternating two independent groups.
        entries_a = [ent("A", 1, "posting")]
        entries_b = [ent("B", 2, "posting")]
        plan = self.make_plan([("R1", pkg([("A", ["A"], True),
                                           ("B", ["B"], True)],
                                          entries_a + entries_b))])
        lines = []
        for c in range(1, 5001):
            lines += lines_of(c, "R1", entries_a if c % 2 else entries_b)
        # Each group appears in 2500 commits -> never signable.
        out = self.verify(plan, [self.write_log("l", lines)], 5000,
                          sort_chunk=128, sort_fanin=4)
        self.assertEqual(out["stats"]["groups_signable"], 0)
        self.assertEqual(out["stats"]["transactions_failed"], 5000)
        self.assertEqual(len(out["failed_transactions"]), 5000)
        self.assertEqual(out["failed_transactions"][0]["commit"], 1)
        self.assertEqual(out["failed_transactions"][-1]["commit"], 5000)


class TestEndToEndWithApprovePackages(ReceiptTestCase):
    """Real correction packages from the approval chain fixture."""

    def _packages(self):
        def load(name):
            with open(os.path.join(APPROVAL_DIR, name),
                      encoding="utf-8") as fh:
                return json.load(fh)

        dims = load("dims_chain.json")
        cur = load("books_current.json")
        prop = load("books_proposed.json")
        handover = load_handover(dims, cur)
        maps = load_mapping(load("mapping_chain.json"),
                            handover.tree_x, handover.tree_y)
        pkg1 = build_correction_package(
            dims, cur, prop, ["H1", "H2", "H3", "H5"], maps)
        mid = {"books": pkg1["effective_handover"]["books"]}
        pkg2 = build_correction_package(
            dims, mid, prop, ["H1", "H2", "H3", "H4", "H5"], maps)
        # Round 3: H5 changes again (55 -> 77) on top of the final state.
        prop3 = json.loads(json.dumps(prop))
        for book in prop3["books"]:
            if book["id"] == "H5":
                book["root"]["value"] = 77
        final = {"books": pkg2["effective_handover"]["books"]}
        pkg3 = build_correction_package(dims, final, prop3, ["H5"], maps)
        return pkg1, pkg2, pkg3

    def test_three_round_chain(self):
        pkg1, pkg2, pkg3 = self._packages()
        plan = self.make_plan([("R1", pkg1), ("R2", pkg2), ("R3", pkg3)])

        def entries_of(package):
            return package["reversals"] + package["postings"]

        # In-order log: every round in its own transaction, in order.
        lines = (lines_of(1, "R1", entries_of(pkg1))
                 + lines_of(2, "R2", entries_of(pkg2))
                 + lines_of(3, "R3", entries_of(pkg3)))
        out = self.verify(plan, [self.write_log("l", lines)], 3)
        self.assertEqual(out["failed_transactions"], [])
        self.assertEqual(out["stats"]["groups_signable"], 3)
        # Round 2's chain includes H3's zero-amount posting: it must be
        # expected, and it signed off only because the log carried it.
        self.assertTrue(any(
            e["amount"] == 0 for e in entries_of(pkg2)
        ))
        # Round 3's H5 group depends on round 1's H5 group (round 2 has no
        # H5 group): posting round 3 before round 1 must fail.
        reordered = (lines_of(1, "R3", entries_of(pkg3))
                     + lines_of(2, "R2", entries_of(pkg2))
                     + lines_of(3, "R1", entries_of(pkg1)))
        out = self.verify(plan, [self.write_log("l2", reordered)], 3)
        h5_r3 = self.group(out, "R3", ["H5"])
        self.assertEqual(h5_r3["status"], "receipt_not_signable")
        self.assertEqual(h5_r3["reasons"][0]["reason"],
                         "prerequisite_commit_not_earlier")
        # The round-2 chain group has no shared members with H5 and is
        # unaffected by the H5 ordering problem.
        chain = self.group(out, "R2", ["H1", "H2", "H3", "H4"])
        self.assertEqual(chain["status"], "signable")

    def test_zero_posting_missing_is_a_finding(self):
        pkg1, pkg2, pkg3 = self._packages()
        plan = self.make_plan([("R1", pkg1), ("R2", pkg2), ("R3", pkg3)])

        def entries_of(package):
            return package["reversals"] + package["postings"]

        round2 = [e for e in entries_of(pkg2) if e["amount"] != 0]
        lines = (lines_of(1, "R1", entries_of(pkg1))
                 + lines_of(2, "R2", round2)
                 + lines_of(3, "R3", entries_of(pkg3)))
        out = self.verify(plan, [self.write_log("l", lines)], 3)
        chain = self.group(out, "R2", ["H1", "H2", "H3", "H4"])
        self.assertEqual(chain["status"], "receipt_not_signable")
        reason = chain["reasons"][0]
        self.assertEqual(reason["reason"], "missing_entries")
        self.assertEqual(reason["entries"][0]["entry"]["amount"], 0)


class TestCli(ReceiptTestCase):
    def run_cli(self, *args):
        script = os.path.join(ROOT, "src", "cube", "cli.py")
        return subprocess.run(
            [sys.executable, "-I", script, *args],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )

    def test_cli_success_and_stdout_match_materialized(self):
        plan = self.make_plan([
            ("R1", pkg([("AB", ["A", "B"], True), ("D", ["D"], True)],
                       G1 + G3)),
            ("R2", pkg([("BC", ["B", "C"], True)], G2)),
        ])
        log = self.write_log("l", lines_of(1, "R1", G1)
                             + lines_of(2, "R2", G2)
                             + lines_of(3, "R1", G3))
        out_path = self._path("conclusion.json")
        proc = self.run_cli("verify", "--plan", plan, "--logs", log,
                            "--cutoff", "3", "--out", out_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out_path, encoding="utf-8") as fh:
            from_file = json.load(fh)
        proc = self.run_cli("verify", "--plan", plan, "--logs", log,
                            "--cutoff", "3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        from_stdout = json.loads(proc.stdout)
        self.assertEqual(from_file, from_stdout)
        self.assertEqual(from_file, self.verify(plan, [log], 3))

    def test_cli_unverifiable_prefix_keeps_existing_conclusion(self):
        plan = self.make_plan([("R1", pkg([("A", ["A"], True)],
                                          [ent("A", 1, "posting")]))])
        good = self.write_log("good", lines_of(1, "R1",
                                               [ent("A", 1, "posting")]))
        out_path = self._path("conclusion.json")
        proc = self.run_cli("verify", "--plan", plan, "--logs", good,
                            "--cutoff", "1", "--out", out_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out_path, encoding="utf-8") as fh:
            saved = fh.read()

        # Now the prefix is broken (missing line 2): exit 2, file intact.
        broken = self.write_log("broken",
                                [line(1, 1, 2, "R1", ent("A", 1, "posting"))])
        proc = self.run_cli("verify", "--plan", plan, "--logs", broken,
                            "--cutoff", "1", "--out", out_path)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("missing line", proc.stderr)
        with open(out_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), saved)

    def test_cli_business_findings_still_exit_zero(self):
        plan = self.make_plan([("R1", pkg([("A", ["A"], True)],
                                          [ent("A", 1, "posting")]))])
        log = self.write_log("l", [])  # complete empty prefix, cutoff 0
        out_path = self._path("c.json")
        proc = self.run_cli("verify", "--plan", plan, "--logs", log,
                            "--cutoff", "0", "--out", out_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out_path, encoding="utf-8") as fh:
            out = json.load(fh)
        self.assertEqual(out["groups"][0]["status"], "no_receipt")

    def test_cli_bad_plan_exit_two_and_no_output(self):
        out_path = self._path("c.json")
        proc = self.run_cli("verify", "--plan", self._path("missing.json"),
                            "--logs", self._path("l"), "--cutoff", "1",
                            "--out", out_path)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(out_path))

    def test_cli_negative_cutoff_rejected(self):
        proc = self.run_cli("verify", "--plan", "p", "--logs", "l",
                            "--cutoff", "-1")
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
