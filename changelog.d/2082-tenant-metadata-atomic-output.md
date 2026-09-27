---
section: Fixed
topic: confd-family
issues: [2082]
created: 2026-09-26T23:38:22+00:00
---
- **`generate_tenant_metadata --output` 寫入中斷不再截斷舊檔，`--check` 對損壞檔改回 rc 2（dx；[#2082](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2082)）**：改為在真實目標所在目錄寫隨機名暫存檔、fsync 後 `os.replace`，失敗時舊檔完好、暫存檔清掉；symlink 輸出仍是 symlink（寫進其目標），使用者自己的 `<out>.tmp` 不會被動到，模式維持 0644。凡是舊的就地寫入會成功或會拒絕的情況，結果都不變：無法建立暫存檔（目錄不可寫、唯讀 rootfs 上的單檔掛載）、hard link、目標檔的 xattr（POSIX ACL、SELinux label、`user.*` 等）與新暫存檔不一致、替換後無法重現（或無法比對）、目標是 mount point 時退回就地寫入並在 stderr 印 WARN；唯讀檔照樣被拒；覆寫時保留原擁有者。刻意的例外：資料空間滿時舊版可能靠截斷舊檔成功，新版回 rc 2 並保留舊檔。新 inode 只會帶上目錄給的預設 xattr／ACL／SELinux label，因此在 chown／chmod 之後比對新舊兩邊：一致（例如 SELinux 主機上兩邊都是目錄預設 label）照常原子替換，不一致才退回就地寫入；不複製，因為寫入 `security.*`／`trusted.*` 需要特權。比對僅限 Linux，且只比對執行者列得出的 namespace：非 root 執行看不到 `trusted.*`，root 設的 `trusted.*` 替換後仍會遺失；macOS／BSD 不比對，替換不保留 xattr（如 `com.apple.*`）。kernel 會重算的 `security.ima`／`security.evm` 不列入比對，以免 IMA／EVM 主機上每次都退回就地寫入。磁碟滿時的訊息改指向空間不足，不再叫人檢查 `--output` 的值。`--check` 遇到壞 JSON、非 object 頂層、非 UTF-8、目錄等損壞檔時回 rc 2「damaged，請重新產生」，與 rc 1「outdated」可區分。
