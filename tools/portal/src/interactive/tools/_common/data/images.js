---
title: "_common — Container image ref accessor"
purpose: |
  Single source of truth for the container image refs the in-browser
  tools hand to a customer (today: the Deployment Profile Wizard's
  generated values.yaml).

  Layered fallback at module-eval time — same shape as rule-packs.js:
    1. window.__PLATFORM_DATA.images — if `make platform-data`
       generated assets/platform-data.json and jsx-loader pre-fetched
       it (production deployment path)
    2. inline mirror below — last-resort offline / standalone bundle

  WHY THIS MODULE EXISTS (#1245): the wizard carried these as literals
  and they rotted to a v2.7.0-era snapshot that nobody noticed —
  prom/prometheus v2.52.0 against a shipped v3.13.1 (a whole MAJOR,
  and 2.x predates the 3.0 left-open lookback our rule-pack fixtures
  are calibrated against), alertmanager v0.27.0 vs v0.33.1,
  `jimmidyson/configmap-reload` months after that component was
  replaced outright, and `oauth2-proxy/oauth2-proxy:v7.6.0` — a ref
  missing its `quay.io/` registry, so it does not exist on Docker Hub
  and the emitted YAML ImagePullBackOffs. Nothing scanned it: the
  image-ref extractor globs helm values + k8s manifests only, so a JS
  generator was outside every detection surface.

  TAG ONLY, never a digest. This is a customer's STARTING values.yaml:
  a digest would freeze it at the instant we generated it and — worse —
  silently defeat their own `--set image.tag=` bump, because the digest
  wins. That is the footgun helm/*/values.yaml carries a ⛔ warning
  about. Our own supply-chain digest pinning (#902 L2) stays where it
  belongs: on what WE deploy.

  The inline mirror is hand-maintained; images-fallback-drift.test.ts
  fails on any divergence from platform-data.json.

  Public API:
    PLATFORM_IMAGES           map of logical key to "repo:tag"
    imageRepository(key)      repo half (for `image.repository:` in YAML)
    imageTag(key)             tag half (for `image.tag:` in YAML)
---

// The inline mirror is the FLOOR, not an either/or alternative: live data
// overrides PER KEY rather than wholesale. `A || B` would let any truthy live
// object — including a PARTIAL one — shadow the mirror entirely, and a missing
// key would then render as an empty `repository:` / `tag:` in the customer's
// values.yaml. That is a live scenario, not a hypothetical: da-portal ships
// docs/assets/platform-data.json inside its image, so a portal deployed one
// release behind a newly added wizard key would serve exactly such a partial
// map. Merging per key keeps the output complete no matter how stale the
// deployed data is.
const IMAGE_MIRROR = {
  prometheus: 'prom/prometheus:v3.13.1',
  alertmanager: 'prom/alertmanager:v0.33.1',
  configReloader: 'quay.io/prometheus-operator/prometheus-config-reloader:v0.92.1',
  oauth2Proxy: 'quay.io/oauth2-proxy/oauth2-proxy:v7.15.3',
  thresholdExporter: 'ghcr.io/vencil/threshold-exporter:v2.9.0',
  daPortal: 'ghcr.io/vencil/da-portal:v2.9.0',
  tenantApi: 'ghcr.io/vencil/tenant-api:v2.7.0',
};

const PLATFORM_IMAGES = { ...IMAGE_MIRROR, ...(window.__PLATFORM_DATA?.images || {}) };

// Split on the LAST colon: a ref may carry a registry port
// (registry.example.com:5000/foo:v1) and only the trailing segment is the tag.
function _split(key) {
  const ref = PLATFORM_IMAGES[key];
  // An unknown key is a CODING error (typo'd accessor / renamed image), not a
  // data condition — the merge above guarantees every mirrored key resolves.
  // Returning '' here would emit a blank `repository:` into the customer's
  // values.yaml and look like it worked, so refuse loudly instead.
  if (!ref) {
    throw new Error(
      `images.js: unknown image key '${key}'. Known keys: ${Object.keys(PLATFORM_IMAGES).join(', ')}`,
    );
  }
  const i = ref.lastIndexOf(':');
  if (i < 0 || ref.indexOf('/', i) !== -1) return { repository: ref, tag: '' };
  return { repository: ref.slice(0, i), tag: ref.slice(i + 1) };
}

function imageRepository(key) { return _split(key).repository; }
function imageTag(key) { return _split(key).tag; }

export { PLATFORM_IMAGES, imageRepository, imageTag };
