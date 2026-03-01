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
#       ├── check_alert.py
#       ├── baseline_discovery.py
#       ├── validate_migration.py
#       ├── migrate_rule.py
#       ├── scaffold_tenant.py
#       ├── offboard_tenant.py
#       ├── deprecate_rule.py
#       ├── lint_custom_rules.py
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
    check_alert.py
    baseline_discovery.py
    validate_migration.py
    migrate_rule.py
    scaffold_tenant.py
    offboard_tenant.py
    deprecate_rule.py
    lint_custom_rules.py
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
