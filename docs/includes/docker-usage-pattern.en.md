<!-- Bilingual pair: docker-usage-pattern.md -->

> **Docker Usage Pattern:** All da-tools commands can be executed via Docker:
> ```bash
> docker run --rm --network=host -v "$(pwd):/workspace" -w /workspace \
>   ghcr.io/vencil/da-tools:v2.9.0 <command> [flags]
> ```
> ⛔ **The mount must match the `--config-dir` you pass** — both spellings appear
> in these docs. A relative path (`conf.d/`) resolves against the **working
> directory inside the container**, which is what the line above mounts; if you
> instead mount only `conf.d` (e.g. `-v "$(pwd)/conf.d:/etc/config:ro"`) you must
> also pass `--config-dir /etc/config`, because keeping the relative path yields
> `ERROR: config-dir not found: conf.d/` (exit 2). Commands that write with `-o`
> also need that directory to exist outside the container.
> ⚠️ The `v2.9.0` pinned above was the GA release when this was written. Any fix
> these docs describe as arriving "from the next image onward" requires changing
> that tag (`latest` currently resolves to the same image).
