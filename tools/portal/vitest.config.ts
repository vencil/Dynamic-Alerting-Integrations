import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

/**
 * Vitest config for portal unit tests — TRK-242 monorepo restructure.
 *
 * After the restructure, source / tests / config all live under
 * `tools/portal/`. The path-alias maze that bridged the old 3-way
 * split (config in tools/portal/, source in docs/interactive/, tests
 * in tests/portal/) is gone.
 *
 * Layout:
 *   tools/portal/src/           — JSX source
 *   tools/portal/tests/         — Vitest specs
 *   tools/portal/node_modules/  — npm deps
 *
 * Frontmatter strip plugin replicates jsx-loader.html behavior so
 * `import { X } from '<...>.jsx'` works regardless of YAML preamble.
 */

const __dirname = dirname(fileURLToPath(import.meta.url));

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
    include: ['tests/**/*.test.{ts,tsx,js,jsx}'],
  },
});
