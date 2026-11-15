---
title: "_common — Helm chart version accessor"
purpose: |
  The chart versions the in-browser tools pin in a `helm upgrade --install`
  they hand to a customer (today: the Deployment Profile Wizard).

  WHY THIS MODULE EXISTS (#2947): the wizard's install command carried no
  `--version`, so the customer installed whatever chart the registry held
  as newest on the day they pasted it, next to a values file written
  against one particular chart. A chart ref without a version is the
  chart-shaped `:latest`.

  Layered at module-eval time, same shape as images.js:
    1. window.__PLATFORM_DATA.charts — the `charts` block of
       platform-data.json
    2. inline mirror below — offline / standalone bundle

  These are PUBLISHED chart versions, not the tree's Chart.yaml: a chart's
  version moves with feature PRs between releases, and `--version` of a
  chart no registry holds fails outright. The block is rewritten from
  Chart.yaml only at release wrap-up (`make platform-data
  RELEASE_CHARTS=1`); `make pre-tag` refuses a pin nothing publishes.

  The inline mirror is hand-maintained; charts-fallback-drift.test.ts
  fails on any divergence from platform-data.json.

  Public API:
    PLATFORM_CHARTS           map of chart name to exact version
    chartVersion(chart)       the version to pass to `--version`
---

// The mirror is the floor and live data overrides PER KEY (see images.js for
// why `A || B` is the wrong shape: a partial live map would drop a chart and
// the install command would lose its pin).
const CHART_MIRROR = {
  'threshold-exporter': '2.9.0',
  'tenant-api': '2.9.8',
};

const PLATFORM_CHARTS = { ...CHART_MIRROR, ...(window.__PLATFORM_DATA?.charts || {}) };

// One exact release. A range (`^2.9.0`, `2.x`), `latest` or an empty string
// is a constraint helm resolves at run time: floating, spelled differently.
const EXACT = /^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$/;

function chartVersion(chart) {
  const version = PLATFORM_CHARTS[chart];
  // Refuse rather than emit: an install command with a blank or ranged
  // `--version` looks pinned and is not.
  if (!EXACT.test(version)) {
    throw new Error(
      `charts.js: no exact version for chart '${chart}' (got ${JSON.stringify(version)}). ` +
      `Known charts: ${Object.keys(PLATFORM_CHARTS).join(', ')}`,
    );
  }
  return version;
}

export { PLATFORM_CHARTS, chartVersion };
