# Memos 管理工具（v0.31.0）

這裡的工具與根目錄唯讀匯出器分開：**管理腳本可能改動 Memos 資料庫**。請先確認 Memos 版本與資料庫類型。

## 轉讓 Memo 所有權：`transfer_memo_owner.py`

Memos v0.31.0 的 `UpdateMemo` API **沒有**修改作者 / `creator_id` 的功能。這支工具直接在 **SQLite** 資料庫中轉讓 Memo 的作者，包含 `PRIVATE` Memo。不使用或要求 admin API Token。**不支援 PostgreSQL / MySQL**；不要把其他類型的 DB dump 當 SQLite 檔案使用。

提供兩種模式：**`--online`** 可在 Memos 持續運行時轉讓（實驗性；不需 Supervisor、不必停 container），**`--offline`**（同 `--stopped`）要求先停止 Memos 及其他資料庫寫入者，更適合敏感或大批量操作。兩者都會在正式執行前使用 SQLite Backup API 建立一致性備份（權限 0600）；不包含磁碟或 S3 附件檔案。**先備份完整資料 volume，並在副本上測試，再用於正式環境。**

```bash
# 1. 預覽：server 正在運行時即可操作，不會寫入
python scripts/transfer_memo_owner.py \
  --db /path/to/memos_prod.db \
  --from-user alice --to-user bob \
  --memo memos/abc123

# 2. 線上轉讓：server 可繼續運行（實驗性）
python scripts/transfer_memo_owner.py \
  --db /path/to/memos_prod.db \
  --from-user alice --to-user bob \
  --memo memos/abc123 \
  --apply --online --lock-timeout 10 --confirm 'alice->bob'

# 3. 離線轉讓：先停止 Memos 和其他資料庫寫入者，再正式轉讓
python scripts/transfer_memo_owner.py \
  --db /path/to/memos_prod.db \
  --from-user alice --to-user bob \
  --memo memos/abc123 \
  --apply --offline --confirm 'alice->bob'

# 4. 批次轉讓 A 的全部 Memo（含封存與留言）：先預覽，再確認
python scripts/transfer_memo_owner.py \
  --db /path/to/memos_prod.db \
  --from-user alice --to-user bob --all
```

`--memo` 可以重複指定多個 UID，也接受 `memos/<uid>`；`--all` 則選取原作者的所有 Memo，兩者不能合用。帳號名稱大小寫必須完全相同。**預設 dry-run，不修改任何資料。**正式執行必須同時提供 `--apply`、`--online` **或** `--offline`，以及正確的 `--confirm`；未指定執行模式會拒絕寫入。舊版 `--stopped` 是 `--offline` 的相容別名。`--lock-timeout` 控制 SQLite 鎖定等待時間（0–300 秒，預設 10 秒）；可用 `--backup /safe/path/backup.sqlite3` 指定尚不存在的備份路徑。

**線上模式的交易保護：**先用 SQLite Backup API 做一致性備份，再以 `BEGIN IMMEDIATE` 等待並取得 SQLite 寫入鎖；取得鎖後，重新確認來源／目標帳號、選定 Memo（包括內容與更新時間）和每筆綁定附件（身分與更新時間）。若備份期間發生不一致，會拒絕轉讓而不寫入；若寫入途中出錯則 `ROLLBACK`；鎖定逾時也會拒絕寫入。成功時一次 `COMMIT`。備份期間可允許其他寫入，**但取得鎖至提交這段期間會短暫阻擋其他 SQLite writers**。

轉讓會在同一個 SQLite transaction 更新 Memo 的 `creator_id`，以及**綁定在這些 Memo 上、由原作者持有**的附件 `creator_id`。Memo UID、內容、時間、可見性、封存狀態、關聯都不會修改。若存在其他作者持有的綁定附件，工具會拒絕轉讓，避免轉讓後新作者無法完整管理它們。

**權限與限制：**

- 新作者帳號必須是 `NORMAL`；對 `SPACE` Memo，新作者需有該 Space 的有效成員資格，或是 instance admin，否則拒絕。
- 留言也是獨立 Memo，轉讓父文不會自動改留言作者；`--all` 則會轉讓原作者的所有留言。Reactions、未綁定的附件及 Space 成員資格也不會修改。
- 只針對 Memos **v0.31.0 SQLite schema** 設計。直接修改資料庫是沒有官方所有權轉讓 API 時的管理操作，升級後可能需要調整；執行前自行核對版本並先在資料庫副本驗證。
- PRIVATE Memo 轉讓後，新作者可依新作者身分存取，原作者則會失去基於作者身分的存取權。**線上模式不保證伺服器快取、既有 WebSocket / Session、或進行中的 API 請求已經同步**；請分別以兩個帳號測試文章、附件與編輯功能，必要時重啟 server。
- 對線上模式，大量 Memo、繁忙 SQLite 或可能與編輯請求同時發生的寫入，仍建議改用離線模式。SQLite 的原子性不代表 Memos 的應用層快取一定立即失效。
- 資料庫備份含私人內容，請保護備份檔案並妥善管理保存期限。資料庫備份不等於包含外部附件儲存的完整災難復原備份。

測試：

```bash
python -m unittest -v test_transfer_memo_owner.py
```


## Docker 內線上使用（不必停 server）

若 image 內有 `python3`，將 `transfer_memo_owner.py` 放入 container 可直接執行，**不需要安裝 pip 套件**：

```bash
docker exec -u 0 YOUR_CONTAINER python3 \
  /opt/memos-tools/transfer_memo_owner.py \
  --db /var/opt/memos/memos_prod.db \
  --from-user alice --to-user bob --memo abc123

# 確認乾跑結果後，才執行線上轉讓
docker exec -u 0 YOUR_CONTAINER python3 \
  /opt/memos-tools/transfer_memo_owner.py \
  --db /var/opt/memos/memos_prod.db \
  --from-user alice --to-user bob --memo abc123 \
  --apply --online --lock-timeout 10 --confirm 'alice->bob'
```

這個模式**不需要 Supervisor**，也不要求重新建置 Docker image（前提是 container 能執行 Python 3；否則需在相同 volume 上另行提供 Python 執行環境）。必須針對**實際運作的**資料庫檔與同一個 volume，不要在正式環境對手動複製、過期的 DB snapshot 寫入。

## 在同一個 Docker container 內暫停 / 恢復 Memos（Supervisor，離線模式）

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
