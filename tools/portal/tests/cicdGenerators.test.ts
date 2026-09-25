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
  CICD_DEFAULT_DA_TOOLS_IMAGE,
  cicdDaToolsImage,
  cicdImageIsMutable,
  cicdSplitImageRef,
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

// Since #1351 ruling (a) the preview IS the file `da-tools init` writes, and
// that file names the image once, in `env:`, and refers to it everywhere else
// as this expression. Anything below that locates "the image" in a command
// looks for THIS token — the literal registry name no longer appears there.
const IMAGE_REF = '${{ env.DA_TOOLS_IMAGE }}';

// Every `docker run` command in a workflow, continuation lines included. A
// shell comment that merely mentions `docker run` is not a command.
const dockerRuns = (yaml: string): string[] => {
  const lines = yaml.split('\n');
  const out: string[] = [];
  for (let i = 0; i < lines.length; i += 1) {
    if (!/\bdocker run\b/.test(lines[i]) || lines[i].trim().startsWith('#')) continue;
    const cmd = [lines[i]];
    while (lines[i].trimEnd().endsWith('\\') && i + 1 < lines.length) {
      i += 1;
      cmd.push(lines[i]);
    }
    out.push(cmd.join('\n'));
  }
  return out;
};

// The value of `env: DA_TOOLS_IMAGE:` — the one place the image is spelled.
const envImage = (yaml: string): string | null => {
  const m = yaml.match(/^env:\n(?: {2}\S.*\n)*? {2}DA_TOOLS_IMAGE: (.+)$/m);
  return m ? m[1] : null;
};

