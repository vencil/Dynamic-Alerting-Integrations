/**
 * Unit + property tests for cicd-setup-wizard generators — TRK-232b (#TBD).
 *
 * The four generators (`cicdGenerateInitCommand` / `Docker` /
 * `FileTree` / `GitHubActionsPreview`) are pure functions with no
 * closure deps — they take a config object and return a string.
 * Despite zero side-effects and no global reads, they had no unit
 * coverage prior to this PR; covered only via E2E spec eyeballing.
 *
 * Property: `cicdGenerateInitCommand` always returns a string that
 * starts with `da-tools init` and contains the joined CSV for any
 * tenants / packs in the config.
 */
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import {
  cicdGenerateInitCommand,
  cicdGenerateDockerCommand,
  cicdGeneratedPaths,
  cicdGenerateFileTree,
  cicdGenerateGitHubActionsPreview,
} from '../src/interactive/tools/cicd-setup-wizard/utils/generators.js';

const baseConfig = (overrides: Record<string, unknown> = {}) => ({
  ci: 'github',
  deploy: 'kustomize',
  tenants: ['db-a'],
  packs: ['mariadb-core'],
  ...overrides,
});

describe('cicdGenerateInitCommand', () => {
  it('always starts with "da-tools init"', () => {
    expect(cicdGenerateInitCommand(baseConfig())).toMatch(/^da-tools init/);
  });

  it('includes --ci, --deploy, --tenants, --rule-packs flags', () => {
    const out = cicdGenerateInitCommand(baseConfig());
    expect(out).toContain('--ci github');
    expect(out).toContain('--deploy kustomize');
    expect(out).toContain('--tenants db-a');
    expect(out).toContain('--rule-packs mariadb-core');
    expect(out).toContain('--non-interactive');
  });

  it('omits --tenants flag when tenants array is empty', () => {
    expect(cicdGenerateInitCommand(baseConfig({ tenants: [] }))).not.toMatch(/--tenants/);
  });

  it('omits --rule-packs flag when packs array is empty', () => {
    expect(cicdGenerateInitCommand(baseConfig({ packs: [] }))).not.toMatch(/--rule-packs/);
  });

  it('joins multiple tenants with comma (no spaces)', () => {
    const out = cicdGenerateInitCommand(baseConfig({ tenants: ['db-a', 'db-b', 'db-c'] }));
    expect(out).toContain('--tenants db-a,db-b,db-c');
  });

  // Property: for any non-empty list of tenant ids matching the
  // canonical RFC-1123 subset, the output contains them joined CSV-style.
  it('property: tenants always serialize as comma-joined CSV', () => {
    fc.assert(
      fc.property(
        fc.array(fc.stringMatching(/^[a-z][a-z0-9-]{0,20}$/), {
          minLength: 1,
          maxLength: 5,
        }),
        (tenants) => {
          const out = cicdGenerateInitCommand(baseConfig({ tenants }));
          return out.includes(`--tenants ${tenants.join(',')}`);
        },
      ),
      { numRuns: 50 },
    );
  });
});

describe('cicdGenerateDockerCommand', () => {
  it('wraps the init command in docker run', () => {
    const out = cicdGenerateDockerCommand(baseConfig());
    expect(out).toMatch(/^docker run/);
    expect(out).toContain('ghcr.io/vencil/da-tools:latest');
    expect(out).toContain('init');
  });

  it('strips the "da-tools " prefix when nesting (avoid double command)', () => {
    const out = cicdGenerateDockerCommand(baseConfig());
    // "da-tools init" appears only ONCE in init form; inside docker,
    // it's just "init --ci github ...".
    expect(out.match(/da-tools init/g) ?? []).toHaveLength(0);
  });
});

