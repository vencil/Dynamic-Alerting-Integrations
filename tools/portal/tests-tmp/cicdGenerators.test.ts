/**
 * Unit + property tests for cicd-setup-wizard generators — TD-032b (#TBD).
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
  cicdGenerateFileTree,
  cicdGenerateGitHubActionsPreview,
} from '../../docs/interactive/tools/cicd-setup-wizard/utils/generators.js';

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

  it('emits .github/workflows/ when ci=github', () => {
    expect(cicdGenerateFileTree(baseConfig({ ci: 'github' }))).toContain('.github/workflows/');
  });

  it('emits .gitlab-ci.d/ when ci=gitlab', () => {
    expect(cicdGenerateFileTree(baseConfig({ ci: 'gitlab' }))).toContain('.gitlab-ci.d/');
  });

  it('emits BOTH github + gitlab when ci=both', () => {
    const out = cicdGenerateFileTree(baseConfig({ ci: 'both' }));
    expect(out).toContain('.github/workflows/');
    expect(out).toContain('.gitlab-ci.d/');
  });

  it('emits kustomize/ when deploy=kustomize or argocd', () => {
    expect(cicdGenerateFileTree(baseConfig({ deploy: 'kustomize' }))).toContain('kustomize/');
    expect(cicdGenerateFileTree(baseConfig({ deploy: 'argocd' }))).toContain('kustomize/');
  });

  it('emits argocd/ ONLY when deploy=argocd', () => {
    expect(cicdGenerateFileTree(baseConfig({ deploy: 'argocd' }))).toContain('argocd/');
    expect(cicdGenerateFileTree(baseConfig({ deploy: 'kustomize' }))).not.toContain('argocd/');
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
