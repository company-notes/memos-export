#!/usr/bin/env python3
"""Offline Memos v0.31.x SQLite memo-ownership transfer (no REST API writes)."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import unquote


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
    p.add_argument("--db", required=True, type=Path, help="path to the STOPPED Memos SQLite database")
    p.add_argument("--from-user", required=True, help="exact existing username (case-sensitive)")
    p.add_argument("--to-user", required=True, help="exact target username (case-sensitive)")
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--memo", action="append", type=memo_uid, metavar="UID", help="memo UID; repeat to select more")
    selection.add_argument("--all", action="store_true", help="all memos owned by --from-user, including archived and comments")
    p.add_argument("--apply", action="store_true", help="actually change database rows (default: dry run)")
    p.add_argument("--confirm", metavar="FROM->TO", help="required with --apply, e.g. alice->bob")
    p.add_argument("--stopped", action="store_true", help="acknowledge that Memos and all other DB writers are STOPPED")
    p.add_argument("--backup", type=Path, help="new backup path; default: generated next to the database")
    return p


def open_db(path: Path, *, write: bool) -> sqlite3.Connection:
    if not path.is_file():
        raise TransferError(f"database file not found: {path}")
    with path.open("rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise TransferError("file is not a SQLite database")
    mode = "rw" if write else "ro"
    db = sqlite3.connect(path.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 1000")
    if not write:
        db.execute("PRAGMA query_only = ON")
    return db


def validate_schema(db: sqlite3.Connection) -> None:
    required = {
        "memo": {"id", "uid", "creator_id", "visibility", "row_status", "space_id"},
        "user": {"id", "username", "row_status", "role"},
        "attachment": {"id", "creator_id", "memo_id"},
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


def plan(db: sqlite3.Connection, args: argparse.Namespace) -> tuple[list[sqlite3.Row], dict[int, int]]:
    source = get_user(db, args.from_user)
    target = get_user(db, args.to_user)
    if source["id"] == target["id"]:
        raise TransferError("source and target must be different users")
    if target["row_status"] != "NORMAL":
        raise TransferError("target user is not active (row_status must be NORMAL")
    if args.all:
        memos = db.execute(
            "SELECT id, uid, creator_id, visibility, row_status, space_id FROM memo WHERE creator_id = ? ORDER BY id",
            (source["id"],),
        ).fetchall()
    else:
        uids = list(dict.fromkeys(args.memo))
        marks = ",".join("?" for _ in uids)
        memos = db.execute(
            f"SELECT id, uid, creator_id, visibility, row_status, space_id FROM memo WHERE uid IN ({marks}) ORDER BY id",
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

    attachments: dict[int, int] = {}
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
            "SELECT creator_id, COUNT(*) AS n FROM attachment WHERE memo_id = ? GROUP BY creator_id",
            (memo["id"],),
        ).fetchall()
        foreign = [str(a["creator_id"]) for a in owned if a["creator_id"] != source["id"]]
        if foreign:
            raise TransferError(f"memo {memo['uid']} has attachment(s) owned by other users: {', '.join(foreign)}")
        attachments[memo["id"]] = sum(a["n"] for a in owned)
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
    if args.apply and (not args.stopped or args.confirm != f"{args.from_user}->{args.to_user}"):
        raise TransferError("--apply requires --stopped and --confirm 'FROM->TO' matching the usernames")
    path = args.db.expanduser().resolve()
    with open_db(path, write=args.apply) as db:
        validate_schema(db)
        memos, assets = plan(db, args)
        counts = Counter(m["visibility"] for m in memos)
        print(f"{args.from_user} -> {args.to_user}: {len(memos)} memo(s), {sum(assets.values())} bound attachment(s)")
        print("Visibility: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        for m in memos[:20]:
            print(f"  memos/{m['uid']} [{m['visibility']}, {m['row_status']}] attachments={assets[m['id']]}")
        if len(memos) > 20:
            print(f"  ... {len(memos) - 20} more memo(s)")
        if not args.apply:
            print("DRY RUN: nothing changed. Stop Memos, then pass --apply --stopped --confirm 'FROM->TO'.")
            return 0

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = args.backup or path.with_name(f"{path.name}.pre-transfer-{stamp}.sqlite3")
        if backup.expanduser().resolve() == path:
            raise TransferError("backup path cannot be the database itself")
        backup_database(db, backup)
        db.execute("BEGIN IMMEDIATE")
        try:
            # Re-check the selection under a write lock before touching any rows.
            locked_memos, locked_assets = plan(db, args)
            if [(m["id"], m["uid"], m["creator_id"], m["visibility"], m["row_status"], m["space_id"]) for m in locked_memos] != [
                (m["id"], m["uid"], m["creator_id"], m["visibility"], m["row_status"], m["space_id"]) for m in memos
            ] or locked_assets != assets:
                raise TransferError("memo selection changed since backup; transaction cancelled")
            source_id = get_user(db, args.from_user)["id"]
            target_id = get_user(db, args.to_user)["id"]
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
                if changed_assets.rowcount != locked_assets[memo["id"]]:
                    raise TransferError(f"attachment count changed concurrently: {memo['uid']}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        print("TRANSFER COMPLETE. Restart Memos; verify access as both accounts.")
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
