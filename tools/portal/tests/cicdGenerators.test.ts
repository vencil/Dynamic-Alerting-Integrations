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

  // ⛔ Every other fixture in this file is `ci: 'github', deploy: 'kustomize',
  // packs: ['mariadb-core']`, so three separate mutations survived the whole
  // suite: hard-coding `--ci github`, hard-coding `--deploy kustomize`, and
  // joining packs with `;` instead of `,`. The values were never varied, so
  // "it interpolates the config" was never actually tested — only "it prints
  // these particular strings".
  it.each([
    ['gitlab', 'kustomize'],
    ['both', 'helm'],
  ])('interpolates ci=%s / deploy=%s rather than emitting a fixed value',
    (ci, deploy) => {
      const out = cicdGenerateInitCommand(baseConfig({ ci, deploy }));
      expect(out).toContain(`--ci ${ci}`);
      expect(out).toContain(`--deploy ${deploy}`);
    });

  it('comma-joins multiple rule packs, like it does for tenants', () => {
    const out = cicdGenerateInitCommand(
      baseConfig({ packs: ['mariadb-core', 'mysql-core', 'pg-core'] }));
    expect(out).toContain('--rule-packs mariadb-core,mysql-core,pg-core');
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

  it('passes --user, because the mount is writable and the image is not root', () => {
    // ⛔ #1495. This is the FIRST command a customer copies. The image ends
    // `USER nonroot:nonroot` (uid 10001) while the mounted directory is the
    // customer's own checkout (typically uid 1000), so `init` — which creates
    // conf.d/, the CI workflow and the deploy tree — cannot write and dies on
    // a bare `PermissionError` traceback having produced zero files. Measured
    // inside one Linux container: uid 10001 fails, uid 1000 succeeds.
    //
    // The equivalent Python guard (check_doc_datools_cmds) cannot reach this
    // string: it scans markdown, and this command is generated at runtime.
    const out = cicdGenerateDockerCommand(baseConfig());
    expect(out).toContain('--user $(id -u):$(id -g)');
  });

  it('quotes the bind mount, because a checkout path may contain spaces', () => {
    // The wizard emits a command the customer pastes into their own shell.
    // Unquoted, `-v $(pwd):/workspace` word-splits as soon as the checkout
    // lives under a path like `C:\Users\A B\repo` or `~/My Projects/repo`,
    // and docker rejects the fragment as an invalid volume spec. Quoting is
    // the form the shared template docs/includes/docker-usage-pattern{,.en}.md
    // already prescribes.
    // ⚠️ Pinned deliberately: the quotes are one character each and the whole
    // suite stayed green when they were missing, so nothing else guards them.
    const out = cicdGenerateDockerCommand(baseConfig());
    expect(out).toContain('-v "$(pwd):/workspace"');
    expect(out).not.toMatch(/-v \$\(pwd\)/);
  });

  it('sets the working directory to the mount point', () => {
    // ⛔ Same failure family as the missing --user, and `--user` cannot fix it.
    // Without `-w /workspace` the container keeps the image's own WORKDIR
    // (/opt/da-tools, root-owned), so `init` writes conf.d/ and the CI
    // workflow *inside the image* — the customer's mounted checkout stays
    // empty and the run still exits 0. Pinned because a mutation deleting
    // this flag survived both this suite and the Python ops suite.
    const out = cicdGenerateDockerCommand(baseConfig());
    expect(out).toContain('-w /workspace');
  });

  it('keeps --user ahead of the image reference', () => {
    // Docker only accepts flags BEFORE the image name; anything after it is
    // passed to the container as arguments. A `--user` that drifts below the
    // image would be silently handed to `da-tools init` instead — the command
    // would still look right in the wizard and still fail for the customer.
    const out = cicdGenerateDockerCommand(baseConfig());
    expect(out.indexOf('--user')).toBeGreaterThan(-1);
    expect(out.indexOf('--user')).toBeLessThan(out.indexOf('ghcr.io/vencil/da-tools'));
  });
});

describe('cicdGenerateGitHubActionsPreview — writable mounts', () => {
  it('runs the only container that writes into a mount as the runner', () => {
    // ⛔ #1495 again, in the OTHER generator of this module. `generate-routes
    // -o /data/output/routes.yaml` writes into the mounted `.output`, which
    // belongs to the runner uid; the image is uid 10001, so without --user the
    // very first PR fails with PermissionError and zero files. The CLI twin
    // (scripts/tools/ops/init_project.py, the "Generate Alertmanager routes"
    // step) has carried the flag all along — this is #1351's divergence
    // surfacing as the exact defect #1495 is about.
    //
    // ⚠️ Asserted per step, not per file: the "Compute blast radius" step
    // mounts nothing writable (its output is a shell redirect written by the
    // runner), so a whole-file `toContain('--user')` would pass even if this
    // step lost the flag again.
    const yaml = cicdGenerateGitHubActionsPreview(baseConfig());
    const step = yaml.slice(yaml.indexOf('- name: Generate routes'),
      yaml.indexOf('- name: Compute blast radius'));
    expect(step).toContain('-v ${{ github.workspace }}/.output:/data/output');
    expect(step).toContain('--user $(id -u):$(id -g)');
    expect(step.indexOf('--user')).toBeLessThan(step.indexOf('ghcr.io/vencil/da-tools'));
  });

  it('keeps the tenant config mounted read-only in every step', () => {
    // The workflow we hand customers should never give a container write
    // access to their conf.d — these steps only read it. Dropping `:ro`
    // survived both suites, and it is the kind of edit that looks like
    // tidying: the command still works, so nothing goes red until something
    // writes there.
    const yaml = cicdGenerateGitHubActionsPreview(baseConfig());
    const mounts = yaml.match(/-v \$\{\{ github\.workspace \}\}\/conf\.d:[^ \\]*/g) ?? [];
    expect(mounts.length).toBeGreaterThan(0);
    for (const m of mounts) {
      expect(m).toMatch(/:ro$/);
    }
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

  // ⛔ #1357. The pipeline above is inert on its own — GitLab auto-loads the
  // repo-root `.gitlab-ci.yml` and nothing else — so listing the .gitlab-ci.d
  // file without this one described a setup that never runs. Paired with the
  // negative below: emitting the root shell under ci=github would have GitLab
  // include a file that selection never wrote.
  it('emits the root .gitlab-ci.yml shell when ci=gitlab', () => {
    expect(cicdGeneratedPaths(baseConfig({ ci: 'gitlab' })))
      .toContain('.gitlab-ci.yml');
  });

  it('emits NO gitlab paths when ci=github', () => {
    expect(cicdGeneratedPaths(baseConfig({ ci: 'github' })).filter(p => p.startsWith('.gitlab-ci')))
      .toEqual([]);
  });

  it('emits BOTH github + gitlab when ci=both', () => {
    const out = cicdGeneratedPaths(baseConfig({ ci: 'both' }));
    expect(out).toContain('.github/workflows/dynamic-alerting.yaml');
    expect(out).toContain('.gitlab-ci.d/dynamic-alerting.yml');
    expect(out).toContain('.gitlab-ci.yml');
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
