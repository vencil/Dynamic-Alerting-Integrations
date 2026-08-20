<!-- Bilingual pair: docker-usage-pattern.en.md -->

> **Docker 使用模式：** 所有 da-tools 指令均可透過 Docker 執行：
> ```bash
> docker run --rm --network=host -v "$(pwd):/workspace" -w /workspace \
>   ghcr.io/vencil/da-tools:v2.9.0 <command> [flags]
> ```
> ⛔ **掛法必須對得上你傳的 `--config-dir`**，兩種寫法在本文件裡都有：傳相對路徑
> （如 `conf.d/`）時它是相對**容器內的工作目錄**解析，所以上面那行掛整個工作目錄；
> 若改成只掛 `conf.d`（如 `-v "$(pwd)/conf.d:/etc/config:ro"`），就必須一併把路徑
> 改成 `--config-dir /etc/config`——沿用相對路徑會得到
> `ERROR: config-dir not found: conf.d/`（exit 2）。而 `-o` 會寫出檔案的指令也需要
> 那個目錄在容器外真的存在。
> ⚠️ 上面釘的 `v2.9.0` 是撰文時的 GA 版。文件中凡寫「下一版映像起」的修正，
> 都要把這個 tag 換掉才拿得到（`latest` 目前指向同一顆映像）。
