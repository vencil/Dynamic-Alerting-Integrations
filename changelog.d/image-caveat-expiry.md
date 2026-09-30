---
section: Added
topic: docs
issues: []
created: 2026-09-28T10:30:00+08:00
---
- **「已發布的映像還是舊行為」的註記會隨版本到期**：文件裡提醒「v2.9.0 映像還是舊行為」的 59 處都加上 `<!-- image-caveat: v2.9.0 -->` 標記。新增 `image-caveat-check`（pre-commit 與 `make pre-tag`）：da-tools VERSION 超過標記版號時失敗，發版時必須把這些註記拿掉或改寫；用了「vX.Y.Z 映像」「the image you have」這類句型卻沒有標記的段落也會擋下。同一支 lint 也守「vX.Y.Z 起行為改了」這類敘述（本輪決策 R2）：這類段落加 `<!-- since: vX.Y.Z -->`，版號不在 CHANGELOG 的 `## [vX.Y.Z]` 標題裡而低於最新一版（被跳過的版號）即失敗；`make pre-tag` 另帶 `--pre-tag`，要打的版本還沒有標題也失敗；寫了這類句型、指向未發布版號卻沒有標記的段落一併擋下。釘版的映像參照仍由 `bump_docs.py --tools` 改寫、`--check` 把關。