// #1495's rule for ONE `docker run` command: a writable mount (`-v` without
// `:ro`) needs `--user $(id -u):$(id -g)` placed before the image, or the
// container (uid 10001) cannot write into the runner's checkout. Returns the
// violations; an empty list is a pass.
//
// ⛔ A command whose image token cannot be found is itself a violation, not a
// skip. The previous form compared `indexOf('--user')` against
// `indexOf('ghcr.io/vencil/da-tools')`, and once the preview started naming
// the image through `env:` that second index would have been -1 — making
// "--user before the image" unsatisfiable-looking yet never evaluated, because
// no writable mount existed to reach it. A tripwire that goes blind the day
// the output changes shape is the failure #1351 ruled on.
const writableMountViolations = (cmd: string): string[] => {
  const mounts = [...cmd.matchAll(/-v ("[^"]*"|\S+)/g)].map((m) => m[1].replace(/^"|"$/g, ''));
  const writable = mounts.filter((m) => !m.endsWith(':ro'));
  if (writable.length === 0) return [];
  const imageAt = cmd.indexOf(IMAGE_REF);
  if (imageAt === -1) return [`cannot find ${IMAGE_REF} in a command with writable mount(s) ${writable.join(', ')}`];
  const userAt = cmd.indexOf('--user $(id -u):$(id -g)');
  if (userAt === -1) return [`writable mount(s) without --user: ${writable.join(', ')}`];
  if (userAt > imageAt) return [`--user after the image, where docker passes it to the container: ${writable.join(', ')}`];
  return [];
};

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
  it('gives no container a writable mount, so no step needs --user', () => {
    // #1423 / #1650: the "Generate routes" step used to mount `.output`
    // writable and pass `-o /data/output/routes.yaml --validate` — but
    // `--validate` returns before any file is written, so the mount was only
    // a permission surface (#1495: image uid 10001 vs the runner's `.output`)
    // and the tool now refuses `-o` under `--validate` (exit 2). The CLI twin
    // (scripts/tools/ops/init_project.py, "Generate Alertmanager routes")
    // dropped the mount and the flag at the same time; this pins the preview
    // to the same shape. `.output/` is still written — by the runner, through
    // the blast-radius shell redirect — never by a container.
    const yaml = cicdGenerateGitHubActionsPreview(baseConfig());
    // Step names come from the artifact now (#1351 後續待辦: the preview's
    // nine job × deploy step lists all differed from what `init` writes), so
    // these are the artifact's names, not the preview's old shorter ones.
    const step = yaml.slice(yaml.indexOf('- name: Generate Alertmanager routes'),
      yaml.indexOf('- name: Resolve base config snapshot'));
    expect(step).not.toBe('');
    expect(step).toContain('generate-routes --config-dir /data/conf.d --validate');
    expect(step).not.toMatch(/generate-routes[^\n]* -o /);
    expect(step).not.toContain('/data/output');
    // The command, not the step: the artifact's shell comment in this step
    // explains why there is no `--user` override, and names the flag to do so.
    const cmds = dockerRuns(step);
    expect(cmds).toHaveLength(1);
    expect(cmds[0]).not.toContain('--user');
    expect(yaml).not.toContain('/data/output');
  });

  it('runs every container that mounts something writable as the runner', () => {
    // Derived, not enumerated: walk every `docker run` command and apply
    // #1495's rule to each. Today no command mounts anything writable (the
    // row above), so this is the tripwire for the next one someone adds — and
    // the test after this one proves the tripwire can still fire.
    const runs = dockerRuns(cicdGenerateGitHubActionsPreview(baseConfig()));
    expect(runs.length).toBeGreaterThan(0);
    for (const cmd of runs) {
      expect(cmd, 'every docker run must name the image through env:').toContain(IMAGE_REF);
      expect(writableMountViolations(cmd), cmd).toEqual([]);
    }
  });

  it('has a tripwire that still fires on the shape the preview now has', () => {
    // The rule above passes vacuously on today's output, so its teeth are
    // shown here, on commands written in the preview's own form.
    const ro = '-v "${{ github.workspace }}/${{ env.CONFIG_DIR }}:/data/conf.d:ro"';
    const rw = '-v "${{ github.workspace }}/.output:/data/output"';
    const user = '--user $(id -u):$(id -g)';
    const run = (...parts: string[]) => ['docker run --rm', ...parts].join(' \\\n  ');
    expect(writableMountViolations(run(ro, IMAGE_REF, 'lint'))).toEqual([]);
    expect(writableMountViolations(run(user, rw, IMAGE_REF, 'x'))).toEqual([]);
    expect(writableMountViolations(run(rw, IMAGE_REF, 'x'))).toHaveLength(1);
    expect(writableMountViolations(run(rw, IMAGE_REF, user, 'x'))).toHaveLength(1);
    // The blind spot the old `indexOf(literal)` form had: no findable image.
    expect(writableMountViolations(run(user, rw, 'ghcr.io/vencil/da-tools:latest', 'x'))).toHaveLength(1);
  });

  it('keeps the tenant config mounted read-only in every step', () => {
    // The workflow we hand customers should never give a container write
    // access to their conf.d — these steps only read it. Dropping `:ro`
    // survived both suites, and it is the kind of edit that looks like
    // tidying: the command still works, so nothing goes red until something
    // writes there. Identified by the CONTAINER side, because the host side is
    // `${{ env.CONFIG_DIR }}` (and `.output/base/…` for the diff's baseline).
    const mounts = dockerRuns(cicdGenerateGitHubActionsPreview(baseConfig()))
      .flatMap((cmd) => [...cmd.matchAll(/-v ("[^"]*"|\S+)/g)].map((m) => m[1].replace(/^"|"$/g, '')))
      .filter((m) => /:\/data\/conf\.d(?:\.base)?(?::|$)/.test(m));
    expect(mounts.length).toBeGreaterThanOrEqual(3);
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

  // ⛔ These replace assertions that pinned the OPPOSITE — "emits
  // kustomize/ when deploy=kustomize or argocd" and "emits argocd/ ONLY when
  // deploy=argocd" (that deploy method is retired, #1351; the lesson is not).
  // Both were green and both were wrong: `da-tools init`
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

  // ⚠️ Was `it.each(['helm', 'argocd'])`. `--deploy argocd` is retired (#1351)
  // — it scaffolded no Application while its apply stage synced one — so the
  // `argocd/` half of this assertion now has no deploy method that could
  // reintroduce it. It is kept as a NEGATIVE on the surviving method because
  // that tree is exactly what the wizard once promised and never delivered.
  it('scaffolds NO kustomize/ or argocd/ tree for deploy=helm', () => {
    const out = cicdGeneratedPaths(baseConfig({ deploy: 'helm' }));
    expect(out.filter(p => p.startsWith('kustomize/') || p.startsWith('argocd/'))).toEqual([]);
    expect(cicdGenerateFileTree(baseConfig({ deploy: 'helm' }))).not.toContain('argocd/');
  });

  it('refuses a deploy method it has no apply block for', () => {
    // ⛔ The apply block is a ternary chain. When its last arm stopped being
    // argocd the natural spelling left `''` there, which renders a job with an
    // EMPTY `steps:` — valid JavaScript, invalid workflow. The CLI leg's two
    // builders raise for the same input; this pins the pair.
    for (const deploy of ['argocd', 'flux', '', undefined]) {
      expect(() => cicdGenerateGitHubActionsPreview(baseConfig({ deploy })))
        .toThrow(/unknown deploy method/);
    }
  });

  // ⛔ Renamed from "scaffolds NO deployment files", which became false when
  // issue 1454 B gave `--deploy helm` a values skeleton. The two trees it
  // still promises nothing for are named in the title now, because "no
  // deployment files" is the kind of claim that goes stale silently.
  it('scaffolds the helm values skeleton for deploy=helm only', () => {
    // Set equality per tenant-independent path, same discipline as the
    // kustomize case above: an added path has to be justified against the CLI,
    // and tests/ops/test_generated_ci_artifacts.py cross-checks both against
    // the real run_init().
    expect(cicdGeneratedPaths(baseConfig({ deploy: 'helm' })).filter(p => p.startsWith('environments/')))
      .toEqual(['environments/prod/values.yaml']);
    expect(cicdGeneratedPaths(baseConfig({ deploy: 'kustomize' })).filter(p => p.startsWith('environments/')))
      .toEqual([]);
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
  it('carries the workflow name `da-tools init` writes', () => {
    // #1351 ruling (a): the preview is the artifact, header comment included,
    // so the name is the artifact's (`Dynamic Alerting`), not the preview's
    // old `… CI/CD`. tests/ops/test_generated_ci_artifacts.py holds the whole
    // file equal to a real run_init(); this is the node-free floor.
    const out = cicdGenerateGitHubActionsPreview(baseConfig());
    expect(out).toMatch(/^# Dynamic Alerting CI\/CD Pipeline\n/);
    expect(out).toMatch(/\nname: Dynamic Alerting\n/);
  });

  it('watches the trees the chosen deploy method actually reads (#1351/#1473)', () => {
    // ⛔ Was `paths: ['conf.d/**']` for every deploy method. That is what the
    // preview said while `da-tools init` wrote three trees — a customer reading
    // it concluded that editing a rule pack starts nothing. The per-deploy set
    // is the point, so this asserts the DIFFERENCE between the methods rather
    // than a single string: helm's apply step reads
    // environments/prod/values.yaml and kustomize's builds
    // kustomize/overlays/prod.
    // The two legs are held equal by
    // tests/ops/test_generated_ci_artifacts.py, which compares this output
    // against a real `run_init()` run; this one keeps the wizard's own side
    // honest when node-less environments skip that comparison.
    const paths = (deploy: 'kustomize' | 'helm') => {
      const out = cicdGenerateGitHubActionsPreview(baseConfig({ deploy }));
      expect(out).toContain('pull_request:');
      expect(out).toContain('push:');
      // Block-style lists now, as the artifact writes them: collect the
      // `- '…'` items under each `paths:` key.
      const lines = out.split('\n');
      const lists = lines.flatMap((l, i) => {
        if (l.trim() !== 'paths:') return [];
        const items: string[] = [];
        for (let j = i + 1; j < lines.length && /^\s+- '/.test(lines[j]); j += 1) items.push(lines[j].trim());
        return [items.join(' ')];
      });
      // Both events, one rendered list: a single `paths:` would mean the push
      // leg lost its filter and now runs on every push.
      expect(lists).toHaveLength(2);
      expect(lists[0]).toBe(lists[1]);
      return lists[0];
    };
    expect(paths('kustomize')).toBe("- 'conf.d/**' - 'kustomize/**' - 'rule-packs/**'");
    expect(paths('helm')).toBe("- 'conf.d/**' - 'environments/**' - 'rule-packs/**'");
  });

  it('declares a validate job on ubuntu-latest', () => {
    const out = cicdGenerateGitHubActionsPreview(baseConfig());
    expect(out).toContain('validate:');
    expect(out).toContain('runs-on: ubuntu-latest');
  });
});

describe('da-tools image is configurable (#1351)', () => {
  // The CLI leg has had `--da-tools-image` (scripts/tools/ops/init_project.py,
  // default DA_TOOLS_IMAGE) since it shipped; this hand-kept twin had the
  // reference typed into four template literals. These pin the asymmetry
  // closed from the wizard's side. The drift gate holding the two DEFAULTS
  // equal is a separate step and is deliberately not asserted here.

  it('defaults to the same reference the CLI defaults to', () => {
    // ⚠️ The literal, not a re-export dance: `cicdGenerateDockerCommand`'s own
    // `toContain('ghcr.io/vencil/da-tools:latest')` above would still pass if
    // the constant and the generator drifted apart in the same direction, so
    // this names the value once more at the source of truth.
    expect(CICD_DEFAULT_DA_TOOLS_IMAGE).toBe('ghcr.io/vencil/da-tools:latest');
    expect(cicdDaToolsImage(baseConfig())).toBe(CICD_DEFAULT_DA_TOOLS_IMAGE);
  });

  it('falls back to the default when the field is blank or whitespace', () => {
    // The field is a free-text input the customer can clear; an empty value
    // reaching the template would emit `docker run  init` (two spaces, no
    // image) and `docker` would treat `init` as the image name.
    for (const daToolsImage of ['', '   ', '\t']) {
      expect(cicdDaToolsImage(baseConfig({ daToolsImage }))).toBe(CICD_DEFAULT_DA_TOOLS_IMAGE);
    }
    expect(cicdGenerateDockerCommand(baseConfig({ daToolsImage: '  ' })))
      .toContain(CICD_DEFAULT_DA_TOOLS_IMAGE);
  });

  it('trims surrounding whitespace off a real value', () => {
    expect(cicdDaToolsImage(baseConfig({ daToolsImage: '  registry.internal/da-tools:v1  ' })))
      .toBe('registry.internal/da-tools:v1');
  });

  it('puts --da-tools-image into the init command a custom image implies', () => {
    // ⛔ Not cosmetic. The flag is what decides the CONTENT `init` writes:
    // scripts/tools/ops/init_project.py threads it into _gen_github_actions /
    // _gen_gitlab_ci / _gen_precommit_snippet. Running the real init_project.py
    // twice, with and without the flag, the generated .github workflow differs
    // on exactly `DA_TOOLS_IMAGE:`. Omit it here and the customer reads a
    // preview naming their registry, runs the command we showed them, and gets
    // :latest in the file on disk — #1351's headline defect, re-created by the
    // change that closes #1351's image row.
    const out = cicdGenerateInitCommand(baseConfig({ daToolsImage: 'registry.internal:5000/da-tools:v1' }));
    expect(out).toContain('--da-tools-image registry.internal:5000/da-tools:v1');
    expect(out.indexOf('--da-tools-image')).toBeLessThan(out.indexOf('--non-interactive'));
  });

  it('omits the flag at the default, where it would be a no-op', () => {
    // The CLI's own default IS this value (init_project.py DA_TOOLS_IMAGE), so
    // emitting it would add a line the customer has to read past. Pinned in
    // both directions so "omit" cannot quietly become "never emit".
    for (const cfg of [baseConfig(), baseConfig({ daToolsImage: '  ' }),
      baseConfig({ daToolsImage: CICD_DEFAULT_DA_TOOLS_IMAGE })]) {
      expect(cicdGenerateInitCommand(cfg)).not.toContain('--da-tools-image');
    }
  });

  it('shows the same image in the init flag, the docker wrapper and the preview', () => {
    // The three artifacts sit on one screen. Any pair disagreeing is the
    // wizard telling the customer two different things at once.
    const daToolsImage = 'registry.internal:5000/da-tools:v1';
    const cfg = baseConfig({ daToolsImage });
    expect(cicdGenerateInitCommand(cfg)).toContain(`--da-tools-image ${daToolsImage}`);
    expect(cicdGenerateDockerCommand(cfg)).toContain(`--da-tools-image ${daToolsImage}`);
    expect(cicdGenerateGitHubActionsPreview(cfg)).toContain(daToolsImage);
  });

  it('still strips only the leading "da-tools " when nesting into docker', () => {
    // The nesting is `init.replace('da-tools ', '')`, a first-match replace.
    // `--da-tools-image` is not a match (no space after `da-tools`), but the
    // flag put a second `da-tools` substring into the string being rewritten,
    // so the invariant the older test pins is re-checked with it present.
    const out = cicdGenerateDockerCommand(baseConfig({ daToolsImage: 'registry.internal:5000/da-tools:v1' }));
    expect(out.match(/da-tools init/g) ?? []).toHaveLength(0);
    expect(out).toContain('--da-tools-image registry.internal:5000/da-tools:v1');
  });

  it('carries a custom image into the docker one-liner', () => {
    const out = cicdGenerateDockerCommand(baseConfig({ daToolsImage: 'registry.internal/da-tools:v1' }));
    expect(out).toContain('registry.internal/da-tools:v1');
    expect(out).not.toContain('ghcr.io/vencil/da-tools');
  });

  it('carries a custom image into EVERY docker run in the workflow preview', () => {
    // Derived, not counted: a `toContain` would pass while one step kept a
    // hardcoded reference. The artifact's shape is "spelled once in env:,
    // referenced everywhere else", so require exactly that: env holds the
    // configured image, every docker run refers to it, and no other registry
    // reference is left in the file.
    const yaml = cicdGenerateGitHubActionsPreview(baseConfig({ daToolsImage: 'registry.internal/da-tools:v1' }));
    expect(envImage(yaml)).toBe('registry.internal/da-tools:v1');
    const runs = dockerRuns(yaml);
    expect(runs.length).toBeGreaterThan(0);
    for (const cmd of runs) {
      expect(cmd).toContain(IMAGE_REF);
    }
    expect(yaml).not.toContain('ghcr.io/vencil/da-tools');
  });

  it('keeps the default in the preview when nothing is configured', () => {
    const yaml = cicdGenerateGitHubActionsPreview(baseConfig());
    expect(envImage(yaml)).toBe(CICD_DEFAULT_DA_TOOLS_IMAGE);
    const runs = dockerRuns(yaml);
    expect(runs.length).toBeGreaterThan(0);
    for (const cmd of runs) {
      expect(cmd).toContain(IMAGE_REF);
    }
  });
});

describe('a reference we cannot vouch for is called out (#1351, review on #1880)', () => {
  const noteLines = (yaml: string) =>
    yaml.split('\n').filter((l) => l.includes('can be repointed'));

  // ⛔ The predicate's exemption is a statement about US, so it may only be
  // applied to us. An earlier revision read components/da-tools/README.md's
  // "production 請釘特定版號" as a property of version tags in general and went
  // silent for `registry.internal/da-tools:v1` — telling a customer their own
  // registry's tag is immutable, which nothing in this file can know (CWE-494).
  // Measured before the fix: that reference produced 0 note lines.
  it('warns for a version tag on a registry whose policy is not ours', () => {
    const yaml = cicdGenerateGitHubActionsPreview(
      baseConfig({ daToolsImage: 'registry.internal/da-tools:v1' }));
    const lines = noteLines(yaml);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('registry.internal/da-tools:v1');
  });

  it('stays silent for a version tag on the repository we publish', () => {
    // The README policy is exactly this case and no wider: the sentence sits
    // directly above `docker pull ghcr.io/vencil/da-tools:latest`.
    expect(noteLines(cicdGenerateGitHubActionsPreview(
      baseConfig({ daToolsImage: 'ghcr.io/vencil/da-tools:v2.9.0' })))).toHaveLength(0);
  });

  it('stays silent for a digest, whatever the registry', () => {
    // ⛔ A digest is the only thing that actually cannot be repointed, so this
    // is the one exemption that needs no policy behind it.
    const digest = '@sha256:' + '0'.repeat(64);
    for (const repo of ['ghcr.io/vencil/da-tools', 'registry.internal/da-tools']) {
      expect(noteLines(cicdGenerateGitHubActionsPreview(
        baseConfig({ daToolsImage: repo + digest })))).toHaveLength(0);
    }
  });

  it('emits exactly one note for the default, and it is a YAML comment', () => {
    const lines = noteLines(cicdGenerateGitHubActionsPreview(baseConfig()));
    expect(lines).toHaveLength(1);
    expect(lines[0].startsWith('#')).toBe(true);
    expect(lines[0]).toContain(CICD_DEFAULT_DA_TOOLS_IMAGE);
  });

  it('places the note where it cannot break the workflow header', () => {
    // It has to survive `yaml.safe_load` and actionlint in the customer's
    // repo, so it sits on its own line directly above `jobs:`, just below the
    // `env:` block that declares DA_TOOLS_IMAGE. It is the ONE line the
    // preview adds to what `da-tools init` writes; the Python drift gate
    // removes exactly this line, at exactly this position, before comparing.
    const yaml = cicdGenerateGitHubActionsPreview(baseConfig());
    expect(yaml).toMatch(/\nname: Dynamic Alerting\n/);
    expect(yaml).toMatch(/\n# \S[^\n]*can be repointed[^\n]*\njobs:\n/);
  });
});

describe('cicdImageIsMutable', () => {
  // Exported because the note above is the only other observer, and a
  // whole-YAML assertion cannot say WHICH input produced the answer.
  it('calls a digest immutable and everything else mutable, except our own version tags', () => {
    const cases: Array<[string, boolean]> = [
      [CICD_DEFAULT_DA_TOOLS_IMAGE, true],
      ['ghcr.io/vencil/da-tools', true],
      ['ghcr.io/vencil/da-tools:v2.9.0', false],
      ['ghcr.io/vencil/da-tools@sha256:' + '0'.repeat(64), false],
      ['registry.internal/da-tools:v1', true],
      ['registry.internal/da-tools:latest', true],
      ['registry.internal/da-tools', true],
      ['registry.internal/da-tools@sha256:' + '0'.repeat(64), false],
    ];
    for (const [image, expected] of cases) {
      expect(cicdImageIsMutable(image), image).toBe(expected);
    }
  });

});

describe('cicdSplitImageRef', () => {
  // ⛔ Tested here rather than through `cicdImageIsMutable`, because through
  // that predicate the port branch is INVISIBLE: our registry has no port, so
  // every port-bearing reference is foreign and mutable however it is split.
  // Measured — deleting the `/`-anchoring left all 49 tests green, which is
  // why this describe exists at all rather than a comment claiming coverage.
  it('takes the tag from the last path segment, so a registry port is not a tag', () => {
    expect(cicdSplitImageRef('registry.internal:5000/da-tools')).toEqual({
      repo: 'registry.internal:5000/da-tools', tag: null, digest: false,
    });
    expect(cicdSplitImageRef('registry.internal:5000/da-tools:v1')).toEqual({
      repo: 'registry.internal:5000/da-tools', tag: 'v1', digest: false,
    });
  });

  it('reports a digest without inventing a tag', () => {
    const d = '@sha256:' + '0'.repeat(64);
    expect(cicdSplitImageRef('ghcr.io/vencil/da-tools' + d)).toEqual({
      repo: 'ghcr.io/vencil/da-tools', tag: null, digest: true,
    });
  });

  it('splits the shipped default the way the repository comparison needs', () => {
    // The exemption compares `repo` against this value, so a parse that put
    // the tag into `repo` would silence the note for every ghcr.io reference.
    expect(cicdSplitImageRef(CICD_DEFAULT_DA_TOOLS_IMAGE)).toEqual({
      repo: 'ghcr.io/vencil/da-tools', tag: 'latest', digest: false,
    });
  });
});
