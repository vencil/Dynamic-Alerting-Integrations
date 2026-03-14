#!/usr/bin/env bash
# vendor_download.sh — Download CDN dependencies for offline/air-gapped usage
# Usage: ./scripts/tools/vendor_download.sh [--check]
set -euo pipefail

VENDOR_DIR="docs/assets/vendor"
CHECK_ONLY=false

if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=true
fi

# CDN resources used by jsx-loader.html and interactive/index.html
declare -A RESOURCES=(
  ["react.production.min.js"]="https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js"
  ["react-dom.production.min.js"]="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js"
  ["babel.min.js"]="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.23.9/babel.min.js"
  ["lucide-react.min.js"]="https://unpkg.com/lucide-react@0.383.0/dist/umd/lucide-react.min.js"
  ["tailwindcss.js"]="https://cdn.tailwindcss.com"
)

if $CHECK_ONLY; then
  echo "Checking vendor files in $VENDOR_DIR ..."
  missing=0
  for file in "${!RESOURCES[@]}"; do
    if [[ -f "$VENDOR_DIR/$file" ]]; then
      size=$(wc -c < "$VENDOR_DIR/$file")
      echo "  ✅ $file (${size} bytes)"
    else
      echo "  ❌ $file — MISSING"
      missing=$((missing + 1))
    fi
  done
  if [[ $missing -gt 0 ]]; then
    echo ""
    echo "$missing file(s) missing. Run: make vendor-download"
    exit 1
  else
    echo ""
    echo "All vendor files present."
    exit 0
  fi
fi

echo "Downloading vendor files to $VENDOR_DIR ..."
mkdir -p "$VENDOR_DIR"

for file in "${!RESOURCES[@]}"; do
  url="${RESOURCES[$file]}"
  echo "  ↓ $file"
  curl -sL "$url" -o "$VENDOR_DIR/$file"
  size=$(wc -c < "$VENDOR_DIR/$file")
  echo "    ✅ ${size} bytes"
done

# Create .gitignore so vendor files are not committed (they're large)
cat > "$VENDOR_DIR/.gitignore" << 'GITIGNORE'
# Vendor files are downloaded on demand — do not commit
*.js
!.gitignore
GITIGNORE

echo ""
echo "Done! $VENDOR_DIR/ is ready for offline use."
echo "jsx-loader.html will automatically detect local vendor files."
