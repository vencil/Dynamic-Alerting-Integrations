/**
 * promql-tester/parse.js — simplified regex PromQL parser.
 *
 * These functions (analyzeQuery / simulateResults + RECORDING_RULES / MOCK_SERIES)
 * were extracted VERBATIM from promql-tester.jsx (portal ROI extraction, mirroring
 * the threshold-calculator / multi-tenant-comparison waves) where they had 0% unit
 * coverage while buried in the JSX. The extraction is a byte-identical move — no
 * logic change — so this suite LOCKS the parser's CURRENT behavior.
 *
 * This is a regex PARSER, not a real PromQL grammar, so it deliberately
 * over-approximates. Every expected value below is HAND-DERIVED by tracing the
 * regexes (then independently re-derived a second time), NOT captured from a run.
 * Several assertions PIN behavior that is surprising/quirky; each is flagged
 * `QUIRK` inline. Do NOT "fix" the parser to make a nicer value pass — these are
 * intentionally pinning what the shipped tool does today; a PM decides fix-vs-benign:
 *
 *   Q1  function names with `_` (histogram_quantile, predict_linear, label_replace,
 *       label_join) leak into `.metrics` — they have `_`, len>3, and are NOT in the
 *       metric denylist (only group_left/group_right are).
 *   Q2  label VALUES with `_` (e.g. {job="api_gateway"}) leak into `.metrics` — the
 *       metric scan runs over the whole lowercased string incl. inside quotes.
 *   Q3  `.functions` is NOT deduplicated (rate/…/rate → ['rate','rate']).
 *   Q4  duration regex `/\[(\d+[smhd])\]/` matches ONE `\d+`+single-unit only:
 *         compound `[1h30m]`, float `[1.5h]`, subquery `[5m:1m]` → duration null;
 *         multiple ranges → FIRST only.
 *   Q5  [FIXED in this PR] a subquery `rate(x[5m:1m])` HAS a range but Q4 can't parse
 *       it; the rate-needs-range warning now keys off a `[<digit>` range/subquery
 *       bracket (not `duration`), so the subquery no longer mis-warns.
 *   Q6  `matchedRules` 'underlying' is a raw `expr.includes(metric)` SUBSTRING match,
 *       so a partial/shared metric prefix (e.g. `kube_pod_container`) matches multiple
 *       semantically-unrelated rules.
 *   Q7  [FIXED in this PR] dedup still keeps 'direct' over 'underlying' per rule name,
 *       but the recording-rule SUGGESTION now keys off `hadUnderlyingMatch` captured
 *       BEFORE dedup, so a rule matched both direct+underlying still yields the hint.
 *   Q8  the irate long-window warning only triggers for unit === 'm' && num > 5;
 *       `[1h]` / `[300s]` never warn regardless of length.
 */
import { describe, it, expect } from 'vitest';
import {
  analyzeQuery,
  simulateResults,
  RECORDING_RULES,
  MOCK_SERIES,
} from '../src/interactive/tools/promql-tester/parse.js';

// Verbatim English fallback strings (tests run with window.__t undefined ⇒ t returns EN arg).
const W_RATE = 'rate() requires a range vector — ensure you include [duration]';
const W_IRATE = 'irate is typically used with short windows (e.g., 5m); for longer windows, consider rate';
const S_REC = 'This metric has a matching Recording Rule — consider using the recording rule name for better performance';

const names = (a: ReturnType<typeof analyzeQuery>) => a.matchedRules.map((r: any) => r.rule);
const types = (a: ReturnType<typeof analyzeQuery>) => a.matchedRules.map((r: any) => r.matchType);

