<!-- Bilingual pair: docker-usage-pattern.en.md -->

> **Docker 使用模式：** 所有 da-tools 指令均可透過 Docker 執行：
> ```bash
> docker run --rm --network=host --user "$(id -u):$(id -g)" \
>   -v "$(pwd):/workspace" -w /workspace \
>   ghcr.io/vencil/da-tools:v2.9.0 <command> [flags]
> ```
> ⛔ **`--user` 不是可選的**：映像以 `USER nonroot`（UID 10001）執行，而你掛進去的
> 目錄屬於你自己（通常 UID 1000）⇒ 任何**會寫檔**的子命令（`init` / `scaffold` /
> `migrate` …）都會在寫入時得到裸 Python traceback（`PermissionError`），而不是可讀的
> 錯誤訊息。⚠️ **`generate-routes` 是例外**：它的 `-o` 自 v2.10.0 起把寫入失敗攔成結束碼 2
> 與一行指名 `-o` 的訊息（[#1617](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1617)）；
> 其餘子命令的那一半仍是 [#1641](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1641)。
> 無論訊息長什麼樣，**沒有 `--user` 就是寫不出檔**，所以這個範本一律帶著；只讀的子命令
> 不加也能跑，但加了無害。
> ⛔ **掛法必須對得上你傳的 `--config-dir`**，兩種寫法在本文件裡都有：傳相對路徑
> （如 `conf.d/`）時它是相對**容器內的工作目錄**解析，所以上面那行掛整個工作目錄；
> 若改成只掛 `conf.d`（如 `-v "$(pwd)/conf.d:/etc/config:ro"`），就必須一併把路徑
> 改成 `--config-dir /etc/config`——沿用相對路徑會得到
> `ERROR: config-dir not found: conf.d/`（exit 2）。而 `-o` 會寫出檔案的指令也需要
> 那個目錄在容器外真的存在。
> ⚠️ 上面釘的 `v2.9.0` 是撰文時的 GA 版。文件中凡把某個修正描述成「下一版映像起」
> 或指名一個晚於它的版本（例如「自 v2.10.0 起」），都要把這個 tag 換掉才拿得到
> （`latest` 目前指向同一顆映像）。
