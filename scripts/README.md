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


## 在同一個 Docker container 內暫停 / 恢復 Memos（Supervisor）

**原始 `neosmemo/memos:0.31.0` 不支援這種操作**：其 entrypoint 會 `exec` Memos，因此 server 是 container 的主程序；結束它會連帶停止 container。要讓 container 持續運行，需要**重新建置／部署一次**以 Supervisor 作為 PID 1，讓 Memos 成為 Supervisor 管理的子程序。之後每次轉讓只停 Memos 程序、不停 container。

自訂 Alpine image 的 Dockerfile 可加入：

```dockerfile
USER root
RUN apk add --no-cache python3 supervisor
COPY scripts/docker/supervisord.conf /etc/supervisord.conf
ENTRYPOINT ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisord.conf"]
```

如果你的 Docker build context **沒有**這個 repo 的 `scripts/docker/`，請把同名設定檔以現有建置系統的方式放進 image，或改用 inline `RUN printf` 建立設定檔。Supervisor 設定刻意沿用 **原本的** `/usr/local/memos/entrypoint.sh /usr/local/memos/memos`，保留自動調整 volume owner、降權、讀取 `MEMOS_DSN_FILE` 等既有行為。Supervisor 控制 socket 只允許 root 存取。

使用方式（先確定 server 已啟動，並先執行轉讓的 dry-run）：

```bash
# container 名稱自行替換；確認新 image 已由 supervisord 管理
docker exec -u 0 YOUR_CONTAINER supervisorctl status memos

# 腳本已放在容器 /opt/memos-tools/ 內，兩支腳本必須在同一個資料夾：
docker exec -u 0 YOUR_CONTAINER sh /opt/memos-tools/with_memos_stopped.sh \
  --db /var/opt/memos/memos_prod.db \
  --from-user alice --to-user bob --memo abc123 \
  --apply --confirm 'alice->bob'

# 確認恢復
docker exec -u 0 YOUR_CONTAINER supervisorctl status memos
```

`with_memos_stopped.sh` 會先確認 Memos 為 RUNNING，接著執行 `supervisorctl stop memos`，將 `--stopped` 傳入 Python 轉讓工具，並在**正常結束或出錯**時嘗試 `supervisorctl start memos`。如果整個 exec 行程被 `SIGKILL`、機器當機，或啟動失敗，cleanup 無法保證執行；請手動使用 `supervisorctl start memos` 復原。

如需手動操作，依序用 `supervisorctl stop memos` → Python 轉讓命令（加 `--apply --stopped --confirm`）→ `supervisorctl start memos`。**服務停止期間網站會暫時不可用**，但 container 本身保持 RUNNING。不要使用 `kill -9`、`SIGSTOP/SIGCONT` 或直接關閉 container 的 PID 1 來替代正常關閉。先備份整個 volume，轉讓工具的 SQLite 備份不包含獨立附件檔。