describe('vector selectors — instant / range / subquery', () => {
  it('instant vector `up`: nothing detected (no `_`, len≤3)', () => {
    const a = analyzeQuery('up');
    expect(a).toEqual({
      functions: [],
      metrics: [],
      labels: [],
      duration: null,
      matchedRules: [],
      warnings: [],
      suggestions: [],
    });
  });

  it('range vector `rate(http_requests_total[5m])`: fn=rate, one metric, duration 5m, no warning', () => {
    const a = analyzeQuery('rate(http_requests_total[5m])');
    expect(a.functions).toEqual(['rate']);
    expect(a.metrics).toEqual(['http_requests_total']);
    expect(a.duration).toBe('5m');
    expect(a.matchedRules).toEqual([]);
    expect(a.warnings).toEqual([]); // duration present ⇒ no rate-needs-range warning
    expect(a.suggestions).toEqual([]);
  });

  it('subquery `rate(x[5m:1m])`: Q4 still leaves duration null, but the Q5 fix suppresses the spurious rate warning', () => {
    const a = analyzeQuery('rate(x[5m:1m])');
    expect(a.functions).toEqual(['rate']);
    expect(a.metrics).toEqual([]); // `x` len1 no `_`; `5m`/`1m` start with a digit
    expect(a.duration).toBeNull(); // Q4 (unchanged): `[5m:1m]` fails /\[(\d+[smhd])\]/ (`:` after unit)
    // Q5 FIX: the rate-needs-range warning now keys off a `[<digit>` range/subquery
    // bracket, not `duration`, so a subquery (which HAS a range) no longer mis-warns.
    expect(a.warnings).toEqual([]);
  });
});

describe('duration extraction — /\\[(\\d+[smhd])\\]/', () => {
  it('single unit + multi-digit are detected', () => {
    expect(analyzeQuery('some_metric_total[5m]').duration).toBe('5m');
    expect(analyzeQuery('some_metric_total[10m]').duration).toBe('10m');
    expect(analyzeQuery('some_metric_total[2h]').duration).toBe('2h');
    expect(analyzeQuery('some_metric_total[30s]').duration).toBe('30s');
    expect(analyzeQuery('some_metric_total[7d]').duration).toBe('7d');
  });

  it('QUIRK Q4 compound `[1h30m]` and float `[1.5h]` are NOT detected (null)', () => {
    expect(analyzeQuery('some_metric_total[1h30m]').duration).toBeNull();
    expect(analyzeQuery('some_metric_total[1.5h]').duration).toBeNull();
    // metric still detected regardless of the unparsed range
    expect(analyzeQuery('some_metric_total[1h30m]').metrics).toEqual(['some_metric_total']);
  });

  it('QUIRK Q4 multiple ranges → FIRST match only', () => {
    const a = analyzeQuery('rate(a_metric_one[5m]) + rate(b_metric_two[10m])');
    expect(a.duration).toBe('5m');
  });
});

describe('function extraction — allowlist, case-insensitive, NOT deduped', () => {
  it('allowlisted nested fns land in .functions in source order', () => {
    const a = analyzeQuery('topk(5, sum(rate(node_cpu_seconds_total[5m])))');
    expect(a.functions).toEqual(['topk', 'sum', 'rate']);
  });

  it('function match is case-insensitive (lowercased)', () => {
    expect(analyzeQuery('RATE(http_requests_total[5m])').functions).toEqual(['rate']);
  });

  it('QUIRK Q3 .functions is NOT deduplicated', () => {
    const a = analyzeQuery('rate(redis_keyspace_hits_total[5m]) / rate(redis_keyspace_misses_total[5m])');
    expect(a.functions).toEqual(['rate', 'rate']);
  });

  it('non-allowlisted callables are ignored', () => {
    // `foo(` and `clamp_max(` are not in the allowlist → no functions
    expect(analyzeQuery('foo(bar_baz_metric)').functions).toEqual([]);
    expect(analyzeQuery('clamp_max(some_gauge_metric, 100)').functions).toEqual([]);
  });
});

