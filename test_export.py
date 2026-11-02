import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from memos_export_v031 import safe_filename, rewrite_asset_links, save_memo, export

class FakeApi:
    base = "https://memos.example.com"
    def __init__(self):
        self.downloaded = []
    def download(self, path, target):
        self.downloaded.append(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test")
    def paginate(self, path, field, extra=None):
        return iter([])

class ExportTests(unittest.TestCase):
    def test_safe_names(self):
        self.assertEqual(safe_filename("../../CON"), "_.._CON")
        self.assertEqual(safe_filename("a/b:c"), "a_b_c")
        self.assertEqual(safe_filename("NUL"), "_NUL")

    def test_rewrite_only_local_saved(self):
        body = "![photo](/file/attachments/u123/a%20b.jpg) ![remote](https://other.example.com/file/attachments/u123/a.jpg)"
        out = rewrite_asset_links(body, {"u123": "assets/u123_我的 圖.jpg"}, "https://memos.example.com")
        self.assertIn("assets/u123_%E6%88%91%E7%9A%84%20%E5%9C%96.jpg", out)
        self.assertIn("https://other.example.com/file/attachments/u123/a.jpg", out)

    def test_export_md_and_attachment(self):
        memo = {"name": "memos/abc123", "creator": "users/bob", "content": "![A](/file/attachments/u1/photo.jpg)", "createTime": "2026-09-01T12:00:00Z", "visibility": "PROTECTED", "attachments": [{"name":"attachments/u1", "filename":"photo.jpg"}]}
        with TemporaryDirectory() as td:
            api = FakeApi()
            count, external = save_memo(api, memo, Path(td) / "bob", [], False)
            self.assertEqual(count, 1)
            self.assertEqual(external, 0)
            self.assertTrue((Path(td)/"bob"/"assets"/"u1_photo.jpg").exists())
            md = list((Path(td)/"bob").glob("*.md"))[0].read_text(encoding="utf-8")
            self.assertIn("assets/u1_photo.jpg", md)
            self.assertIn('creator: "users/bob"', md)
            self.assertTrue((Path(td)/"bob"/"_metadata"/"abc123.json").exists())

    def test_export_never_writes_private_or_space(self):
        class ListingApi(FakeApi):
            def paginate(self, endpoint, field, extra=None):
                if endpoint == "/api/v1/users":
                    return iter([{"name": "users/1", "username": "alice"}])
                if endpoint == "/api/v1/memos":
                    if extra.get("state") == "NORMAL":
                        return iter([
                            {"name":"memos/public01", "creator":"users/1", "content":"Public", "visibility":"PUBLIC"},
                            {"name":"memos/protect01", "creator":"users/1", "content":"Protected", "visibility":"PROTECTED"},
                            {"name":"memos/private01", "creator":"users/1", "content":"Secret", "visibility":"PRIVATE"},
                            {"name":"memos/space01", "creator":"users/1", "content":"Space note", "visibility":"SPACE"},
                        ])
                    return iter([])
                return iter([])
        with TemporaryDirectory() as td:
            folder = Path(td)
            report = export(ListingApi(), folder, [], True, True)
            exported = sorted(folder.rglob("*.md"))
            self.assertEqual(len(exported), 2)
            combined = "\n".join(x.read_text(encoding="utf-8") for x in exported)
            self.assertIn("Public", combined)
            self.assertIn("Protected", combined)
            self.assertNotIn("Secret", combined)
            self.assertNotIn("Space note", combined)
            self.assertEqual(report["skipped_records_by_visibility"], {"PRIVATE": 1, "SPACE": 1})

if __name__ == '__main__':
    unittest.main()
