#!/usr/bin/env python3
"""Trim the unterminated final record from each netflow CSV.

The SFTP upload of every region's ``netflow_5tuple_minute_readable.csv``
stopped mid-record -- verified: no file ends with a newline, and xian's tail
reads ``..."2026-08-24 08:42:19`` (inside a quoted timestamp).  pandas then
raises ``ParserError: EOF inside string`` and the topology stage aborts, which
in turn means no ``incident_candidates.json`` is produced.

This script removes **only** the unterminated final line.  Every complete
record is left byte-identical, and the byte delta is printed so the loss is
auditable.  It never appends anything.

Usage:  python3 trim_netflow_tail.py [glob]
"""

from __future__ import annotations

import glob
import os
import sys

DEFAULT_PATTERN = (
    "/202531630503/lyt/workspace/data/*/*/processed/netflow_5tuple_minute_readable.csv"
)


def ends_with_newline(path: str) -> bool:
    with open(path, "rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"


def trim_to_last_newline(path: str) -> tuple[int, int]:
    """Truncate *path* back to its last complete line.  Returns (before, after)."""
    with open(path, "r+b") as handle:
        handle.seek(0, os.SEEK_END)
        before = handle.tell()
        pos = before - 1
        while pos > 0:
            handle.seek(pos)
            if handle.read(1) == b"\n":
                break
            pos -= 1
        if pos <= 0:
            return before, before
        handle.truncate(pos + 1)
        return before, pos + 1


def main(argv: list[str]) -> int:
    pattern = argv[1] if len(argv) > 1 else DEFAULT_PATTERN
    paths = sorted(glob.glob(pattern))
    if not paths:
        print(f"[!!] 没有匹配到文件: {pattern}")
        return 1

    total_dropped = 0
    for path in paths:
        region = path.split("/data/")[-1].split("/")[0][:20]
        if ends_with_newline(path):
            print(f"[skip] {region} 已以换行结尾，未改动")
            continue
        before, after = trim_to_last_newline(path)
        dropped = before - after
        total_dropped += dropped
        print(f"[trim] {region} {before} -> {after}  丢弃残行 {dropped} 字节")

    print(f"=== 共处理 {len(paths)} 个文件，合计丢弃 {total_dropped} 字节 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