describe('metric detection — [a-z_][a-z0-9_:]* with `_`, len>3, keyword-filtered', () => {
  it('multi-metric query: both operands detected, insertion order preserved', () => {
    const a = analyzeQuery('redis_keyspace_hits_total / redis_keyspace_misses_total');
    expect(a.metrics).toEqual(['redis_keyspace_hits_total', 'redis_keyspace_misses_total']);
  });

  it('recording-rule-style names (with `:`) are single tokens', () => {
    const a = analyzeQuery('da:node_cpu_usage:percent');
    expect(a.metrics).toEqual(['da:node_cpu_usage:percent']);
  });

  it('QUIRK Q1 function names with `_` are mis-detected as metrics', () => {
    const a = analyzeQuery('histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))');
    expect(a.functions).toEqual(['histogram_quantile', 'rate']);
    // histogram_quantile leaks into metrics (has `_`, len>3, not in metric denylist)
    expect(a.metrics).toEqual(['histogram_quantile', 'http_request_duration_seconds_bucket']);

    const b = analyzeQuery('predict_linear(some_gauge_metric[1h])');
    expect(b.functions).toEqual(['predict_linear']);
    expect(b.metrics).toEqual(['predict_linear', 'some_gauge_metric']);
  });

  it('QUIRK Q2 label VALUES with `_` are mis-detected as metrics', () => {
    const a = analyzeQuery('http_requests_total{job="api_gateway", instance=~"web-1", env!="prod"}');
    // api_gateway (the VALUE, not a metric) leaks in; job/instance/env/prod/web have no `_`
    expect(a.metrics).toEqual(['http_requests_total', 'api_gateway']);
  });
});

describe('label extraction — keys from {…}, matchers =/=~/!=', () => {
  it('collects label KEYS across =, =~, != matchers', () => {
    const a = analyzeQuery('http_requests_total{job="x", instance=~"y", env!="z"}');
    expect(a.labels).toEqual(['job', 'instance', 'env']);
  });

  it('no braces → no labels', () => {
    expect(analyzeQuery('rate(http_requests_total[5m])').labels).toEqual([]);
  });
});

describe('matchedRules — direct vs underlying, substring, dedup', () => {
  it('direct: query uses a recording-rule name', () => {
    const a = analyzeQuery('da:mariadb_replication_lag:seconds > 5');
    expect(names(a)).toEqual(['da:mariadb_replication_lag:seconds']);
    expect(types(a)).toEqual(['direct']);
    expect(a.suggestions).toEqual([]); // no 'underlying' ⇒ no suggestion
  });

  it('underlying: query uses the raw metric behind a rule → suggestion fires', () => {
    const a = analyzeQuery('mysql_global_status_threads_connected');
    expect(names(a)).toEqual(['da:mariadb_connections:current']);
    expect(types(a)).toEqual(['underlying']);
    expect(a.suggestions).toEqual([S_REC]);
  });

  it('QUIRK Q6 substring underlying match spans multiple UNRELATED rules', () => {
    // `kube_pod_container` is a substring of both the pod-restart metric and the
    // k8s-memory rule's `kube_pod_container_resource_limits` — two different concepts.
    const a = analyzeQuery('kube_pod_container');
    expect(names(a)).toEqual(['da:k8s_pod_restart:total', 'da:k8s_memory_usage:percent']);
    expect(types(a)).toEqual(['underlying', 'underlying']);
  });

  it('Q7 fix: a rule matched both ways dedups to `direct`, but the underlying match STILL yields a suggestion', () => {
    const a = analyzeQuery('da:redis_connections:current + redis_connected_clients');
    expect(names(a)).toEqual(['da:redis_connections:current']); // deduped to one (unchanged)
    expect(types(a)).toEqual(['direct']); // direct pushed before underlying ⇒ wins (unchanged)
    // Q7 FIX: the suggestion now keys off `hadUnderlyingMatch` captured BEFORE dedup, so
    // the raw metric `redis_connected_clients` (which matched as underlying) still earns the hint.
    expect(a.suggestions).toEqual([S_REC]);
  });

  it('no rule match for an unknown metric', () => {
    const a = analyzeQuery('rate(http_requests_total[5m])');
    expect(a.matchedRules).toEqual([]);
  });
});

