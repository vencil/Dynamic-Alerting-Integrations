---
title: "Migration Toolkit Installation Guide (da-tools / da-guard)"
tags: [migration, toolkit, installation, da-guard, da-tools, phase-c, v2.8.0]
audience: [platform-engineers, sre, customer-ops]
version: v2.7.0
lang: en
---

# Migration Toolkit Installation Guide (da-tools / da-guard)

> **Language / 語言：** **English (Current)** | [中文](./migration-toolkit-installation.md)

> **Applies to**: `tools/v2.8.0` and later (every Release after C-11 packaging lands).  
> Older releases (≤ `tools/v2.7.0`) only ship the Docker image delivery path.

## Why a Migration Toolkit

Importing a customer's existing Prometheus alerting rule corpus (PromRule CRDs / Alertmanager YAML) into the Dynamic Alerting Platform's conf.d/ Profile-as-Directory-Default architecture ([ADR-019](adr/019-profile-as-directory-default.en.md)) requires a chain of tools:

```
PromRule corpus → C-8 parser → C-9 cluster + translator → C-10 batch PR → C-12 guard validation → conf.d/
```

C-11 Migration Toolkit packages this pipeline into a customer-runnable bundle that works offline, in air-gapped environments, and with verifiable binary integrity.

**Currently included**:

| Tool | Interface | Purpose |
|---|---|---|
| `da-tools` | Python CLI | 41+ existing ops / config-gen / policy-eval subcommands; new `guard` subcommand wrapping da-guard |
| `da-guard` | Go binary | C-12 Dangling Defaults Guard CLI (schema / routing / cardinality / redundant-override 4-tier check) |

When future PRs add `da-toolkit batch-pr` / `da-toolkit parser` Go subcommands, this guide will update.

## Three delivery paths

Pick one based on your environment:

| Path | Best for | Setup cost | Upgrade cost |
|---|---|---|---|
| **A. Docker pull from ghcr.io** | Customers with outbound registry access (most common) | Low | Low (`docker pull :v<new>`) |
| **B. Static binary download** | No-Docker environments; just need `da-guard` for pre-commit / GitHub Actions | Medium | Medium (re-download + replace) |
| **C. Air-gapped tar import** | Fully isolated environments (finance / government / defence) | High | High (re-import every upgrade) |

Every GitHub Release (`tools/v*` tag) ships assets for all three paths; pick whichever works.

---

## Path A: Docker pull from ghcr.io

```bash
# Pull latest stable
docker pull ghcr.io/vencil/da-tools:v2.7.0

# Run a one-shot command
docker run --rm ghcr.io/vencil/da-tools:v2.7.0 --help
docker run --rm ghcr.io/vencil/da-tools:v2.7.0 guard --help

# Mount conf.d in to run guard
docker run --rm \
    -v "$(pwd)/conf.d:/conf.d:ro" \
    ghcr.io/vencil/da-tools:v2.7.0 \
    guard defaults-impact --config-dir /conf.d --required-fields cpu,memory
```

**Contents**: Python `da-tools` CLI + bundled `da-guard` Linux/amd64 binary at `/usr/local/bin/da-guard`. The `da-tools guard` subcommand auto-locates the bundled `da-guard` inside the image — no need to set `$DA_GUARD_BINARY`.

**Trivy CVE scan** runs automatically at release time (`CRITICAL` / `HIGH` severities fail-fast). The image SBOM + signatures are listed in the `tools/v2.8.0` Release notes (cosign signing deferred to PR-3).

---

## Path B: Static binary download

Each Release ships 6 cross-compiled `da-guard` binaries:

| OS | ARCH | Filename |
|---|---|---|
| Linux | amd64 | `da-guard-linux-amd64.tar.gz` |
| Linux | arm64 | `da-guard-linux-arm64.tar.gz` |
| macOS | amd64 | `da-guard-darwin-amd64.tar.gz` |
| macOS | arm64 (Apple Silicon) | `da-guard-darwin-arm64.tar.gz` |
| Windows | amd64 | `da-guard-windows-amd64.zip` |
| Windows | arm64 | `da-guard-windows-arm64.zip` |

Each archive contains **one** `da-guard` (or `da-guard.exe`) binary, plus a single `SHA256SUMS` file listing hashes for all six archives.

### Install (Linux/macOS)

```bash
# Download + verify hash + extract + place on PATH
TAG=tools/v2.7.0
OS=linux            # or darwin, windows
ARCH=amd64          # or arm64
URL=https://github.com/vencil/Dynamic-Alerting-Integrations/releases/download/${TAG}

curl -fsSLo da-guard.tar.gz "${URL}/da-guard-${OS}-${ARCH}.tar.gz"
curl -fsSLo SHA256SUMS "${URL}/SHA256SUMS"

# Verify hash (any mismatch aborts)
sha256sum --check --ignore-missing SHA256SUMS

tar xzf da-guard.tar.gz
sudo install -m 0755 da-guard-${OS}-${ARCH} /usr/local/bin/da-guard
da-guard --version    # should print da-guard v2.7.0
```

### Install (Windows)

```powershell
$TAG = "tools/v2.7.0"
$Url = "https://github.com/vencil/Dynamic-Alerting-Integrations/releases/download/$TAG"

Invoke-WebRequest -Uri "$Url/da-guard-windows-amd64.zip" -OutFile da-guard.zip
Invoke-WebRequest -Uri "$Url/SHA256SUMS" -OutFile SHA256SUMS

# Verify hash
Get-FileHash da-guard.zip -Algorithm SHA256
# Compare against the matching line in SHA256SUMS

Expand-Archive -Path da-guard.zip -DestinationPath .
# Move to a directory on PATH (e.g. C:\Tools\)
```

