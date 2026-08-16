<!-- Bilingual pair: docker-usage-pattern.en.md -->

> **Docker 使用模式：** 所有 da-tools 指令均可透過 Docker 執行：
> ```bash
> docker run --rm --network=host -v "$(pwd):/workspace" -w /workspace \
>   ghcr.io/vencil/da-tools:v2.9.0 <command> [flags]
> ```
> ⛔ 掛的是**整個工作目錄**，不是只掛 `conf.d`。文件裡的範例一律用 `conf.d/` 這類
> **相對路徑**，只掛 `conf.d` 到別的位置會得到 `ERROR: config-dir not found: conf.d/`
> （exit 2）；而 `-o` 會寫出檔案的指令也需要那個目錄在容器外真的存在。
