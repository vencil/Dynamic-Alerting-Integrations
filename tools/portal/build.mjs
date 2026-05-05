#!/usr/bin/env node
/**
 * Portal ESM build — TECH-DEBT-030 Option C foundation.
 *
 * Reads `manifest.json` for the list of tool entries to bundle, then
 * for each entry:
 *   - reads `docs/interactive/tools/<entry>.jsx`
 *   - strips YAML frontmatter (jsx-loader did this; we replicate)
 *   - bundles via esbuild with React JSX transform
 *   - writes `docs/assets/dist/<entry>.js`
 *
 * Per-tool subtrees (e.g. tenant-manager/components/*.jsx) are pulled
 * in by esbuild's resolver via standard `import` statements in the
 * entry — no special handling needed once entries use ESM.
 *
 * Why a custom build script instead of vite/parcel:
 *   - Zero dev-server complexity (we already serve via python -m http.server)
 *   - Deterministic output paths (CI reproducibility)
 *   - Frontmatter-stripping plugin is short and codebase-specific
 *   - One file end-to-end; reviewer can fully grok in 5 minutes
 *
 * Usage:
 *   node build.mjs              # one-shot build (CI)
 *   node build.mjs --watch      # watch mode (dev)
 */

import * as esbuild from 'esbuild';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..', '..');

const ENTRIES_DIR = resolve(__dirname, 'entries');
const DIST_DIR = resolve(REPO_ROOT, 'docs', 'assets', 'dist');

/**
 * esbuild plugin that strips YAML frontmatter at the top of `.jsx`/`.js`
 * files. The original jsx-loader.html does this in browser; we replicate
 * exactly the same regex (closing `---` anchored to its own line) so a
 * file that loaded under jsx-loader still loads under esbuild.
 */
function stripFrontmatterPlugin() {
  return {
    name: 'strip-yaml-frontmatter',
    setup(build) {
      build.onLoad({ filter: /\.(jsx|js|ts|tsx)$/ }, async (args) => {
        const source = await readFile(args.path, 'utf8');
        const stripped = source.replace(
          /^---\r?\n[\s\S]*?\r?\n---\s*(?:\r?\n|$)/,
          (match) => '\n'.repeat(match.split('\n').length - 1),
        );
        return {
          contents: stripped,
          loader: args.path.endsWith('.jsx')
            ? 'jsx'
            : args.path.endsWith('.tsx')
              ? 'tsx'
              : args.path.endsWith('.ts')
                ? 'ts'
                : 'js',
        };
      });
    },
  };
}

async function loadManifest() {
  const path = resolve(__dirname, 'manifest.json');
  const raw = await readFile(path, 'utf8');
  const data = JSON.parse(raw);
  if (!Array.isArray(data.entries)) {
    throw new Error(`manifest.json must contain { "entries": [...] }`);
  }
  return data.entries;
}

async function main() {
  const watch = process.argv.includes('--watch');
  const entries = await loadManifest();

  if (entries.length === 0) {
    console.log('[portal-build] manifest empty — no entries to build.');
    console.log('[portal-build] Add tool entries to tools/portal/manifest.json as TD-030b onward migrate them.');
    return;
  }

  await mkdir(DIST_DIR, { recursive: true });

  const config = {
    entryPoints: Object.fromEntries(
      entries.map((name) => [name, resolve(ENTRIES_DIR, `${name}.entry.jsx`)]),
    ),
    bundle: true,
    format: 'esm',
    target: 'es2022',
    outdir: DIST_DIR,
    splitting: true,
    platform: 'browser',
    sourcemap: 'linked',
    jsx: 'automatic',
    jsxImportSource: 'react',
    // React + ReactDOM are bundled into each tool's dist (~140KB) so
    // browser pages can `<script type="module" src="dist/X.js">` without
    // an import map or CDN dependency. Per-tool React instance is fine
    // because each tool is its own page (no cross-tool sharing).
    plugins: [stripFrontmatterPlugin()],
    // Source files under docs/interactive/ have no sibling node_modules;
    // their ancestor walk doesn't reach tools/portal/node_modules either.
    // Explicit nodePaths makes esbuild resolve `react` / `react-dom/client`
    // / etc. from the build harness's deps regardless of where the
    // importing file lives in the repo tree.
    nodePaths: [resolve(__dirname, 'node_modules')],
    // Production-mode React: NODE_ENV=production strips DevTools hooks
    // and dev-only warnings, trimming the bundle by ~85% (1.1MB → ~150KB).
    // Watch mode (--watch) runs unminified for clearer stack traces.
    define: { 'process.env.NODE_ENV': '"production"' },
    minify: !watch,
    logLevel: 'info',
  };

  console.log(`[portal-build] Bundling ${entries.length} entries:`);
  for (const e of entries) {
    console.log(`  - entries/${e}.entry.jsx → dist/${e}.js`);
  }

  if (watch) {
    const ctx = await esbuild.context(config);
    await ctx.watch();
    console.log('[portal-build] watching for changes (Ctrl-C to stop)');
  } else {
    await esbuild.build(config);
    console.log('[portal-build] done.');
  }
}

main().catch((err) => {
  console.error('[portal-build] FAILED:', err);
  process.exit(1);
});
