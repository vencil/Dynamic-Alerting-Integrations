---
title: "Dependency Graph"
tags: [graph, dependencies, visual]
audience: [platform-engineer, domain-expert]
version: v2.6.0
lang: en
related: [rule-pack-matrix, rule-pack-selector, capacity-planner]
---

import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';

const t = window.__t || ((zh, en) => en);

// --- Shared platform data (from platform-data.json via jsx-loader) ---
const __PD = window.__PLATFORM_DATA || {};

// Category mapping for dependency graph (uses broader categories for visual grouping)
const DEP_GRAPH_CAT = { database: 'database', messaging: 'middleware', runtime: 'runtime', webserver: 'middleware', infrastructure: 'infra' };

const PACKS = (() => {
  if (__PD.rulePacks) {
    const packs = Object.entries(__PD.rulePacks).map(([key, p]) => ({
      id: key,
      label: p.label,
      category: DEP_GRAPH_CAT[p.category] || p.category,
      exporter: p.exporter,
      metrics: (p.metrics || []).length,
      alerts: p.alertRules,
    }));
    // Add non-registry conceptual packs for the dependency graph
    if (!__PD.rulePacks.node) packs.push({ id: 'node', label: 'Node Exporter', category: 'infra', exporter: 'node_exporter', metrics: 25, alerts: 12 });
    if (!__PD.rulePacks.etcd) packs.push({ id: 'etcd', label: 'etcd', category: 'infra', exporter: 'etcd', metrics: 10, alerts: 6 });
    if (!__PD.rulePacks.coredns) packs.push({ id: 'coredns', label: 'CoreDNS', category: 'infra', exporter: 'coredns', metrics: 8, alerts: 5 });
    if (!__PD.rulePacks.blackbox) packs.push({ id: 'blackbox', label: 'Blackbox', category: 'infra', exporter: 'blackbox_exporter', metrics: 6, alerts: 4 });
    if (!__PD.rulePacks.custom) packs.push({ id: 'custom', label: 'Custom', category: 'custom', exporter: 'user-defined', metrics: 10, alerts: 5 });
    return packs;
  }
  // Fallback
  return [
    { id: 'kubernetes', label: 'Kubernetes', category: 'infra', exporter: 'kube-state-metrics', metrics: 4, alerts: 4 },
    { id: 'node', label: 'Node Exporter', category: 'infra', exporter: 'node_exporter', metrics: 25, alerts: 12 },
    { id: 'etcd', label: 'etcd', category: 'infra', exporter: 'etcd', metrics: 10, alerts: 6 },
    { id: 'coredns', label: 'CoreDNS', category: 'infra', exporter: 'coredns', metrics: 8, alerts: 5 },
    { id: 'mariadb', label: 'MariaDB', category: 'database', exporter: 'mysqld_exporter', metrics: 6, alerts: 8 },
    { id: 'postgresql', label: 'PostgreSQL', category: 'database', exporter: 'postgres_exporter', metrics: 5, alerts: 9 },
    { id: 'redis', label: 'Redis', category: 'database', exporter: 'redis_exporter', metrics: 4, alerts: 6 },
    { id: 'mongodb', label: 'MongoDB', category: 'database', exporter: 'mongodb_exporter', metrics: 4, alerts: 6 },
    { id: 'kafka', label: 'Kafka', category: 'middleware', exporter: 'kafka_exporter', metrics: 5, alerts: 9 },
    { id: 'elasticsearch', label: 'Elasticsearch', category: 'middleware', exporter: 'elasticsearch_exporter', metrics: 4, alerts: 7 },
    { id: 'rabbitmq', label: 'RabbitMQ', category: 'middleware', exporter: 'rabbitmq_exporter', metrics: 5, alerts: 8 },
    { id: 'nginx', label: 'Nginx', category: 'middleware', exporter: 'nginx-prometheus-exporter', metrics: 3, alerts: 6 },
    { id: 'jvm', label: 'JVM', category: 'runtime', exporter: 'jmx_exporter', metrics: 4, alerts: 7 },
    { id: 'blackbox', label: 'Blackbox', category: 'infra', exporter: 'blackbox_exporter', metrics: 6, alerts: 4 },
    { id: 'custom', label: 'Custom', category: 'custom', exporter: 'user-defined', metrics: 10, alerts: 5 },
  ];
})();

