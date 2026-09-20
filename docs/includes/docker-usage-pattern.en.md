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
> ⛔ **Every other example omits the prefix above and writes only
> `da-tools <command>`.** Copying that form straight into a terminal gets you
> `bash: da-tools: command not found` (rc 127) — `da-tools` is not an executable
> you can put on `$PATH`, and no `install` step makes one appear. To make those
> examples literally copy-pasteable, define a function of the same name in your
> shell first:
> ```bash
> # bash: paste into ~/.bashrc, or run once in the current shell
> da-tools() {
>   local tty=()
>   [ -t 0 ] && [ -t 1 ] && tty=(-t)
>   docker run --rm -i "${tty[@]}" --network=host \
>     --user "$(id -u):$(id -g)" \
>     -v "$(pwd):/workspace" -w /workspace \
>     ghcr.io/vencil/da-tools:v2.9.0 "$@"
> }
> ```
> ⛔ **A hyphen in a function name is a bash extension**: `/bin/sh` (dash)
> answers `Syntax error: Bad function name` with exit code 2 (measured). In a
> `#!/bin/sh` script use another name (e.g. `datools`) or write the full
> `docker run` out.
> ⛔ **Do not wire this into CI.** A function lives only in the shell that
> defined it, and a CI `script:` is a different process — in CI use the full
> `docker run`, or make the image the job's container.
> ⚠️ It mounts `$(pwd)`, so run it **from your repository root** for the
> relative paths in the examples (`--config-dir conf.d/`) to line up — same
> reason as the paragraph above.
