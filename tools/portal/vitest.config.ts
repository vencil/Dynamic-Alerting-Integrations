import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

/**
 * Vitest config for portal unit tests — TECH-DEBT-030 Option C foundation.
 *
 * Tests live in `tests/portal/*.test.{ts,tsx}` (sibling directory).
 * Component sources under `docs/interactive/tools/` use ESM imports.
 *
 * Frontmatter strip plugin replicates jsx-loader.html behavior so test
 * code can `import { X } from '<...>.jsx'` regardless of YAML preamble.
 */

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..', '..');

function stripFrontmatter() {
  return {
    name: 'strip-yaml-frontmatter',
    enforce: 'pre' as const,
    transform(code: string, id: string) {
      if (!/\.(jsx?|tsx?)$/.test(id)) return null;
      const match = code.match(/^---\r?\n[\s\S]*?\r?\n---\s*(?:\r?\n|$)/);
      if (!match) return null;
      const newlines = '\n'.repeat(match[0].split('\n').length - 1);
      return { code: newlines + code.slice(match[0].length), map: null };
    },
  };
}

export default defineConfig({
  root: __dirname,
  plugins: [stripFrontmatter(), react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./test-setup.ts'],
    include: [resolve(REPO_ROOT, 'tests/portal/**/*.test.{ts,tsx,js,jsx}')],
    server: {
      deps: {
        // .jsx/.js files outside node_modules need Vite's transform pipeline.
        inline: [/\/docs\/interactive\/.*\.(jsx?|tsx?)$/],
      },
    },
  },
  resolve: {
    alias: {
      '@portal-tools': resolve(REPO_ROOT, 'docs/interactive/tools'),
    },
  },
});