const EDGES = [
  { from: 'mariadb', to: 'kubernetes', type: 'suggests', reason: t('容器資源監控補充 DB 監控', 'Container resource monitoring complements DB monitoring') },
  { from: 'postgresql', to: 'kubernetes', type: 'suggests', reason: t('容器資源監控', 'Container resource monitoring') },
  { from: 'redis', to: 'kubernetes', type: 'suggests', reason: t('容器資源監控', 'Container resource monitoring') },
  { from: 'mongodb', to: 'kubernetes', type: 'suggests', reason: t('容器資源監控', 'Container resource monitoring') },
  { from: 'kafka', to: 'kubernetes', type: 'suggests', reason: t('Kafka broker 容器監控', 'Kafka broker container monitoring') },
  { from: 'kafka', to: 'jvm', type: 'requires', reason: t('Kafka 運行在 JVM 上', 'Kafka runs on JVM') },
  { from: 'elasticsearch', to: 'jvm', type: 'requires', reason: t('ES 運行在 JVM 上', 'ES runs on JVM') },
  { from: 'elasticsearch', to: 'kubernetes', type: 'suggests', reason: t('容器資源監控', 'Container resource monitoring') },
  { from: 'kubernetes', to: 'node', type: 'suggests', reason: t('節點層監控補充 Pod 監控', 'Node-level monitoring complements pod monitoring') },
  { from: 'etcd', to: 'kubernetes', type: 'requires', reason: t('etcd 是 K8s 核心元件', 'etcd is a core K8s component') },
  { from: 'coredns', to: 'kubernetes', type: 'requires', reason: t('CoreDNS 是 K8s DNS 服務', 'CoreDNS is K8s DNS service') },
  { from: 'rabbitmq', to: 'kubernetes', type: 'suggests', reason: t('容器資源監控', 'Container resource monitoring') },
  { from: 'nginx', to: 'kubernetes', type: 'suggests', reason: t('Ingress controller 容器監控', 'Ingress controller container monitoring') },
  { from: 'nginx', to: 'blackbox', type: 'suggests', reason: t('HTTP 端點探測補充內部 metrics', 'HTTP endpoint probing complements internal metrics') },
];

const CATEGORY_COLORS = {
  infra: { bg: '#3b82f6', light: '#dbeafe', text: '#1e40af' },
  database: { bg: '#10b981', light: '#d1fae5', text: '#065f46' },
  middleware: { bg: '#f59e0b', light: '#fef3c7', text: '#92400e' },
  runtime: { bg: '#8b5cf6', light: '#ede9fe', text: '#5b21b6' },
  custom: { bg: '#6b7280', light: '#f3f4f6', text: '#374151' },
};

/* ── Layout: circular ── */
function computeLayout(packs) {
  const cx = 400, cy = 300, rx = 300, ry = 220;
  const positions = {};
  packs.forEach((p, i) => {
    const angle = (2 * Math.PI * i) / packs.length - Math.PI / 2;
    positions[p.id] = { x: cx + rx * Math.cos(angle), y: cy + ry * Math.sin(angle) };
  });
  return positions;
}

