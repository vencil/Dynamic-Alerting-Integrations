---
title: "Portal Shared Module"
tags: [self-service, shared, internal]
audience: ["platform-engineer"]
version: v2.7.0
lang: en
dependencies: [
  "_common/data/rule-packs.js",
  "_common/data/routing-profiles.js",
  "_common/validation/constants.js",
  "_common/validation/yaml-parser.js",
  "_common/sim/alert-engine.js"
]
---

import React, { useState, useMemo, useEffect, useRef } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── PR-portal-9: shim conversion (590 → ~190 LOC) ───────────────────
 *
 * Pre-PR-portal-3 (#205) this file owned the canonical Rule Pack
 * catalog, validation constants, routing profiles, YAML parser, and
 * alert simulation engine inline (~440 LOC of data + functions).
 * PR-portal-3 promoted those into 5 modules under _common/data/,
 * _common/validation/, _common/sim/ — but kept portal-shared.jsx
 * unchanged so the 4 existing consumers (self-service-portal +
 * AlertPreviewTab + RoutingTraceTab + YamlValidatorTab) didn't have
 * to migrate at the same time. Result: the data/functions lived in
 * BOTH places, with drift risk.
 *
 * This PR (PR-portal-9) flips portal-shared.jsx into a thin BC
 * re-export wrapper:
 *
 *   1. front-matter `dependencies:` block loads the 5 _common modules
 *   2. each canonical symbol is picked up off `window.__X`
 *   3. re-bundled into `window.__portalShared` with the EXACT same
 *      shape the 4 consumers destructure today → zero consumer
 *      changes required
 *   4. the 2 React UI components (MetricAutocomplete + RulePackSelector)
 *      stay inline because they are alert-builder-tab UI only, not
 *      generic enough to live in _common/components/
 *
 * Triggers `.sed-damage-allowlist` because the file shrinks 590 →
 * ~190 LOC (~67%, above the 50% sed-damage-guard threshold). The
 * allowlist entry was added in this same commit with an explanatory
 * comment.
 * ──────────────────────────────────────────────────────────────────── */

// Data + validation + sim — all loaded via dependencies above.
const RULE_PACK_DATA = window.__RULE_PACK_DATA;
const CATEGORY_LABELS = window.__CATEGORY_LABELS;
const getAllMetricKeys = window.__getAllMetricKeys;

const RESERVED_KEYS = window.__RESERVED_KEYS;
const RESERVED_PREFIXES = window.__RESERVED_PREFIXES;
const RECEIVER_TYPES = window.__RECEIVER_TYPES;
const RECEIVER_REQUIRED = window.__RECEIVER_REQUIRED;
const TIMING_GUARDRAILS = window.__TIMING_GUARDRAILS;
const UNSAFE_KEYS = window.__UNSAFE_KEYS;
const MAX_YAML_SIZE = window.__MAX_YAML_SIZE;

const ROUTING_DEFAULTS = window.__ROUTING_DEFAULTS;
const ROUTING_PROFILES = window.__ROUTING_PROFILES;
const DOMAIN_POLICIES = window.__DOMAIN_POLICIES;

const parseDuration = window.__parseDuration;
const parseYaml = window.__parseYaml;
const generateSampleYaml = window.__generateSampleYaml;
const validateConfig = window.__validateConfig;
const simulateAlerts = window.__simulateAlerts;
const resolveRoutingLayers = window.__resolveRoutingLayers;

/* ── Metric key autocomplete dropdown ── */
/* Kept inline: alert-builder-tab UI only, not generic enough for
 * _common/components/. */
function MetricAutocomplete({ allMetrics, onInsert }) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  const filtered = useMemo(() => {
    if (!query) return allMetrics.slice(0, 15);
    const q = query.toLowerCase();
    return allMetrics.filter(m =>
      m.key.toLowerCase().includes(q) || m.label.toLowerCase().includes(q)
    ).slice(0, 15);
  }, [query, allMetrics]);

  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <div ref={ref} className="relative">
      <div className="flex gap-2 items-center">
        <input
          type="text"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          placeholder={t('搜尋 metric key...', 'Search metric key...')}
          className="flex-1 text-sm px-3 py-1.5 border rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
        />
      </div>
      {open && filtered.length > 0 && (
        <div className="absolute z-10 w-full mt-1 bg-white border rounded-lg shadow-lg max-h-48 overflow-y-auto">
          {filtered.map((m, i) => (
            <button
              key={i}
              onClick={() => { onInsert(m); setQuery(''); setOpen(false); }}
              className="w-full text-left px-3 py-2 hover:bg-blue-50 text-sm border-b border-gray-50 last:border-0"
            >
              <code className="font-mono text-blue-700">{m.key}</code>
              <span className="ml-2 text-gray-400 text-xs">{m.label}</span>
              {m.desc && <span className="ml-1 text-gray-400 text-xs">— {m.desc}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Rule Pack multi-select ── */
/* Kept inline alongside MetricAutocomplete — same alert-builder-tab
 * scope; closure over RULE_PACK_DATA + CATEGORY_LABELS picked up off
 * window above. */
function RulePackSelector({ selected, onChange }) {
  const grouped = useMemo(() => {
    const groups = {};
    for (const [id, pack] of Object.entries(RULE_PACK_DATA)) {
      const cat = pack.category || 'other';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push({ id, ...pack });
    }
    return groups;
  }, []);

  const toggle = (id) => {
    if (RULE_PACK_DATA[id]?.required) return;
    const next = selected.includes(id)
      ? selected.filter(x => x !== id)
      : [...selected, id];
    onChange(next);
  };

  return (
    <div className="space-y-2">
      {Object.entries(grouped).map(([cat, packs]) => (
        <div key={cat}>
          <div className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">
            {CATEGORY_LABELS[cat] ? CATEGORY_LABELS[cat]() : cat}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {packs.map(p => {
              const isSelected = selected.includes(p.id);
              const isRequired = p.required;
              return (
                <button
                  key={p.id}
                  onClick={() => toggle(p.id)}
                  disabled={isRequired}
                  className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
                    isRequired
                      ? 'bg-gray-100 text-gray-400 border-gray-200 cursor-not-allowed'
                      : isSelected
                        ? 'bg-blue-100 text-blue-800 border-blue-300 hover:bg-blue-200'
                        : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'
                  }`}
                  title={isRequired ? t('必選（自動啟用）', 'Required (auto-enabled)') : ''}
                >
                  {isSelected && !isRequired && <span className="mr-1">&#10003;</span>}
                  {isRequired && <span className="mr-1">&#128274;</span>}
                  {p.label}
                  {p.defaults && Object.keys(p.defaults).length > 0 && (
                    <span className="ml-1 text-gray-400">({Object.keys(p.defaults).length})</span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── BC re-bundle ──────────────────────────────────────────────────
 * Identical shape to the pre-PR-portal-9 export. The 4 existing
 * consumers (self-service-portal + AlertPreviewTab + RoutingTraceTab
 * + YamlValidatorTab) destructure from `window.__portalShared` and
 * need NO changes. Each new tool should pull individual symbols
 * directly from the underlying _common/ modules instead. */
window.__portalShared = {
  // Data constants
  RULE_PACK_DATA,
  CATEGORY_LABELS,
  RESERVED_KEYS,
  RESERVED_PREFIXES,
  RECEIVER_TYPES,
  ROUTING_PROFILES,
  DOMAIN_POLICIES,
  RECEIVER_REQUIRED,
  TIMING_GUARDRAILS,
  ROUTING_DEFAULTS,
  UNSAFE_KEYS,
  MAX_YAML_SIZE,
  // Utility functions
  parseDuration,
  parseYaml,
  getAllMetricKeys,
  generateSampleYaml,
  validateConfig,
  simulateAlerts,
  resolveRoutingLayers,
  // React components
  MetricAutocomplete,
  RulePackSelector,
};
