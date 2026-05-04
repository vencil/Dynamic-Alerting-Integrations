#!/usr/bin/env bash
# ── da-tools Container Build Script ─────────────────────────────────
#
# Assembles the Docker build context by copying tool scripts from
# scripts/tools/ into a temporary directory, then builds the image.
#
# Usage:
#   ./build.sh              # Build with default tag (da-tools:dev)
#   ./build.sh v0.1.0       # Build with specific version tag
#
# The build context layout:
#   app/
#   ├── Dockerfile
#   ├── entrypoint.py
#   ├── VERSION
#   └── tools/              ← copied from scripts/tools/
#       ├── _lib_python.py           (shared library)
#       ├── check_alert.py
#       ├── diagnose.py
#       ├── batch_diagnose.py
#       ├── baseline_discovery.py
#       ├── validate_migration.py
#       ├── backtest_threshold.py
#       ├── migrate_rule.py
#       ├── scaffold_tenant.py
#       ├── onboard_platform.py
#       ├── offboard_tenant.py
#       ├── deprecate_rule.py
#       ├── cutover_tenant.py
#       ├── blind_spot_discovery.py
#       ├── maintenance_scheduler.py
#       ├── lint_custom_rules.py
#       ├── config_diff.py
#       ├── generate_alertmanager_routes.py
#       ├── validate_config.py
#       ├── analyze_rule_pack_gaps.py
#       ├── patch_config.py
#       ├── alert_quality.py
#       ├── policy_engine.py
#       ├── cardinality_forecasting.py
#       └── metric-dictionary.yaml
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TOOLS_SRC="$PROJECT_ROOT/scripts/tools"

ASSEMBLE_ONLY=false
TAG="dev"
for arg in "$@"; do
    case "$arg" in
        --assemble-only) ASSEMBLE_ONLY=true ;;
        *) TAG="$arg" ;;
    esac
done
IMAGE_NAME="da-tools:$TAG"

# ── Assemble build context ──────────────────────────────────────────
echo "▸ Assembling build context..."
rm -rf "$SCRIPT_DIR/tools"
mkdir -p "$SCRIPT_DIR/tools"

# Copy only the tools we package (not test scripts or other artifacts)
TOOL_FILES=(
    # Shared library (imported by multiple tools)
    _lib_python.py
    # Prometheus API tools (portable)
    ops/check_alert.py
    ops/diagnose.py
    ops/batch_diagnose.py
    ops/baseline_discovery.py
    ops/validate_migration.py
    ops/backtest_threshold.py
    ops/cutover_tenant.py
    ops/blind_spot_discovery.py
    ops/maintenance_scheduler.py
    ops/alert_quality.py
    ops/alert_correlate.py
    # Config generation tools
    ops/generate_alertmanager_routes.py
    # v2.8.0 PR-3a — generate_alertmanager_routes split into 5 helpers
    # (validate / merge / parse / routes / render). All re-exported from
    # the main file so existing entrypoint.py / test imports work
    # unchanged. See scripts/tools/ops/_grar_*.py module docstrings.
    ops/_grar_validate.py
    ops/_grar_merge.py
    ops/_grar_parse.py
    ops/_grar_routes.py
    ops/_grar_render.py
    ops/explain_route.py
    ops/validate_config.py
    ops/analyze_rule_pack_gaps.py
    ops/patch_config.py
    # File system tools (offline)
    ops/migrate_rule.py
    ops/config_diff.py
    ops/scaffold_tenant.py
    ops/onboard_platform.py
    ops/offboard_tenant.py
    ops/deprecate_rule.py
    ops/lint_custom_rules.py
    # v2.0.0 quality & governance tools
    ops/policy_engine.py
    ops/policy_opa_bridge.py
    ops/cardinality_forecasting.py
    ops/notification_tester.py
    ops/threshold_recommend.py
    ops/byo_check.py
    ops/federation_check.py
    ops/grafana_import.py
    ops/shadow_verify.py
    ops/discover_instance_mappings.py
    ops/generate_tenant_mapping_rules.py
    ops/init_project.py
    ops/config_history.py
    ops/gitops_check.py
    ops/drift_detect.py
    # v2.3.0 Operator-native tools
    ops/operator_generate.py
    ops/operator_check.py
    # v2.6.0 Migration tools
    ops/migrate_to_operator.py
    # v2.3.0 Federation tools
    ops/generate_rule_pack_split.py
    # v2.8.0 Phase .c C-12 — Dangling Defaults Guard dispatcher
    # (shells out to the `da-guard` Go binary; see scripts/tools/ops/guard_dispatch.py)
    ops/guard_dispatch.py
    # v2.8.0 Phase .c C-10 PR-5 — Batch PR Pipeline dispatcher
    # (shells out to the `da-batchpr` Go binary; see scripts/tools/ops/batchpr_dispatch.py)
    ops/batchpr_dispatch.py
    # v2.8.0 Phase .c C-8 PR-2 — PromRule parser dispatcher
    # (shells out to the `da-parser` Go binary; see scripts/tools/ops/parser_dispatch.py)
    ops/parser_dispatch.py
    # v2.8.0 Phase .b Track A A5 — tenant verify (B-4 rollback checklist)
    # tenant_verify imports ConfDScanner from describe_tenant (transitive
    # dep); both ship together. See scripts/tools/dx/tenant_verify.py
    # docstring + planning Track A A5.
    #
    # IMPORTANT: `describe_tenant.py` is intentionally **shipped but NOT
    # registered** in entrypoint.py COMMAND_MAP. Customers can still
    # invoke it via `python /app/describe_tenant.py ...` inside the
    # image, but `da-tools describe-tenant ...` is not supported. This
    # is the chosen design tradeoff — describe_tenant is a v2.7.0
    # internal tool that we don't yet want to commit as a stable
    # public CLI surface (its arg shape may change). Promotion to
    # public command happens via a future entrypoint.py registration
    # PR; until then it's a transitive lib for tenant-verify only.
    dx/describe_tenant.py
    dx/tenant_verify.py
    # Shared library sub-modules
    _lib_constants.py
    _lib_validation.py
    _lib_prometheus.py
    _lib_io.py
    # v2.8.0 PR-2 — shared dispatcher for the three Go-binary
    # subcommands (guard / batchpr / parser). Imported by their
    # respective ops/*_dispatch.py shims.
    _lib_godispatch.py
    # Data files
    metric-dictionary.yaml
)

