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
    # Config generation tools
    ops/generate_alertmanager_routes.py
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

echo "✓ Built: $IMAGE_NAME"
echo ""
echo "Quick test:"
echo "  docker run --rm $IMAGE_NAME --version"
echo "  docker run --rm -e PROMETHEUS_URL=http://host.docker.internal:9090 $IMAGE_NAME check-alert MariaDBHighConnections db-a"