describe('warnings', () => {
  it('rate() without a range → rate-needs-range warning', () => {
    const a = analyzeQuery('rate(http_requests_total)');
    expect(a.functions).toEqual(['rate']);
    expect(a.duration).toBeNull();
    expect(a.warnings).toEqual([W_RATE]);
  });

  it('Q5-fix precision: a `[…]` in a label value (no time UNIT) still warns — both `[abc]` and `[404]`', () => {
    // The Q5 guard is `!/\[\d[\d.]*[smhdwy]/` (digit(s) + a time UNIT), NOT a naive
    // `!/\[/` or `!/\[\d/`: a bracket in a label value has no range semantics, so rate()
    // genuinely lacks a range and MUST still warn. `[abc]` has no digit; `[404]` has a
    // digit but NO unit — the `[404]` case is the CodeRabbit-caught regression that a
    // bare `\[\d` would have wrongly suppressed. Both must warn.
    expect(analyzeQuery('rate(http_requests_total{path="[abc]"})').warnings).toEqual([W_RATE]);
    expect(analyzeQuery('rate(http_requests_total{path="[404]"})').warnings).toEqual([W_RATE]);
  });

  it('irate with a > 5m window → long-window warning', () => {
    const a = analyzeQuery('irate(http_requests_total[10m])');
    expect(a.warnings).toEqual([W_IRATE]);
    // `\brate\(` does not match inside `irate(` ⇒ no spurious rate warning
  });

  it('irate with a ≤ 5m window → no warning', () => {
    expect(analyzeQuery('irate(http_requests_total[5m])').warnings).toEqual([]);
  });

  it('QUIRK Q8 irate long-window warning only fires for minutes: [1h]/[300s] never warn', () => {
    expect(analyzeQuery('irate(http_requests_total[1h])').warnings).toEqual([]);
    expect(analyzeQuery('irate(http_requests_total[300s])').warnings).toEqual([]);
    expect(analyzeQuery('irate(http_requests_total[6m])').warnings).toEqual([W_IRATE]);
  });
});

describe('simulateResults', () => {
  it('raw metric in MOCK_SERIES → the raw series', () => {
    expect(simulateResults('mysql_global_status_threads_connected')).toEqual(
      [120, 135, 142, 128, 150, 165, 148, 130, 125, 118],
    );
  });

  it('rate() over a MOCK metric → per-15s deltas as toFixed(4) strings, first point dropped', () => {
    const r = simulateResults('rate(mysql_global_status_threads_connected[5m])') as string[];
    expect(r).toHaveLength(9); // .map(...).slice(1)
    expect(typeof r[0]).toBe('string');
    expect(r[0]).toBe('1.0000'); // (135-120)/15 = 1
    expect(r[1]).toBe('0.4667'); // (142-135)/15 = 0.46666…
    expect(r[2]).toBe('-0.9333'); // (128-142)/15 = -0.93333…
  });

  it('metric with no MOCK series and no matching rule → null', () => {
    expect(simulateResults('http_requests_total')).toBeNull();
  });

  it('recording-rule name whose expr wraps a MOCK metric → the underlying series', () => {
    // da:mariadb_connections:current.expr === mysql_global_status_threads_connected
    expect(simulateResults('da:mariadb_connections:current')).toEqual(
      [120, 135, 142, 128, 150, 165, 148, 130, 125, 118],
    );
  });
});

describe('data constants', () => {
  it('RECORDING_RULES / MOCK_SERIES shapes are intact after extraction', () => {
    expect(RECORDING_RULES).toHaveLength(22);
    expect(Object.keys(MOCK_SERIES)).toHaveLength(7);
    // every rule carries the 4 fields the JSX render + matcher rely on
    for (const r of RECORDING_RULES) {
      expect(r).toHaveProperty('pack');
      expect(r).toHaveProperty('rule');
      expect(r).toHaveProperty('expr');
      expect(r).toHaveProperty('desc');
    }
  });
});
