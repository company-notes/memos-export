#!/usr/bin/env python3
"""Memos v0.31.0 SQLite memo ownership transfer: dry-run, online or offline."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sqlite3
import sys


class TransferError(Exception):
    """Refuse a transfer rather than make an unsafe or ambiguous change."""


def memo_uid(value: str) -> str:
    value = value.strip()
    if value.startswith("memos/"):
        value = value[6:]
    if not value or "/" in value or any(ord(ch) < 33 for ch in value):
        raise argparse.ArgumentTypeError("memo must be a UID or memos/<uid>")
    return value


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, type=Path, help="path to the Memos v0.31.0 SQLite database")
    p.add_argument("--from-user", required=True, help="exact existing username (case-sensitive)")
    p.add_argument("--to-user", required=True, help="exact target username (case-sensitive)")
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--memo", action="append", type=memo_uid, metavar="UID", help="memo UID; repeat to select more")
    selection.add_argument("--all", action="store_true", help="all memos owned by --from-user, including archived and comments")
    p.add_argument("--apply", action="store_true", help="actually change database rows (default: dry run)")
    p.add_argument("--confirm", metavar="FROM->TO", help="required with --apply, e.g. alice->bob")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--online", action="store_true", help="apply while Memos may be running (experimental)")
    mode.add_argument("--offline", "--stopped", dest="stopped", action="store_true",
                      help="acknowledge all database writers are stopped (recommended)")
    p.add_argument("--lock-timeout", type=lock_timeout, default=10.0, metavar="SECONDS",
                   help="SQLite busy/write-lock wait, 0-300 seconds (default: 10)")
    p.add_argument("--backup", type=Path, help="new backup path; default: generated next to the database")
    return p


def lock_timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("lock timeout must be a number of seconds") from exc
    if not math.isfinite(seconds) or not 0 <= seconds <= 300:
        raise argparse.ArgumentTypeError("lock timeout must be between 0 and 300 seconds")
    return seconds


def open_db(path: Path, *, write: bool, lock_wait: float) -> sqlite3.Connection:
    if not path.is_file():
        raise TransferError(f"database file not found: {path}")
    with path.open("rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise TransferError("file is not a SQLite database")
    mode = "rw" if write else "ro"
    db = sqlite3.connect(path.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=lock_wait)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout = {int(lock_wait * 1000)}")
    if not write:
        db.execute("PRAGMA query_only = ON")
    return db


def validate_schema(db: sqlite3.Connection) -> None:
    required = {
        "memo": {"id", "uid", "creator_id", "visibility", "row_status", "space_id", "updated_ts", "content"},
        "user": {"id", "username", "row_status", "role"},
        "attachment": {"id", "creator_id", "memo_id", "updated_ts"},
        "space_member": {"space_id", "user_id", "status", "role"},
    }
    for table, columns in required.items():
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not columns <= existing:
            raise TransferError(f"unsupported Memos schema: {table} missing {sorted(columns - existing)}")
    result = db.execute("PRAGMA quick_check").fetchone()
    if result is None or result[0] != "ok":
        raise TransferError("SQLite quick_check failed; repair the database before transferring")


def get_user(db: sqlite3.Connection, username: str) -> sqlite3.Row:
    rows = db.execute("SELECT id, username, row_status, role FROM user WHERE username = ?", (username,)).fetchall()
    if len(rows) != 1:
        raise TransferError(f"username not found (case-sensitive): {username!r}")
    return rows[0]


def plan(db: sqlite3.Connection, args: argparse.Namespace) -> tuple[list[sqlite3.Row], dict[int, tuple[tuple, ...]]]:
    source = get_user(db, args.from_user)
    target = get_user(db, args.to_user)
    if source["id"] == target["id"]:
        raise TransferError("source and target must be different users")
    if target["row_status"] != "NORMAL":
        raise TransferError("target user is not active (row_status must be NORMAL)")
    if args.all:
        memos = db.execute(
            "SELECT id, uid, creator_id, visibility, row_status, space_id, updated_ts, content "
            "FROM memo WHERE creator_id = ? ORDER BY id",
            (source["id"],),
        ).fetchall()
    else:
        uids = list(dict.fromkeys(args.memo))
        marks = ",".join("?" for _ in uids)
        memos = db.execute(
            f"SELECT id, uid, creator_id, visibility, row_status, space_id, updated_ts, content "
            f"FROM memo WHERE uid IN ({marks}) ORDER BY id",
            uids,
        ).fetchall()
        missing = set(uids) - {m["uid"] for m in memos}
        if missing:
            raise TransferError(f"unknown memo UID(s): {', '.join(sorted(missing))}")
        mismatched = [m["uid"] for m in memos if m["creator_id"] != source["id"]]
        if mismatched:
            raise TransferError(f"memo(s) not owned by {args.from_user}: {', '.join(mismatched)}")
    if not memos:
        raise TransferError("no memos selected; nothing to transfer")

    attachments: dict[int, tuple[tuple, ...]] = {}
    for memo in memos:
        if memo["visibility"] == "SPACE":
            if memo["space_id"] is None:
                raise TransferError(f"SPACE memo {memo['uid']} has no Space")
            if target["role"] != "ADMIN":
                member = db.execute(
                    "SELECT 1 FROM space_member WHERE space_id = ? AND user_id = ? "
                    "AND status = 'ACTIVE' AND role IN ('ADMIN', 'USER')",
                    (memo["space_id"], target["id"]),
                ).fetchone()
                if not member:
                    raise TransferError(f"target is not an active member of Space for memo {memo['uid']}")
        elif memo["visibility"] not in {"PRIVATE", "PROTECTED", "PUBLIC"}:
            raise TransferError(f"unknown visibility for memo {memo['uid']}: {memo['visibility']}")
        owned = db.execute(
            "SELECT id, creator_id, memo_id, updated_ts FROM attachment WHERE memo_id = ? ORDER BY id",
            (memo["id"],),
        ).fetchall()
        foreign = [str(a["creator_id"]) for a in owned if a["creator_id"] != source["id"]]
        if foreign:
            raise TransferError(f"memo {memo['uid']} has attachment(s) owned by other users: {', '.join(foreign)}")
        attachments[memo["id"]] = tuple(tuple(row) for row in owned)
    return memos, attachments


def backup_database(db: sqlite3.Connection, filename: Path) -> None:
    filename = filename.expanduser().resolve()
    try:
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise TransferError(f"backup already exists: {filename}") from exc
    else:
        os.close(fd)
    try:
        with sqlite3.connect(filename) as dest:
            db.backup(dest)
            check = dest.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise TransferError("backup SQLite quick_check failed")
    except Exception:
        filename.unlink(missing_ok=True)
        raise
    print(f"Backup written: {filename}")


def run(args: argparse.Namespace) -> int:
    if args.backup and not args.apply:
        raise TransferError("--backup is only used with --apply")
    if args.apply and (
        not (args.online or args.stopped)
        or args.confirm != f"{args.from_user}->{args.to_user}"
    ):
        raise TransferError("--apply requires --online or --offline/--stopped and matching --confirm 'FROM->TO'")
    path = args.db.expanduser().resolve()
    with closing(open_db(path, write=args.apply, lock_wait=args.lock_timeout)) as db:
        validate_schema(db)
        memos, assets = plan(db, args)
        user_snapshot = (tuple(get_user(db, args.from_user)), tuple(get_user(db, args.to_user)))
        counts = Counter(m["visibility"] for m in memos)
        print(f"{args.from_user} -> {args.to_user}: {len(memos)} memo(s), {sum(map(len, assets.values()))} bound attachment(s)")
        print("Visibility: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        for m in memos[:20]:
            print(f"  memos/{m['uid']} [{m['visibility']}, {m['row_status']}] attachments={len(assets[m['id']])}")
        if len(memos) > 20:
            print(f"  ... {len(memos) - 20} more memo(s)")
        if not args.apply:
            print("DRY RUN: nothing changed. For writes, pass --apply --online (experimental) "
                  "or --apply --offline (server stopped), plus --confirm 'FROM->TO'.")
            return 0

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = args.backup or path.with_name(f"{path.name}.pre-transfer-{stamp}.sqlite3")
        if backup.expanduser().resolve() == path:
            raise TransferError("backup path cannot be the database itself")
        # Online backup API captures a consistent snapshot even if the source DB is in WAL mode.
        # This is intentionally outside the write lock. Changes between backup and lock abort.
        backup_database(db, backup)
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise TransferError(
                    f"could not obtain SQLite write lock within {args.lock_timeout:g}s; "
                    "no memos changed (backup retained)"
                ) from exc
            raise
        try:
            # Under the write lock, detect API writes occurring during preview/backup.
            # Compare memo content/timestamp and each attachment identity + updated timestamp,
            # not merely the number of bound files.
            locked_memos, locked_assets = plan(db, args)
            locked_users = (tuple(get_user(db, args.from_user)), tuple(get_user(db, args.to_user)))
            if (
                [tuple(m) for m in locked_memos] != [tuple(m) for m in memos]
                or locked_assets != assets
                or locked_users != user_snapshot
            ):
                raise TransferError(
                    "memo, attachment, or user changed during preview/backup; "
                    "transfer cancelled, rerun the dry-run"
                )
            source_id, target_id = user_snapshot[0][0], user_snapshot[1][0]
            for memo in locked_memos:
                changed = db.execute(
                    "UPDATE memo SET creator_id = ? WHERE id = ? AND creator_id = ?",
                    (target_id, memo["id"], source_id),
                )
                if changed.rowcount != 1:
                    raise TransferError(f"memo owner changed concurrently: {memo['uid']}")
                changed_assets = db.execute(
                    "UPDATE attachment SET creator_id = ? WHERE memo_id = ? AND creator_id = ?",
                    (target_id, memo["id"], source_id),
                )
                if changed_assets.rowcount != len(locked_assets[memo["id"]]):
                    raise TransferError(f"attachment count changed concurrently: {memo['uid']}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        print("TRANSFER COMPLETE. " +
              ("ONLINE: verify both accounts; Memos may have stale cached data/session views."
               if args.online else "OFFLINE: restart Memos and verify access as both accounts."))
        print("Comments, reactions, detached attachments, and Space memberships were not altered.")
        return 0


def main() -> int:
    args = parser().parse_args()
    try:
        return run(args)
    except (TransferError, sqlite3.Error, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
