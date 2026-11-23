"""Command line: offline build + cell retrieval. No data-entry UI.

Examples
--------
Build a cube and print the publish/withhold audit report::

    python -m cube.cli build --dims dims.json --books books.json

Read one target cell (subtree x subtree)::

    python -m cube.cli query --dims dims.json --books books.json \\
        --target x1 y1

Read several cells from a batch file (one "x y" pair per line, '#' comments)::

    python -m cube.cli batch --dims dims.json --books books.json \\
        --targets targets.txt --out results.json

Everything runs offline; malformed input or a withheld book is reported,
never silently turned into zero.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import List

try:  # package execution: python -m cube.cli
    from .engine import BookResult, Cube, Fragment, build_cube, query_cube
    from .loader import load_handover
    from .mapping import load_mapping
    from .model import ModelError
    from .recode import convert_handover, reconcile
except ImportError:  # direct script execution: python /path/to/cli.py
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from cube.engine import BookResult, Cube, Fragment, build_cube, query_cube
    from cube.loader import load_handover
    from cube.mapping import load_mapping
    from cube.model import ModelError
    from cube.recode import convert_handover, reconcile


def _load_inputs(dims_path: str, books_path: str):
    with open(dims_path, "r", encoding="utf-8") as fh:
        dims_raw = json.load(fh)
    with open(books_path, "r", encoding="utf-8") as fh:
        books_raw = json.load(fh)
    return load_handover(dims_raw, books_raw)


def _load_mapping_file(path: str, handover):
    with open(path, "r", encoding="utf-8") as fh:
        mapping_raw = json.load(fh)
    return load_mapping(mapping_raw, handover.tree_x, handover.tree_y)


def _atomic_write(path: str, text: str) -> None:
    """Write a new batch without touching an existing one on failure.

    The temp file lives beside the target so the final replace is atomic on
    the same filesystem; an interrupted run never leaves a partial batch.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".recode-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _frag_to_dict(f: Fragment) -> dict:
    return {
        "rect": list(f.rect),
        "kinds": list(f.kinds),
        "value": f.value,
        "source": f.source,
        "source_cell": list(f.cell) if f.cell is not None else None,
        "reason": f.reason,
    }


def _book_result_to_dict(br: BookResult) -> dict:
    return {
        "book": br.book_id,
        "status": br.status,
        "sum_known": br.sum_known,
        "exact": [_frag_to_dict(f) for f in br.exact],
        "not_applicable": [_frag_to_dict(f) for f in br.not_applicable],
        "unknown": [_frag_to_dict(f) for f in br.unknown],
    }


def result_to_dict(result: dict) -> dict:
    return {
        "target": result["target"],
        "status": result["status"],
        "sum_known": result["sum_known"],
        "exact_fragments": [_frag_to_dict(f) for f in result["exact"]],
        "not_applicable_fragments": [_frag_to_dict(f) for f in result["not_applicable"]],
        "unknown_fragments": [_frag_to_dict(f) for f in result["unknown"]],
        "per_book": [_book_result_to_dict(br) for br in result["book_results"]],
        "engine_stats": result["stats"],
    }


def build_report(cube: Cube) -> dict:
    return {
        "published_books": sorted(cube.published),
        "withheld_books": {
            bid: reasons for bid, reasons in sorted(cube.withheld.items())
        },
        "control_checks": {
            bid: notes for bid, notes in sorted(cube.checks.items())
        },
        "stats": dict(cube.stats),
    }


def _print_human(result: dict, out) -> None:
    out.write(f"target {result['target'][0]} x {result['target'][1]}\n")
    out.write(f"  status     : {result['status']}\n")
    out.write(f"  sum_known  : {result['sum_known']}\n")
    for label, frags in (
        ("exact", result["exact"]),
        ("not_applicable", result["not_applicable"]),
        ("unknown (not counted)", result["unknown"]),
    ):
        out.write(f"  {label}:\n")
        if not frags:
            out.write("    (none)\n")
        for f in frags:
            val = f" value={f.value}" if f.value is not None else ""
            out.write(
                f"    - {f.rect[0]}|{f.rect[1]} via {f.source}"
                f"{(' cell ' + f.cell[0] + '|' + f.cell[1]) if f.cell else ''}"
                f"{val} [{f.reason}]\n"
            )


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cube",
        description="Offline sparse summary cube from sparse book handovers.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dims", required=True, help="dimension trees JSON")
        p.add_argument("--books", required=True, help="book handovers JSON")

    p_build = sub.add_parser("build", help="build cube and print audit report")
    add_common(p_build)

    p_query = sub.add_parser("query", help="read one target cell")
    add_common(p_query)
    p_query.add_argument("--target", nargs=2, metavar=("X_NODE", "Y_NODE"),
                         required=True)
    p_query.add_argument("--json", action="store_true",
                         help="emit machine-readable JSON")

    p_batch = sub.add_parser("batch", help="read many target cells")
    add_common(p_batch)
    p_batch.add_argument("--targets", required=True,
                         help="text file, one 'X_NODE Y_NODE' pair per line")
    p_batch.add_argument("--out", help="write JSON results here (default stdout)")

    p_recode = sub.add_parser(
        "recode",
        help="convert old handover to an importable new-code batch",
    )
    add_common(p_recode)
    p_recode.add_argument("--mapping", required=True,
                          help="old-node -> new-code rules JSON")
    p_recode.add_argument("--out", required=True,
                          help="write the recode batch JSON here")
    p_recode.add_argument("--audit", action="store_true",
                          help="also print a short audit report to stdout")

    args = parser.parse_args(argv)
    try:
        handover = _load_inputs(args.dims, args.books)
        cube = build_cube(handover)
    except (ModelError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "recode":
        try:
            maps = _load_mapping_file(args.mapping, handover)
        except (ModelError, FileNotFoundError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        batch = convert_handover(handover, cube, maps)
        text = json.dumps(batch, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            _atomic_write(args.out, text + "\n")
        except OSError as exc:
            print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
            return 2
        check = reconcile(cube, batch)
        if args.audit:
            print(json.dumps({
                "published_books": batch["published_books"],
                "withheld_books": [b["book"] for b in batch["withheld_books"]],
                "totals": batch["totals"],
                "reconcile": check,
                "stats": batch["stats"],
            }, ensure_ascii=False, indent=2))
        if not check["ok"]:
            print(
                "error: recode batch does not reconcile with old sources: "
                + ", ".join(check["unbalanced_books"]),
                file=sys.stderr,
            )
            return 3
        return 0

    if args.cmd == "build":
        print(json.dumps(build_report(cube), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "query":
        result = query_cube(cube, tuple(args.target))
        if args.json:
            print(json.dumps(result_to_dict(result), ensure_ascii=False, indent=2))
        else:
            _print_human(result, sys.stdout)
        return 0

    # batch
    targets = []
    with open(args.targets, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 2:
                print(
                    f"error: {args.targets}:{lineno}: expected 'X Y', got {line!r}",
                    file=sys.stderr,
                )
                return 2
            targets.append((parts[0], parts[1]))
    try:
        results = [result_to_dict(query_cube(cube, t)) for t in targets]
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload = {
        "build_report": build_report(cube),
        "results": results,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
