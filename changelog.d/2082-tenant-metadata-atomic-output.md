---
section: Fixed
topic: confd-family
issues: [2082]
created: 2026-09-26T23:38:22+00:00
---
- **`generate_tenant_metadata --output` 寫入中斷不再截斷舊檔，`--check` 對損壞檔改回 rc 2（dx；[#2082](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2082)）**：改為在真實目標所在目錄寫隨機名暫存檔、fsync 後 `os.replace`，失敗時舊檔完好、暫存檔清掉；symlink 輸出仍是 symlink（寫進其目標），使用者自己的 `<out>.tmp` 不會被動到，模式維持 0644。凡是舊的就地寫入會成功或會拒絕的情況，結果都不變：無法建立暫存檔（目錄不可寫、唯讀 rootfs 上的單檔掛載）、hard link、目標檔帶有 xattr（POSIX ACL、SELinux label、`user.*` 等；無法列出時亦同）、目標是 mount point 時退回就地寫入並在 stderr 印 WARN；唯讀檔照樣被拒；覆寫時保留原擁有者。刻意的例外：資料空間滿時舊版可能靠截斷舊檔成功，新版回 rc 2 並保留舊檔。帶 xattr 的檔案不走原子替換，因為新 inode 不會帶上舊檔的 xattr／ACL／SELinux label，而複製 `security.*`／`trusted.*` 需要特權。磁碟滿時的訊息改指向空間不足，不再叫人檢查 `--output` 的值。`--check` 遇到壞 JSON、非 object 頂層、非 UTF-8、目錄等損壞檔時回 rc 2「damaged，請重新產生」，與 rc 1「outdated」可區分。