describe('cicdGenerateFileTree', () => {
  it('starts with "your-repo/" header', () => {
    expect(cicdGenerateFileTree(baseConfig())).toMatch(/^your-repo\//);
  });

  it('lists each tenant as a separate yaml file', () => {
    const out = cicdGenerateFileTree(baseConfig({ tenants: ['db-a', 'db-b'] }));
    expect(out).toContain('db-a.yaml');
    expect(out).toContain('db-b.yaml');
  });

  it('emits the github workflow when ci=github', () => {
    expect(cicdGeneratedPaths(baseConfig({ ci: 'github' })))
      .toContain('.github/workflows/dynamic-alerting.yaml');
  });

  it('emits the gitlab pipeline when ci=gitlab', () => {
    expect(cicdGeneratedPaths(baseConfig({ ci: 'gitlab' })))
      .toContain('.gitlab-ci.d/dynamic-alerting.yml');
  });

  it('emits BOTH github + gitlab when ci=both', () => {
    const out = cicdGeneratedPaths(baseConfig({ ci: 'both' }));
    expect(out).toContain('.github/workflows/dynamic-alerting.yaml');
    expect(out).toContain('.gitlab-ci.d/dynamic-alerting.yml');
  });

  // ⛔ These two replace assertions that pinned the OPPOSITE — "emits
  // kustomize/ when deploy=kustomize or argocd" and "emits argocd/ ONLY when
  // deploy=argocd". Both were green and both were wrong: `da-tools init`
  // scaffolds deployment files for deploy=kustomize only, so the wizard was
  // promising two directories that never arrive, and this suite was what kept
  // the promise load-bearing. Set equality (not toContain) so an added path
  // also has to be justified against the CLI. Cross-checked against the real
  // run_init() by tests/ops/test_generated_ci_artifacts.py.
  it('scaffolds deployment files for deploy=kustomize only', () => {
    expect(cicdGeneratedPaths(baseConfig({ deploy: 'kustomize' })).filter(p => p.startsWith('kustomize/')))
      .toEqual([
        'kustomize/base/kustomization.yaml',
        'kustomize/base/README.md',
        'kustomize/overlays/dev/kustomization.yaml',
        'kustomize/overlays/prod/kustomization.yaml',
      ]);
  });

  it.each(['helm', 'argocd'])('scaffolds NO deployment files for deploy=%s', (deploy) => {
    const out = cicdGeneratedPaths(baseConfig({ deploy }));
    expect(out.filter(p => p.startsWith('kustomize/') || p.startsWith('argocd/'))).toEqual([]);
    expect(cicdGenerateFileTree(baseConfig({ deploy }))).not.toContain('argocd/');
  });

  it('draws a well-formed tree for multiple tenants', () => {
    // The hand-drawn version gave EVERY tenant the terminal `└──` connector,
    // so from the second tenant on the tree was malformed.
    const lines = cicdGenerateFileTree(baseConfig({ tenants: ['db-a', 'db-b'] })).split('\n');
    const lineFor = (name: string) => lines.find(l => l.trim().endsWith(name));
    expect(lineFor('_defaults.yaml')).toBe('│   ├── _defaults.yaml');
    expect(lineFor('db-a.yaml')).toBe('│   ├── db-a.yaml');
    expect(lineFor('db-b.yaml')).toBe('│   └── db-b.yaml');
  });
});

describe('cicdGenerateGitHubActionsPreview', () => {
  it('returns a string starting with "name: Dynamic Alerting CI/CD"', () => {
    const out = cicdGenerateGitHubActionsPreview(baseConfig());
    expect(out).toMatch(/^name: Dynamic Alerting CI\/CD/);
  });

  it('declares pull_request and push triggers on conf.d/**', () => {
    const out = cicdGenerateGitHubActionsPreview(baseConfig());
    expect(out).toContain('pull_request:');
    expect(out).toContain('push:');
    expect(out).toContain("paths: ['conf.d/**']");
  });

  it('declares a validate job on ubuntu-latest', () => {
    const out = cicdGenerateGitHubActionsPreview(baseConfig());
    expect(out).toContain('validate:');
    expect(out).toContain('runs-on: ubuntu-latest');
  });
});
