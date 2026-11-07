# Memos v0.31.0 — Markdown & Assets Exporter

Export the **PUBLIC and PROTECTED** memos visible through the Memos v0.31.0 REST API, using a **single instance-admin Personal Access Token**. The script is read-only: it sends GET requests and writes local Markdown files and attachments. No server upgrade or database access is required.

**Visibility policy:** `PUBLIC` ✅, `PROTECTED` ✅, `PRIVATE` ❌, `SPACE` ❌. This rule also applies to comments and IDs supplied with `--known-ids`. Archived memos are included when the API lists them. The API may not return all archived content; the output is not a database-level backup.

## Setup

Python 3.10+ required. Install the only dependency:

```bash
python -m pip install -r requirements.txt
```

Create a Memos instance-admin personal access token. Use an environment variable, **never commit a token to GitHub**.

Linux / macOS:

```bash
export MEMOS_ADMIN_TOKEN='paste-token-here'
python memos_export_v031.py --url https://memos.example.com --out ./export
```

Windows PowerShell:

```powershell
$env:MEMOS_ADMIN_TOKEN = 'paste-token-here'
python .\memos_export_v031.py --url https://memos.example.com --out .\export
```

Use a **new or empty** output folder. If you use an HTTPS site with a trusted certificate, the exporter checks TLS automatically. Do not put an admin token into issue reports, shell history, log output, or the repo.

## Output structure

```text
export/
  alice/
    2026-10-01_abc123_meeting-notes.md
    assets/
      att123_photo.jpg
    _metadata/
      abc123.json
  bob/
    2026-10-02_def456_product-notes.md
    assets/
      att456_diagram.png
  export_report.json
  README_EXPORT_SCOPE.txt
```

Every note is a separate Markdown file with YAML frontmatter (creator, dates, visibility, state, Space, tags). Original JSON metadata is saved under `_metadata/`. Attachments linked in Markdown are downloaded and rewritten to local relative paths when the attachment is hosted by the same Memos instance. External links stay external.

## Optional flags

```bash
python memos_export_v031.py --help
python memos_export_v031.py --url https://memos.example.com --out ./export --no-comments
python memos_export_v031.py --url https://memos.example.com --out ./export --no-assets
python memos_export_v031.py --url https://memos.example.com --out ./export --known-ids memo_ids.txt
```

`--known-ids` accepts one `memos/<uid>` or `<uid>` per line. Entries with `PRIVATE` or `SPACE` visibility are **still excluded**. It is only useful when you know additional non-private memo UIDs that were not in the listing.

The script paginates the user list, normal and archived memos, comments, and attachment lists. It logs download failures and returns nonzero when errors occur. `export_report.json` includes visibility filtering and error summaries. If you see failed attachment downloads, retain the report and retry with a fresh output folder.

## Test

```bash
python -m unittest discover -s . -p 'test_*.py' -v
```

Test coverage is local and simulated; check results against your real Memos deployment before relying on it as a backup.

## Limitations

- With an admin token, the API **lists only visibility-eligible content**. This script deliberately excludes all `PRIVATE` and `SPACE` memos, even if the admin could fetch them individually.
- File attachments stored in Memos are downloaded where accessible. External URLs are not mirrored. Remote files are left linked externally.
- An API export is not a complete instance backup. For disaster recovery back up the database and attachment storage separately.
- Public/Protected notes should be handled carefully after export; the generated files are unencrypted.

Source compatibility target: **Memos v0.31.0**. Earlier or later versions may use different endpoints.

## Database administration tools

For offline SQLite memo ownership transfer (including `PRIVATE` memos), see [`scripts/README.md`](scripts/README.md). Unlike the exporter above, this is a **database-changing** operation, requires the Memos service to be stopped, and creates a backup before applying changes.