### Run da-guard against conf.d/

```bash
da-guard --config-dir conf.d/ \
    --required-fields cpu,memory \
    --cardinality-limit 500 \
    --format md
```

Exit codes: `0` clean / `1` error-tier finding (block CI) / `2` caller error. The full flag reference lives in `components/threshold-exporter/README.md` § da-guard CLI (outside the MkDocs site — open from GitHub).

---

## Path C: Air-gapped tar import

For environments that cannot pull from `ghcr.io` at all (isolated internal registry / no-internet builds).

### One-time import flow

```bash
TAG=tools/v2.7.0
VER=2.7.0
URL=https://github.com/vencil/Dynamic-Alerting-Integrations/releases/download/${TAG}

# 1. Download image tar + SHA256
curl -fsSLo da-tools-image.tar.gz "${URL}/da-tools-image-v${VER}.tar.gz"
curl -fsSLo da-tools-image.tar.gz.sha256 "${URL}/da-tools-image-v${VER}.tar.gz.sha256"

# 2. Verify hash
sha256sum --check da-tools-image.tar.gz.sha256

# 3. Move the entire tar.gz into the air-gapped environment
#    (USB / internal file transfer / etc.)

# 4. Inside the air-gapped environment, load into local Docker
gunzip -c da-tools-image.tar.gz | docker load
# Prints: Loaded image: ghcr.io/vencil/da-tools:v2.7.0

# 5. Re-tag to your internal registry (optional)
docker tag ghcr.io/vencil/da-tools:v2.7.0 internal-registry.corp/da-tools:v2.7.0
docker push internal-registry.corp/da-tools:v2.7.0
```

After that, internal CI / pre-commit hooks use `internal-registry.corp/da-tools:v2.7.0` directly. The `da-guard` binary is bundled at `/usr/local/bin/da-guard` inside the image — no separate transfer needed.

### Pure binary also works in air-gapped

If the customer doesn't use Docker, walk Path B: download the 6 binary archives + `SHA256SUMS`, take them in via USB, extract. Each binary is statically linked; no runtime deps.

---

## Hash verification across all paths

Each Release ships:

| Asset | Contents |
|---|---|
| `SHA256SUMS` | Hashes for all 6 binary archives + 6 raw binaries (used by Paths B / C) |
| `da-tools-image-v<X.Y.Z>.tar.gz.sha256` | Hash of the air-gapped image tar (used by Path C) |

From `tools/v2.8.0` onward every artefact carries SHA-256. GPG / cosign signing is C-11 PR-3 work (DEC-J pending — gated on customer security team requiring signature verification).

## Upgrades

| Path | Upgrade command |
|---|---|
| A (ghcr.io) | `docker pull ghcr.io/vencil/da-tools:v<NEW>` |
| B (binary) | Re-walk "download + verify hash + replace `/usr/local/bin/da-guard`" |
| C (air-gapped) | Repeat the import flow (every release requires a fresh import) |

For major-version upgrades (e.g. `tools/v2.x → tools/v3.x`), read the Breaking changes section in the corresponding Release notes first. From `tools/v2.8.0` onward, [Release notes templates](https://github.com/vencil/Dynamic-Alerting-Integrations/releases/tag/tools/v2.8.0) are auto-generated (with commit-log links); a maintainer reviews and publishes manually.

## Verify da-guard works in your repo

First-time sanity check after install:

```bash
cd <customer-repo>/conf.d
da-guard --config-dir . --required-fields cpu --format md
```

Expected output: `✅ No findings — defaults change is safe to merge.` (or `❌ N errors found, M warnings`).

For CI integration, see [`.github/workflows/guard-defaults-impact.yml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.github/workflows/guard-defaults-impact.yml) — the C-12 PR-5 customer template, copy-paste ready.

## Troubleshooting

| Symptom | Possible cause | Resolution |
|---|---|---|
| `da-guard: command not found` | Binary not on `$PATH` | Confirm via `which da-guard`; place in `/usr/local/bin/` or any directory on PATH |
| `SHA256SUMS` mismatch | Truncated download / MITM | Re-download; verify network safety; fall back to a known-good earlier version |
| Docker pull 401 / 403 | ghcr.io rejects anonymous (private image / rate limit) | Wait 1 hour for rate-limit reset; or `docker login ghcr.io` for auth |
| `gunzip: invalid magic` | tar.gz corrupted or downloaded as an HTML error page | `file <path>` should report gzip data; re-download |
| `docker load` succeeds but `image not found` | Tag mismatch | `docker images | grep da-tools` for the actual tag; or run by image ID with `docker run <id>` |

For further issues, open a [GitHub issue](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/new) with `da-guard --version`, the observed exit code, and a reduced reproducer.

## Related documents

- [`migration-guide.en.md`](migration-guide.en.md) — Overall migration flow
- [`scenarios/incremental-migration-playbook.en.md`](scenarios/incremental-migration-playbook.en.md) — Incremental migration playbook (incl. Emergency Rollback Procedures)
- [`adr/019-profile-as-directory-default.en.md`](adr/019-profile-as-directory-default.en.md) — Why conf.d/ uses sparse-override shape
- [`cli-reference.en.md` § guard](cli-reference.en.md#guard) — Full `da-tools guard defaults-impact` flag reference
