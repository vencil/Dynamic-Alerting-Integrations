/**
 * What the portal build (build.mjs) and the JSX syntax lint
 * (scripts/tools/lint/lint_jsx_syntax.py) must read the same way — the one
 * definition of each (#3669). The lint parses each source file with exactly
 * the front-matter strip, loader and JSX settings the bundle uses, so "the
 * lint passes" and "the build parses it" cannot drift apart.
 */

/**
 * YAML front matter at the top of a source file. The closing `---` must sit
 * on its own line (PR-portal-6): a `------` separator inside a `purpose:`
 * block is not the end of it.
 */
export const FRONTMATTER_RE = /^---\r?\n[\s\S]*?\r?\n---\s*(?:\r?\n|$)/;

/**
 * *source* without its front matter, replaced by as many newlines as it
 * spanned, so the line numbers in an esbuild error stay the file's own.
 */
export function stripFrontmatter(source) {
  return source.replace(
    FRONTMATTER_RE,
    (match) => '\n'.repeat(match.split('\n').length - 1),
  );
}

/** esbuild's loader for *path*, by its extension. */
export function loaderFor(path) {
  return path.endsWith('.jsx')
    ? 'jsx'
    : path.endsWith('.tsx')
      ? 'tsx'
      : path.endsWith('.ts')
        ? 'ts'
        : 'js';
}

/** The JSX transform and output settings of the bundle. */
export const TRANSFORM_OPTIONS = Object.freeze({
  format: 'esm',
  target: 'es2022',
  jsx: 'automatic',
  jsxImportSource: 'react',
});
