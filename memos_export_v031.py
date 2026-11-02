#!/usr/bin/env python3
"""Read-only Memos v0.31.0 REST API -> Markdown + assets.

Exports only PUBLIC and PROTECTED memos (including archived records if the API
returns them). PRIVATE and SPACE memos are deliberately excluded.
This is an API-visible export, not a complete database backup.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import quote, unquote, urlsplit

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as exc:
    raise SystemExit("Missing dependency: python -m pip install requests>=2.31") from exc

PAGE_SIZE = 200
ALLOWED_VISIBILITIES = frozenset({"PUBLIC", "PROTECTED"})
ASSET_URL_RE = re.compile(r"https?://[^\s)\]<>\"']+|/file/attachments/[^\s)\]<>\"']+")
INVALID_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def safe_filename(name: str, limit: int = 96) -> str:
    name = INVALID_PATH_CHARS.sub("_", str(name or "untitled"))
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:limit].rstrip(" .") or "untitled"
    if WINDOWS_RESERVED.match(name):
        name = "_" + name
    return name


def uid_from_resource(resource: str) -> str:
    return resource.rstrip("/").split("/")[-1]


def yaml_string(value: Any) -> str:
    # JSON strings are valid YAML double-quoted scalars.
    return json.dumps(value, ensure_ascii=False)


def nice_title(content: str, title: str | None) -> str:
    if title:
        return safe_filename(title, 55)
    first = next((line.strip() for line in (content or "").splitlines() if line.strip()), "Untitled")
    first = re.sub(r"^[#>*\-\s]+", "", first)
    first = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", first)
    return safe_filename(first or "Untitled", 55)


def memo_filename(memo: dict[str, Any]) -> str:
    uid = safe_filename(uid_from_resource(memo["name"]), 50)
    date = (memo.get("createTime") or "unknown-date")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date = "unknown-date"
    title = nice_title(memo.get("content", ""), (memo.get("property") or {}).get("title"))
    return f"{date}_{uid}_{title}.md"


def rewrite_asset_links(content: str, mapping: dict[str, str], base_url: str) -> str:
    instance = urlsplit(base_url)

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        parsed = urlsplit(raw)
        if parsed.netloc and (parsed.hostname != instance.hostname or parsed.port != instance.port):
            return raw
        path = unquote(parsed.path)
        m = re.fullmatch(r"/file/attachments/([^/]+)(?:/.*)?", path)
        if not m:
            return raw
        new_path = mapping.get(m.group(1))
        if not new_path:
            return raw
        # Relative Markdown links use URL encoding to support spaces and Unicode.
        return quote(new_path, safe="/-_.~")

    return ASSET_URL_RE.sub(replace, content or "")


class Api:
    def __init__(self, base: str, token: str, timeout: int = 45):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        retry = Retry(total=3, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET"], respect_retry_after_header=True)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self.session.get(self.base + endpoint, params=params, timeout=self.timeout)
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"GET {endpoint}: HTTP {r.status_code}: {r.text[:300]}") from e
        try:
            data = r.json()
        except ValueError as e:
            raise RuntimeError(f"GET {endpoint}: expected JSON, received {r.headers.get('Content-Type')}") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"GET {endpoint}: unexpected response type")
        return data

    def paginate(self, endpoint: str, field: str, extra: dict[str, Any] | None = None):
        seen_tokens: set[str] = set()
        token = ""
        for _ in range(100000):
            params: dict[str, Any] = {"pageSize": PAGE_SIZE}
            if extra:
                params.update(extra)
            if token:
                params["pageToken"] = token
            data = self.get_json(endpoint, params=params)
            entries = data.get(field, [])
            if not isinstance(entries, list):
                raise RuntimeError(f"GET {endpoint}: expected an array named {field}")
            yield from entries
            token = data.get("nextPageToken", "")
            if not token:
                return
            if token in seen_tokens:
                raise RuntimeError(f"GET {endpoint}: repeated nextPageToken")
            seen_tokens.add(token)
        raise RuntimeError(f"GET {endpoint}: pagination exceeded maximum pages")

    def download(self, endpoint: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            with self.session.get(self.base + endpoint, timeout=(15, 180), stream=True) as resp:
                resp.raise_for_status()
                if "text/html" in resp.headers.get("Content-Type", "").lower():
                    raise RuntimeError("Received HTML instead of attachment bytes (login/proxy error?)")
                with part.open("wb") as out:
                    for chunk in resp.iter_content(chunk_size=128 * 1024):
                        if chunk:
                            out.write(chunk)
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)


def save_memo(api: Api, memo: dict[str, Any], folder: Path, errors: list[str],
              no_assets: bool) -> tuple[int, int]:
    attachments = memo.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []
    # Some API responses omit attachments. Query the per-memo endpoint then.
    if not attachments and not no_assets:
        try:
            attachments = list(api.paginate(f"/api/v1/{memo['name']}/attachments", "attachments"))
        except Exception as e:
            errors.append(f"Attachment listing failed for {memo['name']}: {e}")
    mapping: dict[str, str] = {}
    files_downloaded = 0
    external_count = 0
    for item in attachments:
        uid = uid_from_resource(item.get("name", ""))
        if not uid or uid == "attachments":
            errors.append(f"Invalid attachment name for {memo['name']}: {item!r}")
            continue
        if item.get("externalLink"):
            external_count += 1
            # External URLs are retained as remote links, not downloaded.
            continue
        if no_assets:
            continue
        original_name = item.get("filename") or "file"
        stored_name = f"{safe_filename(uid, 48)}_{safe_filename(original_name, 110)}"
        target = folder / "assets" / stored_name
        # Route verified in Memos v0.31.0, and intentionally same-origin.
        route = f"/file/attachments/{quote(uid, safe='')}/{quote(original_name, safe='')}"
        try:
            api.download(route, target)
            mapping[uid] = f"assets/{stored_name}"
            files_downloaded += 1
        except Exception as e:
            errors.append(f"Attachment download failed for {memo['name']} / {uid}: {e}")

    creator = memo.get("creator", "")
    metadata = [
        "---",
        f"memo_name: {yaml_string(memo['name'])}",
        f"creator: {yaml_string(creator)}",
        f"create_time: {yaml_string(memo.get('createTime', ''))}",
        f"update_time: {yaml_string(memo.get('updateTime', ''))}",
        f"visibility: {yaml_string(memo.get('visibility', ''))}",
        f"state: {yaml_string(memo.get('state', ''))}",
        f"space: {yaml_string(memo.get('space', ''))}",
        f"pinned: {'true' if memo.get('pinned') else 'false'}",
        f"tags: {yaml_string(memo.get('tags', []))}",
    ]
    if memo.get("parent"):
        metadata.append(f"parent: {yaml_string(memo['parent'])}")
    metadata.extend(["---", ""])
    content = rewrite_asset_links(memo.get("content", ""), mapping, api.base)
    dest = folder / memo_filename(memo)
    dest.write_text("\n".join(metadata) + "\n" + content + "\n", encoding="utf-8")
    # Retain original structured metadata and attachment identifiers for future migration.
    meta_dir = folder / "_metadata"
    meta_dir.mkdir(exist_ok=True)
    (meta_dir / f"{safe_filename(uid_from_resource(memo['name']), 70)}.json").write_text(
        json.dumps(memo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return files_downloaded, external_count


def read_known_ids(file: Path | None) -> list[str]:
    if not file:
        return []
    ids = []
    for raw in file.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        uid = uid_from_resource(raw)
        if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", uid):
            raise ValueError(f"Invalid Memo UID in {file}: {raw}")
        ids.append(uid)
    return ids


def export(api: Api, out: Path, extra_ids: list[str], no_assets: bool,
           no_comments: bool) -> dict[str, Any]:
    errors: list[str] = []
    print("Getting user list ...", flush=True)
    users = list(api.paginate("/api/v1/users", "users"))
    users_by_resource = {u.get("name"): u.get("username") or uid_from_resource(u.get("name", "")) for u in users}
    if not users:
        raise RuntimeError("User list empty: cannot safely group the export")
    print(f"Discovered {len(users)} user(s)", flush=True)

    memos: dict[str, dict[str, Any]] = {}
    skipped_visibilities: Counter[str] = Counter()
    for state in ("NORMAL", "ARCHIVED"):
        print(f"Listing {state} memos ...", flush=True)
        for memo in api.paginate("/api/v1/memos", "memos", {"state": state}):
            if not memo.get("name"):
                errors.append(f"Memo without name: {memo!r}")
                continue
            visibility = memo.get("visibility", "UNKNOWN")
            if visibility not in ALLOWED_VISIBILITIES:
                skipped_visibilities[str(visibility)] += 1
                continue
            memos[memo["name"]] = memo

    known_added = 0
    for uid in extra_ids:
        name = "memos/" + uid
        if name in memos:
            continue
        try:
            memo = api.get_json(f"/api/v1/{name}")
            if memo.get("name"):
                visibility = memo.get("visibility", "UNKNOWN")
                if visibility in ALLOWED_VISIBILITIES:
                    memos[memo["name"]] = memo
                    known_added += 1
                else:
                    skipped_visibilities[str(visibility)] += 1
            else:
                errors.append(f"GET {name} returned no memo name")
        except Exception as e:
            errors.append(f"Known Memo UID {uid} inaccessible: {e}")

    comment_added = 0
    if not no_comments:
        queue = deque(memos.keys())
        visited: set[str] = set()
        while queue:
            parent = queue.popleft()
            if parent in visited:
                continue
            visited.add(parent)
            try:
                for comment in api.paginate(f"/api/v1/{parent}/comments", "memos"):
                    name = comment.get("name")
                    if not name:
                        errors.append(f"Comment without name under {parent}")
                    elif comment.get("visibility") not in ALLOWED_VISIBILITIES:
                        skipped_visibilities[str(comment.get("visibility", "UNKNOWN"))] += 1
                    elif name not in memos:
                        memos[name] = comment
                        comment_added += 1
                        queue.append(name)
            except Exception as e:
                errors.append(f"Comment listing failed for {parent}: {e}")

    print(f"Exporting {len(memos)} PUBLIC/PROTECTED memos ...", flush=True)
    by_user = Counter()
    attachments_downloaded = 0
    external_attachments = 0
    # Avoid sanitization collisions across usernames.
    folder_by_creator: dict[str, Path] = {}
    used_foldernames: set[str] = set()
    for creator in sorted({m.get("creator", "") for m in memos.values()}):
        username = users_by_resource.get(creator) or uid_from_resource(creator) or "unknown"
        foldername = safe_filename(username)
        base_name = foldername
        i = 2
        while foldername.casefold() in used_foldernames:
            foldername = f"{base_name}_{i}"
            i += 1
        used_foldernames.add(foldername.casefold())
        folder_by_creator[creator] = out / foldername

    for n, memo in enumerate(sorted(memos.values(), key=lambda m: (m.get("creator", ""), m.get("name", ""))), 1):
        creator = memo.get("creator", "")
        folder = folder_by_creator[creator]
        folder.mkdir(parents=True, exist_ok=True)
        count, external = save_memo(api, memo, folder, errors, no_assets)
        attachments_downloaded += count
        external_attachments += external
        by_user[folder.name] += 1
        if n % 50 == 0 or n == len(memos):
            print(f"  exported {n}/{len(memos)}", flush=True)

    report = {
        "memos_version": "0.31.0",
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "export_method": "API-only (one instance admin token)",
        "all_instance_memos_exported": False,
        "can_guarantee_full_instance_backup": False,
        "exported_visibilities": sorted(ALLOWED_VISIBILITIES),
        "excluded_visibilities": ["PRIVATE", "SPACE"],
        "skipped_records_by_visibility": dict(sorted(skipped_visibilities.items())),
        "reason": ("Only PUBLIC and PROTECTED content returned by the API is exported. "
                   "PRIVATE and SPACE are intentionally excluded. "
                   "The API may omit archived records or comments; this is not a full database backup."),
        "total_users_in_api": len(users),
        "returned_memo_records": len(memos),
        "memos_from_known_uid_file": known_added,
        "comments_discovered": comment_added,
        "memos_by_folder": dict(sorted(by_user.items())),
        "attachments_downloaded": attachments_downloaded,
        "external_attachments_not_downloaded": external_attachments,
        "attachments_intentionally_skipped": no_assets,
        "comments_intentionally_skipped": no_comments,
        "errors": errors,
    }
    (out / "export_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "README_EXPORT_SCOPE.txt").write_text(
        "Memos export scope: PUBLIC and PROTECTED only.\n"
        "PRIVATE and SPACE memos were intentionally excluded, even if visible to admin.\n"
        "Results reflect records returned by Memos v0.31.0 API, not a complete database snapshot.\n"
        "See export_report.json for counts, skipped visibility categories, and errors.\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Memos v0.31.0 PUBLIC/PROTECTED records to Markdown and assets")
    parser.add_argument("--url", required=True, help="Memos origin, e.g. https://memos.example.com")
    parser.add_argument("--out", help="New output directory; defaults to a timestamped folder")
    parser.add_argument("--known-ids", type=Path, help="Optional .txt with Memo UIDs, one per line")
    parser.add_argument("--no-assets", action="store_true", help="Skip attachment byte downloads")
    parser.add_argument("--no-comments", action="store_true", help="Skip comment traversal")
    parser.add_argument("--timeout", type=int, default=45, help="JSON API timeout, seconds")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/") or parsed.query:
        parser.error("--url must be an origin URL, e.g. https://memos.example.com")
    token = os.environ.get("MEMOS_ADMIN_TOKEN", "").strip()
    if not token:
        parser.error("Set MEMOS_ADMIN_TOKEN in your environment (never pass your token on the command line)")
    out = Path(args.out or "memos-export-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    if out.exists() and any(out.iterdir()):
        parser.error(f"Output directory already contains files: {out} (use a fresh directory)")
    out.mkdir(parents=True, exist_ok=True)
    try:
        known_ids = read_known_ids(args.known_ids)
        report = export(Api(url, token, timeout=args.timeout), out, known_ids, args.no_assets, args.no_comments)
    except (requests.RequestException, RuntimeError, OSError, ValueError) as e:
        print(f"FATAL: {e}", file=sys.stderr)
        print(f"Incomplete output may remain in: {out}", file=sys.stderr)
        return 1
    print("\nOutput:", out.resolve())
    print("Memo records:", report["returned_memo_records"])
    print("Downloaded attachment files:", report["attachments_downloaded"])
    print("Errors:", len(report["errors"]))
    print("Scope: PUBLIC and PROTECTED; PRIVATE and SPACE are intentionally excluded.")
    return 2 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())