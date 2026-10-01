/**
 * Tests for deployment-wizard's per-chart Helm values generator.
 *
 * THE LOAD-BEARING GATE is the first describe block: every leaf key the
 * wizard emits, for every config combination, must be declared by the target
 * chart's values.yaml. The previous generator emitted an umbrella-chart shape
 * (`thresholdExporter:` / `prometheus:` / `platform:` …) that no chart in
 * helm/ has, so a customer following its own `helm install threshold-exporter
 * -f values.yaml` instruction got a file the chart read NONE of — including
 * `configValidation`, `cardinalityGuard` and `tripleState`, which are not
 * Helm values anywhere. Substring assertions on the output (what this file
 * used to contain) could not see that: they proved the text was emitted, not
 * that anything consumed it.
 *
 * Declared is enough because the other half is guarded on the Python side:
 * tests/helm/test_values_keys_are_read.py fails on any values.yaml leaf no
 * template reads (declared ⇒ read; its `_UNREAD_ALLOWED` table is the only
 * escape, so a key listed there must not be one the wizard emits). That guard
 * resolves Go template bindings properly; this file used to carry a second,
 * looser resolver of its own (#2555 F), and two resolvers drift. Each emitted
 * values file is parsed to leaf paths and compared with the leaves of the
 * chart's values.yaml, read by js-yaml. Controls: keys the chart declares must
 * be found, and the three fictional keys must be rejected, otherwise a broken
 * lookup would pass everything.
 *
 * NOTE: output embeds `# Generated: <today>`, so the date FORMAT is pinned,
 * never its value.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from 'js-yaml';
import { deployGenerateHelmReleases } from '../src/interactive/tools/deployment-wizard/utils/generators.js';
import { DEPLOY_RULE_PACKS } from '../src/interactive/tools/deployment-wizard/fixtures/wizard-defaults.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HELM = resolve(__dirname, '../../../helm');

type Cfg = { tier: string; environment: string; tenantSize: string; auth: string; packs: string[] };
const config = (overrides: Partial<Cfg> = {}): Cfg => ({
  tier: 'tier2',
  environment: 'production',
  tenantSize: 'medium',
  auth: 'github',
  packs: ['mariadb', 'redis'],
  ...overrides,
});
const ALL_CONFIGS: Cfg[] = [];
for (const tier of ['tier1', 'tier2'])
  for (const environment of ['local', 'staging', 'production'])
    for (const tenantSize of ['small', 'medium', 'large'])
      for (const auth of ['github', 'google', 'gitlab', 'oidc'])
        for (const packs of [[], ['mariadb', 'kubernetes']])
          ALL_CONFIGS.push({ tier, environment, tenantSize, auth, packs });

const releaseOf = (cfg: Cfg, chart: string) => {
  const r = deployGenerateHelmReleases(cfg).releases.find((x: { chart: string }) => x.chart === chart);
  if (!r) throw new Error(`no ${chart} release for ${JSON.stringify(cfg)}`);
  return r as { chart: string; namespace: string; file: string; install: string; yaml: string };
};

// ── values side: leaf paths of the emitted YAML ─────────────────────────────
// The generator only emits nested maps of scalars. This parser accepts exactly
// that and THROWS on anything else (lists, flow style, multi-line scalars), so
// a future generator change it cannot read fails loudly instead of being
// flattened wrong.
function leafPaths(yaml: string): string[] {
  const out: string[] = [];
  const stack: { indent: number; key: string }[] = [];
  let pendingParent: { indent: number; key: string } | null = null;
  for (const raw of yaml.split('\n')) {
    const line = raw.replace(/\s+#.*$/, '');
    if (!line.trim() || line.trim().startsWith('#')) continue;
    const m = line.match(/^( *)([A-Za-z_][\w.-]*):(?: (.*))?$/);
    if (!m) throw new Error(`leafPaths: unsupported YAML line: ${JSON.stringify(raw)}`);
    const indent = m[1].length;
    const value = m[3];
    if (pendingParent && indent <= pendingParent.indent) {
      throw new Error(`leafPaths: '${pendingParent.key}:' has no value and no children`);
    }
    pendingParent = null;
    while (stack.length && stack[stack.length - 1].indent >= indent) stack.pop();
    const path = [...stack.map(s => s.key), m[2]];
    if (value === undefined || value === '') {
      stack.push({ indent, key: m[2] });
      pendingParent = { indent, key: m[2] };
    } else {
      if (/^[[{|>]/.test(value)) throw new Error(`leafPaths: unsupported value form: ${JSON.stringify(raw)}`);
      out.push(path.join('.'));
    }
  }
  if (pendingParent) throw new Error(`leafPaths: '${pendingParent.key}:' has no value and no children`);
  return out;
}

// ── chart side: leaf paths values.yaml declares ─────────────────────────────
// Same leaf rule as tests/helm/test_values_keys_are_read.py `_leaves`: a
// non-empty map recurses, anything else (scalar, list, empty map) is a leaf.
function declaredLeaves(node: unknown, path: string[] = [], out: Set<string> = new Set()): Set<string> {
  if (node && typeof node === 'object' && !Array.isArray(node) && Object.keys(node).length) {
    for (const [k, v] of Object.entries(node)) declaredLeaves(v, [...path, k], out);
  } else if (path.length) out.add(path.join('.'));
  return out;
}
const DECLARED: Record<string, Set<string>> = {};
const isDeclared = (chart: string, leaf: string) => {
  DECLARED[chart] ??= declaredLeaves(load(readFileSync(join(HELM, chart, 'values.yaml'), 'utf8')));
  return DECLARED[chart].has(leaf);
};

describe('every emitted key is declared by the target chart (structural, all configs)', () => {
  it('lookup controls: finds keys the chart declares, rejects keys it does not', () => {
    expect(isDeclared('threshold-exporter', 'replicaCount')).toBe(true);
    expect(isDeclared('threshold-exporter', 'resources.limits.cpu')).toBe(true);
    expect(isDeclared('da-portal', 'oauth2Proxy.oidcIssuerUrl')).toBe(true);
    for (const fictional of [
      'tripleState.enabled', 'cardinalityGuard.maxPerTenant', 'configValidation.sha256',
      'thresholdExporter.replicaCount', 'prometheus.retention',
    ]) {
      expect(isDeclared('threshold-exporter', fictional), fictional).toBe(false);
    }
    // da-portal's values.yaml declared this key until #2532 removed it as
    // read by no template; it stays here as the negative control.
    expect(isDeclared('da-portal', 'serviceAccount.create')).toBe(false);
    // A map is not a leaf: only its children are.
    expect(isDeclared('threshold-exporter', 'resources')).toBe(false);
  });

  it('leafPaths control: parses nesting, and throws on shapes it cannot read', () => {
    expect(leafPaths('a:\n  b: 1\n  c:\n    d: x  # note\ne: 2\n')).toEqual(['a.b', 'a.c.d', 'e']);
    expect(() => leafPaths('a:\n  - x\n')).toThrow(/unsupported/);
    expect(() => leafPaths('a: [1, 2]\n')).toThrow(/unsupported/);
    expect(() => leafPaths('a:\nb: 1\n')).toThrow(/no value and no children/);
  });

  it(`no undeclared keys across ${ALL_CONFIGS.length} configs`, () => {
    const dead = new Set<string>();
    let checked = 0;
    for (const cfg of ALL_CONFIGS) {
      for (const rel of deployGenerateHelmReleases(cfg).releases) {
        const leaves = leafPaths(rel.yaml);
        expect(leaves.length, `${rel.chart} emitted nothing`).toBeGreaterThan(0);
        for (const leaf of leaves) {
          checked++;
          if (!isDeclared(rel.chart, leaf)) dead.add(`${rel.chart}: ${leaf}`);
        }
      }
    }
    expect(checked).toBeGreaterThan(ALL_CONFIGS.length); // guard the guard: loop ran
    expect([...dead]).toEqual([]);
  });

  it('only targets charts that exist in helm/', () => {
    const charts = new Set(readdirSync(HELM));
    for (const cfg of ALL_CONFIGS)
      for (const rel of deployGenerateHelmReleases(cfg).releases) expect(charts.has(rel.chart), rel.chart).toBe(true);
  });
});

describe('releases — tier gating and install commands', () => {
  it('tier1 → threshold-exporter only', () => {
    const { releases } = deployGenerateHelmReleases(config({ tier: 'tier1' }));
    expect(releases.map((r: { chart: string }) => r.chart)).toEqual(['threshold-exporter']);
  });

  it('tier2 → + da-portal (monitoring) + tenant-api (its own namespace, #1004)', () => {
    const { releases } = deployGenerateHelmReleases(config({ tier: 'tier2' }));
    expect(releases.map((r: { chart: string; namespace: string }) => `${r.chart}@${r.namespace}`))
      .toEqual(['threshold-exporter@monitoring', 'da-portal@monitoring', 'tenant-api@tenant-api']);
  });

  it('install command targets the chart, its namespace and its own values file', () => {
    const rel = releaseOf(config(), 'tenant-api');
    expect(rel.file).toBe('values-tenant-api.yaml');
    expect(rel.install).toBe(
      'helm upgrade --install tenant-api oci://ghcr.io/vencil/charts/tenant-api -n tenant-api --create-namespace -f values-tenant-api.yaml',
    );
  });

  it('emits a date-stamped header in YYYY-MM-DD form (format pinned, value not)', () => {
    expect(releaseOf(config(), 'threshold-exporter').yaml).toMatch(/# Generated: \d{4}-\d{2}-\d{2}\n/);
  });
});

describe('tenant size / environment drive the values the chart reads', () => {
  it('size → threshold-exporter replicaCount 1 / 2 / 3', () => {
    // The header label proves the size entry was looked up (the `|| 2`
    // fallback alone would keep 'medium' green on a failed lookup).
    const medium = releaseOf(config({ tenantSize: 'medium' }), 'threshold-exporter').yaml;
    expect(medium).toContain('/ Medium');
    expect(medium).not.toContain('undefined');
    expect(medium).toMatch(/^replicaCount: 2$/m);
    expect(releaseOf(config({ tenantSize: 'small' }), 'threshold-exporter').yaml).toMatch(/^replicaCount: 1$/m);
    expect(releaseOf(config({ tenantSize: 'large' }), 'threshold-exporter').yaml).toMatch(/^replicaCount: 3$/m);
  });

  it('environment → exporter resources', () => {
    expect(releaseOf(config({ environment: 'local' }), 'threshold-exporter').yaml).toContain('limits:\n    cpu: 200m\n    memory: 128Mi');
    expect(releaseOf(config({ environment: 'production' }), 'threshold-exporter').yaml).toContain('limits:\n    cpu: 1000m\n    memory: 512Mi');
  });

  it('production → operator-managed oauth2 Secret; local → no secure cookie', () => {
    expect(releaseOf(config({ environment: 'production' }), 'da-portal').yaml).toMatch(/^  createSecret: false$/m);
    expect(releaseOf(config({ environment: 'local' }), 'da-portal').yaml).toMatch(/^  cookieSecure: false$/m);
  });
});

describe('auth provider (tier2)', () => {
  it.each(['github', 'google', 'gitlab'])('%s → provider on both oauth2-proxy sidecars, no issuer URL', auth => {
    for (const chart of ['da-portal', 'tenant-api']) {
      const yaml = releaseOf(config({ auth }), chart).yaml;
      expect(yaml).toMatch(new RegExp(`^  provider: ${auth}$`, 'm'));
      expect(yaml).not.toContain('oidcIssuerUrl');
    }
  });

  it('oidc → provider oidc + an issuer URL placeholder', () => {
    const yaml = releaseOf(config({ auth: 'oidc' }), 'da-portal').yaml;
    expect(yaml).toMatch(/^  provider: oidc$/m);
    expect(yaml).toMatch(/^  oidcIssuerUrl: "https:\/\//m);
  });

  it('never overrides the chart-pinned oauth2-proxy image (#1302)', () => {
    for (const chart of ['da-portal', 'tenant-api'])
      expect(releaseOf(config(), chart).yaml).not.toContain('oauth2-proxy/oauth2-proxy');
  });
});

describe('notes — features that are not Helm values are explained, not emitted', () => {
  const notesOf = (cfg: Cfg) => deployGenerateHelmReleases(cfg).notes as { id: string; title: string; body: string }[];

  it('silent mode / cardinality / hot-reload / Prometheus are notes on every tier', () => {
    for (const tier of ['tier1', 'tier2']) {
      const ids = notesOf(config({ tier })).map(n => n.id);
      for (const id of ['silent-mode', 'cardinality', 'hot-reload', 'prometheus-alertmanager', 'rule-packs'])
        expect(ids, `${tier} missing ${id}`).toContain(id);
    }
    const byId = Object.fromEntries(notesOf(config()).map(n => [n.id, n.body]));
    expect(byId['silent-mode']).toContain('_silent_mode');
    expect(byId['cardinality']).toContain('500');
    expect(byId['prometheus-alertmanager']).toContain('retention 14d');
  });

  it('rule-pack note names the ConfigMap for each selected pack', () => {
    const body = notesOf(config({ packs: ['mariadb', 'redis'] })).find(n => n.id === 'rule-packs')!.body;
    expect(body).toContain('configmap-rules-mariadb.yaml');
    expect(body).toContain('configmap-rules-redis.yaml');
  });

  it('every pack in the catalog names a ConfigMap that exists in k8s/03-monitoring/', () => {
    const shipped = new Set(readdirSync(resolve(HELM, '../k8s/03-monitoring')));
    const ids = DEPLOY_RULE_PACKS.map((p: { id: string }) => p.id);
    expect(ids.length).toBeGreaterThan(0);
    const body = notesOf(config({ packs: ids })).find(n => n.id === 'rule-packs')!.body;
    for (const name of body.match(/configmap-rules-[\w-]+\.yaml/g) ?? []) expect(shipped.has(name), name).toBe(true);
    expect(body.match(/configmap-rules-[\w-]+\.yaml/g)?.length).toBe(ids.length);
  });

  it('the three fictional keys are gone from every emitted file', () => {
    for (const cfg of ALL_CONFIGS)
      for (const rel of deployGenerateHelmReleases(cfg).releases)
        for (const k of ['configValidation', 'cardinalityGuard', 'tripleState', 'maxPerTenant'])
          expect(rel.yaml, `${rel.chart} re-emitted ${k}`).not.toContain(k);
  });
});
