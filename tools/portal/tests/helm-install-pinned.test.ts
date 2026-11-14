/**
 * No portal generator hands a customer an unpinned chart install (#2947).
 *
 * `helm upgrade --install <oci chart>` with no exact `--version` installs
 * whatever the registry holds as newest on the day the customer runs it: the
 * chart-shaped `:latest`. #2932 closed that for `da-tools init` and guards it
 * in tests/ops/test_init_project.py, but that guard walks init's output only;
 * the Deployment Profile Wizard emitted the same shape unseen.
 *
 * This is the portal-side counterpart, keyed on the helm COMMAND in each
 * generator's real output, with the same detector as that guard.
 *
 * A generator that this file does not run cannot be checked by it, so the
 * last test fails when a source file that carries an OCI chart ref is not in
 * GENERATORS: adding a third generator means adding it here. It sees the
 * literal `oci://` only; a generator that installs from a `helm repo add`
 * alias is outside it.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { resolve, dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deployGenerateHelmReleases } from '../src/interactive/tools/deployment-wizard/utils/generators.js';
import { cicdGenerateGitHubActionsPreview } from '../src/interactive/tools/cicd-setup-wizard/utils/generators.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(__dirname, '../src');

const HELM_INSTALL = /\bhelm\s+(?:upgrade|install)\b/;
// One exact release. `^2.9.0`, `~2.9`, `>=2.0.0`, `2.x` and `latest` are
// constraints helm resolves at run time.
const HELM_PINNED = /(?:^|\s)--version[ =]v?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?=\s|$)/;

/** [helm commands found, those without an exact --version]; `\` continuations joined, comment lines skipped. */
function unpinnedHelmInstalls(text: string): [string[], string[]] {
  const logical: string[] = [];
  let buf = '';
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!buf && line.startsWith('#')) continue;
    if (line.endsWith('\\')) { buf += `${line.slice(0, -1)} `; continue; }
    logical.push(buf + line);
    buf = '';
  }
  if (buf) logical.push(buf);
  const found = logical.filter(l => HELM_INSTALL.test(l));
  return [found, found.filter(l => !HELM_PINNED.test(l))];
}

// Each generator's customer-facing output, over every input that changes
// which commands it emits.
const GENERATORS: Record<string, () => string[]> = {
  'interactive/tools/deployment-wizard/utils/generators.js': () => {
    const out: string[] = [];
    for (const tier of ['tier1', 'tier2'])
      for (const environment of ['local', 'staging', 'production'])
        for (const auth of ['github', 'google', 'gitlab', 'oidc'])
          for (const rel of deployGenerateHelmReleases({ tier, environment, tenantSize: 'medium', auth, packs: ['mariadb'] }).releases)
            out.push(rel.install, rel.yaml);
    return out;
  },
  'interactive/tools/cicd-setup-wizard/utils/generators.js': () =>
    ['kustomize', 'helm'].map(deploy =>
      cicdGenerateGitHubActionsPreview({ ci: 'github', deploy, tenants: ['tenant-a'], packs: ['mariadb-core'] })),
};

describe('portal generators — every helm install names an exact chart version', () => {
  it.each(Object.keys(GENERATORS))('%s', (name) => {
    const commands: string[] = [];
    const offenders: string[] = [];
    for (const text of GENERATORS[name]()) {
      const [found, bad] = unpinnedHelmInstalls(text);
      commands.push(...found);
      offenders.push(...bad);
    }
    // Anti-vacuity: no command found means the detector or the generator
    // stopped producing one, and "no offenders" would say nothing.
    expect(commands.length, 'helm commands found').toBeGreaterThan(0);
    expect(offenders).toEqual([]);
  });

  it('the detector actually detects', () => {
    // The command the deployment wizard shipped before #2947.
    const before = 'helm upgrade --install tenant-api oci://ghcr.io/vencil/charts/tenant-api -n tenant-api --create-namespace -f values-tenant-api.yaml';
    expect(unpinnedHelmInstalls(before)[1]).toEqual([before]);
    for (const spec of ['--version ^2.9.0', '--version ~2.9', '--version ">=2.0.0"', '--version 2.x', '--version latest', '--version', '--version 2.9'])
      expect(unpinnedHelmInstalls(`helm install x oci://r/c ${spec}\n`)[1], spec).toHaveLength(1);
    for (const spec of ['--version 2.9.0', '--version=2.9.0', '--version 2.10.0-rc.1'])
      expect(unpinnedHelmInstalls(`helm install x oci://r/c ${spec} -n y\n`)[1], spec).toHaveLength(0);
    // Continuations are joined: the pin may sit on a later line.
    expect(unpinnedHelmInstalls('helm upgrade --install x \\\n  oci://r/c --version 1.2.3 \\\n  -n y\n')).toEqual(
      [['helm upgrade --install x  oci://r/c --version 1.2.3  -n y'], []],
    );
    // A comment that mentions the command is not one.
    expect(unpinnedHelmInstalls('# run helm upgrade --install later\n')[0]).toEqual([]);
  });

  it('every source file that carries an OCI chart ref is a generator this file runs', () => {
    const walk = (dir: string): string[] =>
      readdirSync(dir, { withFileTypes: true }).flatMap(e =>
        e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)]);
    const carriers = walk(SRC)
      .filter(f => /\.(?:jsx?|tsx?)$/.test(f) && readFileSync(f, 'utf8').includes('oci://'))
      .map(f => relative(SRC, f).split('\\').join('/'))
      .sort();
    expect(carriers).toEqual(Object.keys(GENERATORS).sort());
  });
});
