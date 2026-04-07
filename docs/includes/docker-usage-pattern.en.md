> **Docker Usage Pattern:** All da-tools commands can be executed via Docker:
> ```bash
> docker run --rm --network=host ghcr.io/vencil/da-tools:v2.6.0 <command> [flags]
> ```
> Add volume mount for local file access: `-v $(pwd)/conf.d:/etc/config:ro`
