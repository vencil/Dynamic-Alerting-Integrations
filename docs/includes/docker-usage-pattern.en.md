<!-- Bilingual pair: docker-usage-pattern.md -->

> **Docker Usage Pattern:** All da-tools commands can be executed via Docker:
> ```bash
> docker run --rm --network=host -v "$(pwd):/workspace" -w /workspace \
>   ghcr.io/vencil/da-tools:v2.9.0 <command> [flags]
> ```
> ⛔ Mount the **whole working directory**, not just `conf.d`. Examples in the
> docs use **relative** paths like `conf.d/`, so mounting `conf.d` elsewhere
> yields `ERROR: config-dir not found: conf.d/` (exit 2); and commands that
> write with `-o` need that directory to exist outside the container too.
