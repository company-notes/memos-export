"""Integration tests for the offline SQLite transfer CLI (no Memos server needed)."""

from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).parent / "scripts" / "transfer_memo_owner.py"


class TestMemoOwnerTransfer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "memos.db"
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
                CREATE TABLE user (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    row_status TEXT NOT NULL, role TEXT NOT NULL
                );
                CREATE TABLE memo (
                    id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE,
                    creator_id INTEGER NOT NULL, content TEXT NOT NULL,
                    visibility TEXT NOT NULL, row_status TEXT NOT NULL,
                    space_id INTEGER
                );
                CREATE TABLE attachment (
                    id INTEGER PRIMARY KEY, uid TEXT UNIQUE, creator_id INTEGER,
                    memo_id INTEGER
                );
                CREATE TABLE space_member (
                    space_id INTEGER, user_id INTEGER, status TEXT, role TEXT,
                    PRIMARY KEY (space_id, user_id)
                );
                INSERT INTO user VALUES (1, 'alice', 'NORMAL', 'USER');
                INSERT INTO user VALUES (2, 'bob', 'NORMAL', 'USER');
                INSERT INTO user VALUES (3, 'charlie', 'ARCHIVED', 'USER');
                INSERT INTO memo VALUES (11, 'first', 1, 'keep this', 'PRIVATE', 'NORMAL', NULL);
                INSERT INTO memo VALUES (12, 'second', 1, 'other', 'PROTECTED', 'ARCHIVED', NULL);
                INSERT INTO attachment VALUES (21, 'att21', 1, 11);
            """)

    def run_tool(self, *flags):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(self.db_path),
             "--from-user", "alice", "--to-user", "bob", *flags],
            capture_output=True, text=True,
        )

    def select(self, sql):
        with sqlite3.connect(self.db_path) as db:
            return db.execute(sql).fetchall()

    def test_dry_run_does_not_change_db_or_create_backup(self):
        result = self.run_tool("--memo", "memos/first")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY RUN", result.stdout)
        self.assertEqual(self.select("SELECT creator_id FROM memo WHERE id = 11"), [(1,)])
        self.assertEqual(list(Path(self.tmp.name).glob("*.sqlite3")), [])

    def test_apply_changes_author_and_bound_attachment_only(self):
        result = self.run_tool("--memo", "first", "--apply", "--stopped", "--confirm", "alice->bob")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.select("SELECT creator_id, content, visibility FROM memo WHERE id = 11"),
                         [(2, "keep this", "PRIVATE")])
        self.assertEqual(self.select("SELECT creator_id FROM attachment WHERE id = 21"), [(2,)])
        self.assertEqual(self.select("SELECT creator_id FROM memo WHERE id = 12"), [(1,)])
        backups = list(Path(self.tmp.name).glob("*.pre-transfer-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(backup.execute("SELECT creator_id FROM memo WHERE id = 11").fetchone(), (1,))

    def test_apply_rejects_missing_confirmation(self):
        result = self.run_tool("--memo", "first", "--apply", "--confirm", "alice->bob")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.select("SELECT creator_id FROM memo WHERE id=11"), [(1,)])

    def test_all_includes_archived_memos(self):
        result = self.run_tool("--all", "--apply", "--stopped", "--confirm", "alice->bob")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.select("SELECT creator_id FROM memo ORDER BY id"), [(2,), (2,)])

    def test_refuses_wrong_owner_for_selected_uid(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE memo SET creator_id = 2 WHERE id=12")
        result = self.run_tool("--memo", "first", "--memo", "second")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not owned", result.stderr)

    def test_rejects_inactive_target(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(self.db_path),
             "--from-user", "alice", "--to-user", "charlie", "--memo", "first"],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not active", result.stderr)

    def test_space_requires_membership(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE memo SET visibility='SPACE', space_id=7 WHERE id=11")
        self.assertIn("not an active member", self.run_tool("--memo", "first").stderr)
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT INTO space_member VALUES (7, 2, 'ACTIVE', 'USER')")
        self.assertEqual(self.run_tool("--memo", "first").returncode, 0)

    def test_refuses_foreign_owned_attached_file(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE attachment SET creator_id=2 WHERE id=21")
        result = self.run_tool("--memo", "first", "--apply", "--stopped", "--confirm", "alice->bob")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owned by other users", result.stderr)
        self.assertEqual(self.select("SELECT creator_id FROM memo WHERE id=11"), [(1,)])

    def test_selection_rejects_nonexistent_uid(self):
        result = self.run_tool("--memo", "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown memo", result.stderr)

    def test_nonexistent_db_is_not_created(self):
        missing = Path(self.tmp.name) / "missing.db"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(missing),
             "--from-user", "alice", "--to-user", "bob", "--memo", "first"],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
