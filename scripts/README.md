# Memos 管理工具（v0.31.0）

這裡的工具與根目錄唯讀匯出器分開：**管理腳本可能改動 Memos 資料庫**。請先確認 Memos 版本與資料庫類型。

## 轉讓 Memo 所有權：`transfer_memo_owner.py`

Memos v0.31.0 的 `UpdateMemo` API **沒有**修改作者 / `creator_id` 的功能。這支工具直接在 **SQLite** 資料庫中轉讓 Memo 的作者，包含 `PRIVATE` Memo。不使用或要求 admin API Token。**不支援 PostgreSQL / MySQL**；不要把其他類型的 DB dump 當 SQLite 檔案使用。

必須在 **Memos 服務及所有寫入同一資料庫的程序均已停止** 後，才能正式執行。先建立完整的資料庫與附件儲存備份。工具還會在套用變更前自動生成一份 SQLite 備份（檔案權限 0600）；但它不備份磁碟或 S3 的附件內容。

```bash
# 1. 預覽：服務運行時也可以，但建議在服務停止後操作
python scripts/transfer_memo_owner.py \
  --db /path/to/memos_prod.db \
  --from-user alice --to-user bob \
  --memo memos/abc123

# 2. 停止 Memos 和其他資料庫寫入者後，再正式轉讓
python scripts/transfer_memo_owner.py \
  --db /path/to/memos_prod.db \
  --from-user alice --to-user bob \
  --memo memos/abc123 \
  --apply --stopped --confirm 'alice->bob'

# 3. 批次轉讓 A 的全部 Memo（含封存與留言）：先預覽，再確認
python scripts/transfer_memo_owner.py \
  --db /path/to/memos_prod.db \
  --from-user alice --to-user bob --all
```

`--memo` 可以重複指定多個 UID，也接受 `memos/<uid>`；`--all` 則選取原作者的所有 Memo，兩者不能合用。帳號名稱大小寫必須完全相同。**預設 dry-run，不修改任何資料。**正式執行必須同時提供 `--apply`、`--stopped` 與正確的 `--confirm`。可以用 `--backup /safe/path/backup.sqlite3` 指定尚不存在的備份路徑。

轉讓會在同一個 SQLite transaction 更新 Memo 的 `creator_id`，以及**綁定在這些 Memo 上、由原作者持有**的附件 `creator_id`。Memo UID、內容、時間、可見性、封存狀態、關聯都不會修改。若存在其他作者持有的綁定附件，工具會拒絕轉讓，避免轉讓後新作者無法完整管理它們。

**權限與限制：**

- 新作者帳號必須是 `NORMAL`；對 `SPACE` Memo，新作者需有該 Space 的有效成員資格，或是 instance admin，否則拒絕。
- 留言也是獨立 Memo，轉讓父文不會自動改留言作者；`--all` 則會轉讓原作者的所有留言。Reactions、未綁定的附件及 Space 成員資格也不會修改。
- 只針對 Memos **v0.31.0 SQLite schema** 設計。直接修改資料庫是沒有官方所有權轉讓 API 時的管理操作，升級後可能需要調整；執行前自行核對版本並先在資料庫副本驗證。
- PRIVATE Memo 轉讓後，新作者可依新作者身分存取，原作者則會失去基於作者身分的存取權。執行後重啟 Memos，分別以兩個帳號驗證文章、附件與編輯功能。
- 資料庫備份含私人內容，請保護備份檔案並妥善管理保存期限。資料庫備份不等於包含外部附件儲存的完整災難復原備份。

測試：

```bash
python -m unittest -v test_transfer_memo_owner.py
```
