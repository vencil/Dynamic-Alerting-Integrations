> **Docker 使用模式：** 所有 da-tools 指令均可透過 Docker 執行：
> ```bash
> docker run --rm --network=host ghcr.io/vencil/da-tools:v2.3.0 <command> [flags]
> ```
> 需要存取本地檔案時加入 volume mount：`-v $(pwd)/conf.d:/etc/config:ro`
