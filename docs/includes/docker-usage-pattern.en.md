<!-- Bilingual pair: docker-usage-pattern.md -->

> **Docker Usage Pattern:** All da-tools commands can be executed via Docker:
> ```bash
> docker run --rm --network=host --user "$(id -u):$(id -g)" \
>   -v "$(pwd):/workspace" -w /workspace \
>   ghcr.io/vencil/da-tools:v2.9.0 <command> [flags]
> ```
> ⛔ **`--user` is not optional.** The image runs as `USER nonroot` (UID 10001)
> while the directory you mount is your own checkout (typically UID 1000), so
> any subcommand that WRITES (`init` / `scaffold` / `migrate` …) fails at the
> first write with a bare Python traceback (`PermissionError`) rather than a
> readable error. ⚠️ **`generate-routes` is the exception**: from v2.10.0 its
> `-o` catches the write failure and reports exit 2 with a line naming the flag
> ([#1617](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1617));
> the rest of that class is
> [#1641](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1641).
> Whatever the message looks like, **without `--user` the write still fails**,
> so this pattern always carries it. Read-only subcommands work without it, but
> it is harmless there.
> ⛔ **The mount must match the `--config-dir` you pass** — both spellings appear
> in these docs. A relative path (`conf.d/`) resolves against the **working
> directory inside the container**, which is what the line above mounts; if you
> instead mount only `conf.d` (e.g. `-v "$(pwd)/conf.d:/etc/config:ro"`) you must
> also pass `--config-dir /etc/config`, because keeping the relative path yields
> `ERROR: config-dir not found: conf.d/` (exit 2). Commands that write with `-o`
> also need that directory to exist outside the container.
> ⚠️ The `v2.9.0` pinned above was the GA release when this was written. Any fix
> these docs describe as arriving "from the next image onward" — or as of a
> named later version, e.g. "from v2.10.0" — requires changing that tag
> (`latest` currently resolves to the same image).
