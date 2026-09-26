/**
 * Tests for deployment-wizard's per-chart Helm values generator.
 *
 * THE LOAD-BEARING GATE is the first describe block: every leaf key the
 * wizard emits, for every config combination, must be read by the target
 * chart's templates. The previous generator emitted an umbrella-chart shape
 * (`thresholdExporter:` / `prometheus:` / `platform:` …) that no chart in
 * helm/ has, so a customer following its own `helm install threshold-exporter
 * -f values.yaml` instruction got a file the chart read NONE of — including
 * `configValidation`, `cardinalityGuard` and `tripleState`, which are not
 * Helm values anywhere. Substring assertions on the output (what this file
 * used to contain) could not see that: they proved the text was emitted, not
 * that anything consumed it.
 *
 * "Read by the chart" is resolved structurally, not by grep: each emitted
 * values file is parsed to leaf paths, and each chart's templates are
 * tokenised into `{{ … }}` actions whose `.Values.*` / `$.Values.*`
 * references (plus relative `.x` inside `with` / `range`) are collected. A
 * leaf is consumed iff some reference is a prefix of it (e.g. `toYaml
 * .Values.resources` consumes `resources.limits.cpu`). The resolver itself
 * carries controls: it must find keys known to be read and must reject the
 * three fictional keys, otherwise a broken resolver would pass everything.
 *
 * NOTE: output embeds `# Generated: <today>`, so the date FORMAT is pinned,
 * never its value.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
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

// ── template side: .Values references a chart actually reads ────────────────
const NON_VALUES = new Set(['Release', 'Chart', 'Capabilities', 'Template', 'Files', 'Values']);
function chartValueRefs(chart: string): string[][] {
  const dir = join(HELM, chart, 'templates');
  const refs: string[][] = [];
  for (const f of readdirSync(dir).sort()) {
    const src = readFileSync(join(dir, f), 'utf8');
    // null = root / unknown context; 'IF' = transparent (if/else keep the context)
    const stack: (string[] | null | 'IF')[] = [];
    for (const m of src.matchAll(/\{\{-?([\s\S]*?)-?\}\}/g)) {
      const action = m[1].trim();
      if (action.startsWith('/*')) continue;
      const kw = action.split(/\s+/)[0];
      const ctx = [...stack].reverse().find(c => c !== 'IF') ?? null;
      const found: (string[] | null)[] = [];
      for (const r of action.matchAll(/(\$?)\.([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)/g)) {
        const parts = r[2].split('.');
        if (parts[0] === 'Values') found.push(parts.slice(1));
        else if (r[1] || NON_VALUES.has(parts[0]) || ctx === null || ctx === 'IF') found.push(null);
        else found.push([...ctx, ...parts]);
      }
      for (const p of found) if (p && p.length) refs.push(p);
      if (kw === 'with' || kw === 'range') {
        const target = found.find(Boolean) ?? null;
        // range elements are not addressable leaves of a scalar-only values file
        stack.push(kw === 'range' ? null : target);
      } else if (kw === 'if') stack.push('IF');
      else if (kw === 'define' || kw === 'block') stack.push(null);
      else if (kw === 'end') stack.pop();
    }
  }
  return refs;
}
const REFS: Record<string, string[][]> = {};
const isConsumed = (chart: string, leaf: string) => {
  REFS[chart] ??= chartValueRefs(chart);
  const p = leaf.split('.');
  return REFS[chart].some(r => r.length <= p.length && r.every((seg, i) => seg === p[i]));
};

describe('every emitted key is read by the target chart (structural, all configs)', () => {
  it('resolver controls: finds keys known to be read, rejects keys known to be fictional', () => {
    expect(isConsumed('threshold-exporter', 'replicaCount')).toBe(true);
    expect(isConsumed('threshold-exporter', 'resources.limits.cpu')).toBe(true); // via toYaml subtree
    expect(isConsumed('da-portal', 'oauth2Proxy.oidcIssuerUrl')).toBe(true);     // via `with`
    for (const fictional of [
      'tripleState.enabled', 'cardinalityGuard.maxPerTenant', 'configValidation.sha256',
      'thresholdExporter.replicaCount', 'prometheus.retention',
    ]) {
      expect(isConsumed('threshold-exporter', fictional), fictional).toBe(false);
    }
    // Declared in da-portal's values.yaml yet read by no template (the
    // ServiceAccount is created unconditionally under a fixed name): the
    // resolver must not treat "present in values.yaml" as "consumed".
    expect(isConsumed('da-portal', 'serviceAccount.create')).toBe(false);
    // #2027 gave both charts an Ingress template; ingress.* is now read.
    expect(isConsumed('da-portal', 'ingress.enabled')).toBe(true);
    expect(isConsumed('tenant-api', 'ingress.hosts')).toBe(true);
  });

  it('leafPaths control: parses nesting, and throws on shapes it cannot read', () => {
    expect(leafPaths('a:\n  b: 1\n  c:\n    d: x  # note\ne: 2\n')).toEqual(['a.b', 'a.c.d', 'e']);
    expect(() => leafPaths('a:\n  - x\n')).toThrow(/unsupported/);
    expect(() => leafPaths('a: [1, 2]\n')).toThrow(/unsupported/);
    expect(() => leafPaths('a:\nb: 1\n')).toThrow(/no value and no children/);
  });

  it(`no dead keys across ${ALL_CONFIGS.length} configs`, () => {
    const dead = new Set<string>();
    let checked = 0;
    for (const cfg of ALL_CONFIGS) {
      for (const rel of deployGenerateHelmReleases(cfg).releases) {
        const leaves = leafPaths(rel.yaml);
        expect(leaves.length, `${rel.chart} emitted nothing`).toBeGreaterThan(0);
        for (const leaf of leaves) {
          checked++;
          if (!isConsumed(rel.chart, leaf)) dead.add(`${rel.chart}: ${leaf}`);
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