export default function DependencyGraph() {
  const [selected, setSelected] = useState(null);
  const [hoveredEdge, setHoveredEdge] = useState(null);
  const [showType, setShowType] = useState('all'); // all, requires, suggests

  const positions = useMemo(() => computeLayout(PACKS), []);

  const filteredEdges = useMemo(() => {
    if (showType === 'all') return EDGES;
    return EDGES.filter(e => e.type === showType);
  }, [showType]);

  const selectedPack = selected ? PACKS.find(p => p.id === selected) : null;
  const relatedEdges = selected ? EDGES.filter(e => e.from === selected || e.to === selected) : [];
  const relatedIds = new Set(relatedEdges.flatMap(e => [e.from, e.to]));

  const svgW = 800, svgH = 600;
  const svgStyle = { minHeight: '400px' };
  const pointerStyle = { cursor: 'pointer' };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-6xl mx-auto">
        <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('依賴關係圖', 'Dependency Graph')}</h1>
        <p className="text-slate-600 mb-6">{t('視覺化 Rule Pack 之間的依賴和建議搭配關係', 'Visualize dependencies and suggested pairings between Rule Packs')}</p>

        {/* Filter */}
        <div className="flex items-center gap-3 mb-6">
          <span className="text-sm text-slate-600">{t('顯示', 'Show')}:</span>
          {[
            { v: 'all', label: t('全部', 'All') },
            { v: 'requires', label: t('必要依賴', 'Required') },
            { v: 'suggests', label: t('建議搭配', 'Suggested') },
          ].map(f => (
            <button key={f.v} onClick={() => setShowType(f.v)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium ${showType === f.v ? 'bg-blue-600 text-white' : 'bg-white border border-slate-200 text-slate-600 hover:border-blue-300'}`}>
              {f.label}
            </button>
          ))}
          {selected && (
            <button onClick={() => setSelected(null)} className="ml-auto text-xs text-slate-500 hover:text-slate-700 border border-slate-200 px-3 py-1.5 rounded-lg">
              {t('清除選取', 'Clear Selection')}
            </button>
          )}
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* SVG Graph */}
          <div className="lg:col-span-2 bg-white rounded-xl shadow-sm border border-slate-200 p-4 overflow-hidden">
            <svg viewBox={'0 0 ' + svgW + ' ' + svgH} className="w-full" style={svgStyle}>
              <defs>
                <marker id="arrow-req" viewBox="0 0 10 7" refX="10" refY="3.5" markerWidth="8" markerHeight="6" orient="auto">
                  <polygon points="0 0, 10 3.5, 0 7" fill="#ef4444" />
                </marker>
                <marker id="arrow-sug" viewBox="0 0 10 7" refX="10" refY="3.5" markerWidth="8" markerHeight="6" orient="auto">
                  <polygon points="0 0, 10 3.5, 0 7" fill="#3b82f6" />
                </marker>
              </defs>

              {/* Edges */}
              {filteredEdges.map((edge, i) => {
                const from = positions[edge.from];
                const to = positions[edge.to];
                if (!from || !to) return null;
                const isHighlighted = selected && (edge.from === selected || edge.to === selected);
                const isDimmed = selected && !isHighlighted;
                const isHovered = hoveredEdge === i;

                // Offset endpoint to not overlap node circle
                const dx = to.x - from.x, dy = to.y - from.y;
                const dist = Math.sqrt(dx * dx + dy * dy);
                const r = 28;
                const tx = to.x - (dx / dist) * r;
                const ty = to.y - (dy / dist) * r;
                const fx = from.x + (dx / dist) * r;
                const fy = from.y + (dy / dist) * r;

                return (
                  <g key={i}
                    onMouseEnter={() => setHoveredEdge(i)}
                    onMouseLeave={() => setHoveredEdge(null)}
                    style={pointerStyle} className="cursor-pointer">
                    <line x1={fx} y1={fy} x2={tx} y2={ty}
                      stroke={edge.type === 'requires' ? '#ef4444' : '#3b82f6'}
                      strokeWidth={isHighlighted || isHovered ? 3 : 1.5}
                      strokeDasharray={edge.type === 'suggests' ? '6 4' : 'none'}
                      opacity={isDimmed ? 0.15 : isHighlighted || isHovered ? 1 : 0.5}
                      markerEnd={edge.type === 'requires' ? 'url(#arrow-req)' : 'url(#arrow-sug)'} />
                    {isHovered && (
                      <text x={(from.x + to.x) / 2} y={(from.y + to.y) / 2 - 8}
                        textAnchor="middle" fontSize="10" fill="#475569"
                        className="pointer-events-none">
                        {edge.reason}
                      </text>
                    )}
                  </g>
                );
              })}

              {/* Nodes */}
              {PACKS.map(pack => {
                const pos = positions[pack.id];
                const color = CATEGORY_COLORS[pack.category];
                const isSelected = selected === pack.id;
                const isRelated = selected && relatedIds.has(pack.id);
                const isDimmed = selected && !isSelected && !isRelated;

                return (
                  <g key={pack.id}
                    onClick={() => setSelected(isSelected ? null : pack.id)}
                    style={pointerStyle} className="cursor-pointer"
                    opacity={isDimmed ? 0.25 : 1}>
                    <circle cx={pos.x} cy={pos.y} r={isSelected ? 30 : 24}
                      fill={isSelected ? color.bg : color.light}
                      stroke={color.bg} strokeWidth={isSelected ? 3 : 1.5} />
                    <text x={pos.x} y={pos.y + 1} textAnchor="middle" dominantBaseline="middle"
                      fontSize={pack.label.length > 10 ? 9 : 10} fontWeight="bold"
                      fill={isSelected ? '#fff' : color.text}>
                      {pack.label}
                    </text>
                    <text x={pos.x} y={pos.y + 42} textAnchor="middle" fontSize="9" fill="#94a3b8">
                      {pack.alerts}a / {pack.metrics}m
                    </text>
                  </g>
                );
              })}
            </svg>
          </div>

          {/* Detail panel */}
          <div className="space-y-4">
            {/* Legend */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
              <h3 className="text-xs font-semibold text-slate-500 uppercase mb-3">{t('圖例', 'Legend')}</h3>
              <div className="space-y-2 text-xs">
                {Object.entries(CATEGORY_COLORS).map(([cat, c]) => (
                  <div key={cat} className="flex items-center gap-2">
                    <span className="w-3 h-3 rounded-full" style={({ backgroundColor: c.bg })}></span>
                    <span className="capitalize text-slate-700">{cat}</span>
                  </div>
                ))}
                <hr className="my-2" />
                <div className="flex items-center gap-2">
                  <span className="w-8 h-0 border-t-2 border-red-500" />
                  <span className="text-slate-600">{t('必要依賴', 'Required')}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="w-8 h-0 border-t-2 border-blue-500 border-dashed" />
                  <span className="text-slate-600">{t('建議搭配', 'Suggested')}</span>
                </div>
              </div>
            </div>

            {/* Selected pack detail */}
            {selectedPack ? (
              <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
                <h3 className="font-semibold text-slate-900 mb-2">{selectedPack.label}</h3>
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between"><span className="text-slate-500">{t('分類', 'Category')}</span><span className="capitalize font-medium">{selectedPack.category}</span></div>
                  <div className="flex justify-between"><span className="text-slate-500">Exporter</span><span className="font-mono text-xs">{selectedPack.exporter}</span></div>
                  <div className="flex justify-between"><span className="text-slate-500">{t('Metrics', 'Metrics')}</span><span className="font-bold">{selectedPack.metrics}</span></div>
                  <div className="flex justify-between"><span className="text-slate-500">{t('Alerts', 'Alerts')}</span><span className="font-bold">{selectedPack.alerts}</span></div>
                </div>
                {relatedEdges.length > 0 && (
                  <div className="mt-4">
                    <h4 className="text-xs font-semibold text-slate-500 uppercase mb-2">{t('關聯', 'Connections')}</h4>
                    <div className="space-y-2">
                      {relatedEdges.map((e, i) => {
                        const other = e.from === selected ? e.to : e.from;
                        const direction = e.from === selected ? '→' : '←';
                        return (
                          <div key={i} className="text-xs p-2 rounded-lg bg-slate-50">
                            <div className="flex items-center gap-1">
                              <span className={`px-1.5 py-0.5 rounded ${e.type === 'requires' ? 'bg-red-100 text-red-700' : 'bg-blue-100 text-blue-700'}`}>
                                {e.type}
                              </span>
                              <span>{direction} {other}</span>
                            </div>
                            <p className="text-slate-500 mt-1">{e.reason}</p>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-4 text-center text-sm text-slate-400">
                {t('點擊節點查看詳情', 'Click a node to see details')}
              </div>
            )}

            {/* Stats */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-4">
              <h3 className="text-xs font-semibold text-slate-500 uppercase mb-2">{t('統計', 'Stats')}</h3>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="text-slate-500">{t('Rule Pack', 'Rule Packs')}</div><div className="font-bold">{PACKS.length}</div>
                <div className="text-slate-500">{t('依賴關係', 'Dependencies')}</div><div className="font-bold">{EDGES.filter(e => e.type === 'requires').length}</div>
                <div className="text-slate-500">{t('建議搭配', 'Suggestions')}</div><div className="font-bold">{EDGES.filter(e => e.type === 'suggests').length}</div>
                <div className="text-slate-500">{t('總 Alerts', 'Total Alerts')}</div><div className="font-bold">{PACKS.reduce((s, p) => s + p.alerts, 0)}</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
