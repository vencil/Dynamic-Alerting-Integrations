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
  // root stays at tools/portal/ so node_modules resolution works.
  // server.fs.allow expands to repo root so tests/portal/ and
  // docs/interactive/ files outside root are still loadable.
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
  server: {
    fs: {
      // Allow Vite to read source files outside root: the test specs in
      // tests/portal/ and the components under docs/interactive/.
      allow: [REPO_ROOT],
    },
  },
  resolve: {
    alias: {
      '@portal-tools': resolve(REPO_ROOT, 'docs/interactive/tools'),
      // Test specs in tests/portal/ are outside Vitest root and have no
      // sibling node_modules. Force resolution to tools/portal/node_modules.
      'react': resolve(__dirname, 'node_modules/react'),
      'react/jsx-runtime': resolve(__dirname, 'node_modules/react/jsx-runtime.js'),
      'react-dom': resolve(__dirname, 'node_modules/react-dom'),
      'react-dom/client': resolve(__dirname, 'node_modules/react-dom/client.js'),
      '@testing-library/react': resolve(__dirname, 'node_modules/@testing-library/react'),
      '@testing-library/jest-dom': resolve(__dirname, 'node_modules/@testing-library/jest-dom'),
      '@testing-library/jest-dom/vitest': resolve(__dirname, 'node_modules/@testing-library/jest-dom/vitest.js'),
    },
  },
});