for f in "${TOOL_FILES[@]}"; do
    if [ ! -f "$TOOLS_SRC/$f" ]; then
        echo "✗ Missing: $TOOLS_SRC/$f" >&2
        exit 1
    fi
    cp "$TOOLS_SRC/$f" "$SCRIPT_DIR/tools/"
done

echo "  Copied ${#TOOL_FILES[@]} files from scripts/tools/"

# ── Strip repo-layout sys.path hack ──────────────────────────────────
# In the repo, tools use dual sys.path (current dir + parent dir) to
# support both flat Docker layout and subdir repo layout.  In Docker
# everything is flat, so remove the parent-dir line to keep images clean.
for py in "$SCRIPT_DIR"/tools/*.py; do
    [ -f "$py" ] || continue
    sed -i "/sys\.path\.insert.*os\.path\.join.*_THIS_DIR.*'\.\.')/d" "$py"
done
echo "  Stripped repo-layout sys.path from Docker copies"

# ── Build da-guard binary (v2.8.0 C-11) ──────────────────────────────
# da-tools' `guard` subcommand shells out to the da-guard Go binary
# (see scripts/tools/ops/guard_dispatch.py). Bundle the linux/amd64
# binary into the image so customers running `da-tools guard ...` in
# the container don't need to install or download it separately.
#
# Local devs without Go on PATH get a clear "Go not found" error
# (rather than a cryptic Dockerfile COPY failure later). Production
# CI has Go via actions/setup-go in release.yaml.
EXPORTER_APP="$PROJECT_ROOT/components/threshold-exporter/app"
echo "▸ Building da-guard binary for da-tools image bundling..."
if [ ! -d "$EXPORTER_APP" ]; then
    echo "  ✗ threshold-exporter source not found at $EXPORTER_APP" >&2
    exit 1
fi
if ! command -v go >/dev/null 2>&1; then
    echo "  ✗ go not on PATH; install Go 1.26+ to bundle da-guard, or use the prebuilt binary from a tools/v* release" >&2
    exit 1
fi
DA_TOOLS_VERSION=$(cat "$SCRIPT_DIR/VERSION" | tr -d '[:space:]')
(cd "$EXPORTER_APP" && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
        -buildvcs=false \
        -ldflags "-X main.Version=v${DA_TOOLS_VERSION}" \
        -o "$SCRIPT_DIR/da-guard" \
        ./cmd/da-guard)
echo "  Built da-guard linux/amd64 (DA_TOOLS_VERSION=v${DA_TOOLS_VERSION})"

# ── Build da-batchpr binary (v2.8.0 C-10 PR-5) ──────────────────────
# da-tools' `batch-pr` subcommand shells out to the da-batchpr Go
# binary (subcommands: apply / refresh / refresh-source). Bundle the
# linux/amd64 binary so customers running `da-tools batch-pr ...` in
# the container don't need to install or download it separately.
echo "▸ Building da-batchpr binary for da-tools image bundling..."
(cd "$EXPORTER_APP" && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
        -buildvcs=false \
        -ldflags "-X main.Version=v${DA_TOOLS_VERSION}" \
        -o "$SCRIPT_DIR/da-batchpr" \
        ./cmd/da-batchpr)
echo "  Built da-batchpr linux/amd64 (DA_TOOLS_VERSION=v${DA_TOOLS_VERSION})"

# ── Build da-parser binary (v2.8.0 C-8 PR-2) ────────────────────────
# da-tools' `parser` subcommand shells out to the da-parser Go binary
# (subcommands: import / allowlist). Bundle the linux/amd64 binary so
# customers running `da-tools parser import ...` in the container
# don't need to install or download it separately.
echo "▸ Building da-parser binary for da-tools image bundling..."
(cd "$EXPORTER_APP" && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
        -buildvcs=false \
        -ldflags "-X main.Version=v${DA_TOOLS_VERSION}" \
        -o "$SCRIPT_DIR/da-parser" \
        ./cmd/da-parser)
echo "  Built da-parser linux/amd64 (DA_TOOLS_VERSION=v${DA_TOOLS_VERSION})"

# ── Assemble-only mode (for CI — Buildx handles the docker build) ──
if [ "$ASSEMBLE_ONLY" = true ]; then
    echo "✓ Build context assembled (--assemble-only). tools/ kept for Buildx."
    exit 0
fi

# ── Build ───────────────────────────────────────────────────────────
echo "▸ Building $IMAGE_NAME..."
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

# ── Cleanup temporary copies ────────────────────────────────────────
rm -rf "$SCRIPT_DIR/tools"
rm -f "$SCRIPT_DIR/da-guard" "$SCRIPT_DIR/da-batchpr" "$SCRIPT_DIR/da-parser"

echo "✓ Built: $IMAGE_NAME"
echo ""
echo "Quick test:"
echo "  docker run --rm $IMAGE_NAME --version"
echo "  docker run --rm -e PROMETHEUS_URL=http://host.docker.internal:9090 $IMAGE_NAME check-alert MariaDBHighConnections db-a"
