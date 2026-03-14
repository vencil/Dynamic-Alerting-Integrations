---
title: "Architecture Decision Quiz"
tags: [architecture, quiz, decision]
audience: [platform-engineer]
version: v2.0.0-preview.2
lang: en
related: [capacity-planner, dependency-graph, onboarding-checklist]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

const REPO_BASE = "https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main";

/* ── Questions ── */
const QUESTIONS = [
  {
    id: 'clusters',
    question: t('你有幾個 Kubernetes 叢集？', 'How many Kubernetes clusters do you have?'),
    options: [
      { id: 'single', label: t('單叢集', 'Single cluster'), icon: '1️⃣' },
      { id: 'multi', label: t('多叢集（2–5）', 'Multi-cluster (2–5)'), icon: '🔢' },
      { id: 'many', label: t('大規模（>5）', 'Large scale (>5)'), icon: '🌐' },
    ],
  },
  {
    id: 'prometheus',
    question: t('你已經有 Prometheus 了嗎？', 'Do you already have Prometheus?'),
    options: [
      { id: 'none', label: t('沒有，從頭開始', 'No, starting fresh'), icon: '🆕' },
      { id: 'standalone', label: t('有，獨立部署', 'Yes, standalone deployment'), icon: '📦' },
      { id: 'operator', label: t('有，用 Prometheus Operator', 'Yes, with Prometheus Operator'), icon: '⚙️' },
      { id: 'thanos', label: t('有，搭配 Thanos / Mimir', 'Yes, with Thanos / Mimir'), icon: '🏗️' },
    ],
  },
  {
    id: 'alertmanager',
    question: t('你已經有 Alertmanager 了嗎？', 'Do you already have Alertmanager?'),
    options: [
      { id: 'none', label: t('沒有', 'No'), icon: '🆕' },
      { id: 'existing', label: t('有，想整合', 'Yes, want to integrate'), icon: '🔌' },
      { id: 'shared', label: t('有，跨團隊共用', 'Yes, shared across teams'), icon: '👥' },
    ],
  },
  {
    id: 'tenants',
    question: t('你預計有多少 tenant？', 'How many tenants do you expect?'),
    options: [
      { id: 'few', label: t('1–5 個', '1–5'), icon: '📁' },
      { id: 'medium', label: t('6–20 個', '6–20'), icon: '📂' },
      { id: 'many', label: t('20+ 個', '20+'), icon: '🗄️' },
    ],
  },
  {
    id: 'gitops',
    question: t('你的部署方式？', 'How do you deploy?'),
    options: [
      { id: 'manual', label: t('手動 kubectl / Helm', 'Manual kubectl / Helm'), icon: '🖐️' },
      { id: 'cicd', label: t('CI/CD pipeline', 'CI/CD pipeline'), icon: '🔄' },
      { id: 'gitops', label: t('GitOps（ArgoCD / Flux）', 'GitOps (ArgoCD / Flux)'), icon: '🔀' },
    ],
  },
  {
    id: 'routing',
    question: t('告警通知需求？', 'Alert notification needs?'),
    options: [
      { id: 'simple', label: t('統一發到一個頻道', 'All to one channel'), icon: '📢' },
      { id: 'per-tenant', label: t('每個 tenant 各自頻道', 'Per-tenant channels'), icon: '📬' },
      { id: 'dual', label: t('NOC + tenant 雙軌', 'NOC + tenant dual track'), icon: '📡' },
    ],
  },
  {
    id: 'migration',
    question: t('你有既有的告警規則嗎？', 'Do you have existing alert rules?'),
    options: [
      { id: 'none', label: t('沒有，全新開始', 'No, starting from scratch'), icon: '✨' },
      { id: 'few', label: t('有一些，手動遷移即可', 'A few, manual migration is fine'), icon: '📋' },
      { id: 'many', label: t('大量規則需要自動遷移', 'Many rules need automated migration'), icon: '🤖' },
    ],
  },
];

/* ── Architecture patterns ── */
const ARCHITECTURES = [
  {
    id: 'standalone-simple',
    name: t('單叢集簡易部署', 'Single-Cluster Simple'),
    desc: t('最小化部署：一個 Prometheus + Alertmanager + threshold-exporter，適合小規模快速開始', 'Minimal deployment: one Prometheus + Alertmanager + threshold-exporter, ideal for small-scale quick start'),
    diagram: '[ Prometheus ] → [ threshold-exporter ] → [ Alertmanager ] → [ Slack ]',
    docs: [
      { name: 'Platform Engineer Quick Start', path: 'docs/getting-started/for-platform-engineers.md' },
      { name: 'Architecture & Design', path: 'docs/architecture-and-design.md' },
    ],
    adrs: [],
    tools: ['Rule Pack Selector', 'YAML Playground'],
  },
  {
    id: 'standalone-byo',
    name: t('單叢集 BYO 整合', 'Single-Cluster BYO Integration'),
    desc: t('自帶 Prometheus 和/或 Alertmanager，透過 Projected Volume 和 configmap-reload 整合', 'Bring your own Prometheus and/or Alertmanager, integrate via Projected Volume and configmap-reload'),
    diagram: '[ Existing Prom ] ← Projected Vol ← [ Rule Packs ]\n[ Existing AM ] ← configmap-reload ← [ Generated Routes ]',
    docs: [
      { name: 'BYO Prometheus Integration', path: 'docs/byo-prometheus-integration.md' },
      { name: 'BYO Alertmanager Integration', path: 'docs/byo-alertmanager-integration.md' },
      { name: 'ADR-005: Projected Volume', path: 'docs/adr/005-projected-volume-for-rule-packs.en.md' },
    ],
    adrs: ['005'],
    tools: ['Config Lint', 'Capacity Planner'],
  },
  {
    id: 'multi-cluster-federation',
    name: t('多叢集 Federation（場景 A）', 'Multi-Cluster Federation (Scenario A)'),
    desc: t('中央 exporter + 邊緣 Prometheus：每個叢集有本地 Prometheus，中央彙整告警', 'Central exporter + edge Prometheus: each cluster has local Prometheus, central aggregation'),
    diagram: '[ Edge Prom 1 ] ──┐\n[ Edge Prom 2 ] ──┤→ [ Central Exporter ] → [ Central AM ]\n[ Edge Prom N ] ──┘',
    docs: [
      { name: 'Federation Integration Guide', path: 'docs/federation-integration.md' },
      { name: 'Multi-Cluster Scenarios', path: 'docs/scenarios/multi-cluster-federation.md' },
      { name: 'ADR-004: Federation Scenario A First', path: 'docs/adr/004-federation-scenario-a-first.en.md' },
    ],
    adrs: ['004'],
    tools: ['Capacity Planner', 'Dependency Graph'],
  },
  {
    id: 'gitops-pipeline',
    name: t('GitOps 驅動部署', 'GitOps-Driven Deployment'),
    desc: t('配置變更經 Git PR → CI 驗證 → ArgoCD/Flux 同步，config_diff 檢查 drift', 'Config changes via Git PR → CI validation → ArgoCD/Flux sync, config_diff checks for drift'),
    diagram: '[ Git Repo ] → [ CI: validate + diff ] → [ ArgoCD/Flux ] → [ K8s ]',
    docs: [
      { name: 'GitOps Deployment Guide', path: 'docs/gitops-deployment.md' },
      { name: 'Custom Rule Governance', path: 'docs/custom-rule-governance.md' },
      { name: 'Governance & Security', path: 'docs/governance-security.md' },
    ],
    adrs: [],
    tools: ['Config Diff Viewer', 'Config Lint'],
  },
  {
    id: 'dual-routing',
    name: t('雙軌通知架構', 'Dual-Track Notification'),
    desc: t('平台強制 NOC 收到所有告警（platform_summary），tenant 收到自己的告警（summary），互不干擾', 'Platform-enforced: NOC gets all alerts (platform_summary), tenants get their own (summary)'),
    diagram: '[ Alert ] ──→ [ NOC Channel: platform_summary ]\n           └→ [ Tenant Channel: summary ]',
    docs: [
      { name: 'Alert Routing Split', path: 'docs/scenarios/alert-routing-split.md' },
      { name: 'Architecture & Design §2.11', path: 'docs/architecture-and-design.md' },
    ],
    adrs: [],
    tools: ['Alert Simulator', 'Runbook Viewer'],
  },
  {
    id: 'migration-shadow',
    name: t('影子監控遷移', 'Shadow Monitoring Migration'),
    desc: t('新舊規則並行運行，自動比對收斂度，確認一致後一鍵切換', 'Old and new rules run in parallel, auto-compare convergence, one-click cutover when aligned'),
    diagram: '[ Old Rules ] ──→ compare ←── [ New DA Rules ]\n                    ↓\n          [ Convergence Report ] → [ Cutover ]',
    docs: [
      { name: 'Shadow Monitoring SOP', path: 'docs/shadow-monitoring-sop.md' },
      { name: 'Shadow Monitoring Cutover', path: 'docs/scenarios/shadow-monitoring-cutover.md' },
      { name: 'Migration Engine Guide', path: 'docs/migration-engine.md' },
    ],
    adrs: [],
    tools: ['Migration Simulator', 'Alert Timeline Replay'],
  },
];

/* ── Scoring engine ── */
function scoreArchitectures(answers) {
  const scores = {};
  ARCHITECTURES.forEach(a => { scores[a.id] = 0; });

  const ans = answers;

  // Cluster count
  if (ans.clusters === 'single') { scores['standalone-simple'] += 3; scores['standalone-byo'] += 3; }
  if (ans.clusters === 'multi' || ans.clusters === 'many') { scores['multi-cluster-federation'] += 5; }

  // Prometheus
  if (ans.prometheus === 'none') { scores['standalone-simple'] += 3; }
  if (ans.prometheus === 'standalone' || ans.prometheus === 'operator') { scores['standalone-byo'] += 4; }
  if (ans.prometheus === 'thanos') { scores['standalone-byo'] += 2; scores['multi-cluster-federation'] += 3; }

  // Alertmanager
  if (ans.alertmanager === 'none') { scores['standalone-simple'] += 2; }
  if (ans.alertmanager === 'existing' || ans.alertmanager === 'shared') { scores['standalone-byo'] += 3; }

  // Tenants
  if (ans.tenants === 'few') { scores['standalone-simple'] += 2; }
  if (ans.tenants === 'medium' || ans.tenants === 'many') { scores['dual-routing'] += 3; scores['gitops-pipeline'] += 2; }

  // GitOps
  if (ans.gitops === 'gitops') { scores['gitops-pipeline'] += 5; }
  if (ans.gitops === 'cicd') { scores['gitops-pipeline'] += 3; }

  // Routing
  if (ans.routing === 'dual') { scores['dual-routing'] += 5; }
  if (ans.routing === 'per-tenant') { scores['dual-routing'] += 2; }

  // Migration
  if (ans.migration === 'many') { scores['migration-shadow'] += 5; }
  if (ans.migration === 'few') { scores['migration-shadow'] += 2; }

  return Object.entries(scores)
    .map(([id, score]) => ({ ...ARCHITECTURES.find(a => a.id === id), score }))
    .filter(a => a.score > 0)
    .sort((a, b) => b.score - a.score);
}

export default function ArchitectureQuiz() {
  const [answers, setAnswers] = useState({});
  const [currentQ, setCurrentQ] = useState(0);
  const [showResults, setShowResults] = useState(false);

  const progress = Object.keys(answers).length;
  const totalQ = QUESTIONS.length;

  const handleAnswer = (qId, optionId) => {
    const next = { ...answers, [qId]: optionId };
    setAnswers(next);
    if (currentQ < totalQ - 1) {
      setTimeout(() => setCurrentQ(currentQ + 1), 300);
    }
  };

  const results = useMemo(() => {
    if (progress < totalQ) return [];
    return scoreArchitectures(answers);
  }, [answers, progress]);

  const handleShowResults = () => {
    setShowResults(true);
  };

  const handleReset = () => {
    setAnswers({});
    setCurrentQ(0);
    setShowResults(false);
  };

  const question = QUESTIONS[currentQ];
  const allAnswered = progress === totalQ;

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-3xl mx-auto">
        <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('架構決策問答', 'Architecture Decision Quiz')}</h1>
        <p className="text-slate-600 mb-8">{t('回答幾個問題，找出最適合你的部署架構和參考文件', 'Answer a few questions to find the best deployment architecture and reference docs for your situation')}</p>

        {!showResults ? (
          <>
            {/* Progress */}
            <div className="flex items-center gap-3 mb-8">
              {QUESTIONS.map((q, i) => (
                <button key={q.id} onClick={() => setCurrentQ(i)}
                  className={`w-8 h-8 rounded-full text-xs font-bold transition-all flex items-center justify-center ${
                    answers[q.id] ? 'bg-green-500 text-white' :
                    i === currentQ ? 'bg-blue-600 text-white ring-4 ring-blue-200' :
                    'bg-slate-200 text-slate-500'
                  }`}>
                  {answers[q.id] ? '✓' : i + 1}
                </button>
              ))}
              <span className="ml-auto text-sm text-slate-500">{progress}/{totalQ}</span>
            </div>

            {/* Current question */}
            <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-8 mb-6">
              <h2 className="text-lg font-semibold text-slate-900 mb-6">{question.question}</h2>
              <div className="space-y-3">
                {question.options.map(opt => {
                  const isSelected = answers[question.id] === opt.id;
                  return (
                    <button key={opt.id} onClick={() => handleAnswer(question.id, opt.id)}
                      className={`w-full text-left p-4 rounded-xl border-2 transition-all flex items-center gap-4 ${
                        isSelected ? 'border-blue-500 bg-blue-50 shadow-sm' : 'border-slate-200 hover:border-blue-300 hover:bg-slate-50'
                      }`}>
                      <span className="text-2xl">{opt.icon}</span>
                      <span className={`text-sm font-medium ${isSelected ? 'text-blue-700' : 'text-slate-700'}`}>{opt.label}</span>
                      {isSelected && <span className="ml-auto text-blue-500 font-bold">✓</span>}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Navigation */}
            <div className="flex items-center justify-between">
              <button onClick={() => setCurrentQ(Math.max(0, currentQ - 1))}
                disabled={currentQ === 0}
                className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800 disabled:opacity-30">
                ← {t('上一題', 'Previous')}
              </button>
              {allAnswered ? (
                <button onClick={handleShowResults}
                  className="px-6 py-2.5 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 shadow-sm">
                  {t('查看結果', 'See Results')} →
                </button>
              ) : (
                <button onClick={() => setCurrentQ(Math.min(totalQ - 1, currentQ + 1))}
                  disabled={currentQ === totalQ - 1}
                  className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800 disabled:opacity-30">
                  {t('下一題', 'Next')} →
                </button>
              )}
            </div>
          </>
        ) : (
          /* Results */
          <>
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-lg font-semibold text-slate-800">{t('推薦架構', 'Recommended Architectures')}</h2>
              <button onClick={handleReset} className="text-sm text-blue-600 hover:underline">{t('重新作答', 'Retake Quiz')}</button>
            </div>

            <div className="space-y-6">
              {results.map((arch, rank) => (
                <div key={arch.id} className={`bg-white rounded-xl shadow-sm border ${rank === 0 ? 'border-blue-400 ring-2 ring-blue-100' : 'border-slate-200'} overflow-hidden`}>
                  {rank === 0 && (
                    <div className="bg-blue-600 text-white text-xs font-bold px-4 py-1.5 text-center">
                      ⭐ {t('最佳匹配', 'Best Match')}
                    </div>
                  )}
                  <div className="p-6">
                    <div className="flex items-start justify-between mb-3">
                      <div>
                        <h3 className="text-lg font-bold text-slate-900">{arch.name}</h3>
                        <p className="text-sm text-slate-600 mt-1">{arch.desc}</p>
                      </div>
                      <span className="text-xs text-slate-400 font-mono bg-slate-100 px-2 py-1 rounded">
                        {t('匹配度', 'score')}: {arch.score}
                      </span>
                    </div>

                    {/* ASCII diagram */}
                    <pre className="text-xs bg-slate-900 text-green-400 px-4 py-3 rounded-lg font-mono overflow-x-auto mb-4 whitespace-pre">{arch.diagram}</pre>

                    {/* Docs */}
                    <div className="mb-3">
                      <h4 className="text-xs font-semibold text-slate-500 uppercase mb-2">{t('參考文件', 'Reference Docs')}</h4>
                      <div className="flex flex-wrap gap-2">
                        {arch.docs.map((doc, i) => (
                          <a key={i} href={`${REPO_BASE}/${doc.path}`} target="_blank" rel="noopener noreferrer"
                            className="text-xs px-3 py-1.5 bg-blue-50 text-blue-700 rounded-lg hover:bg-blue-100 transition-colors">
                            {doc.name}
                          </a>
                        ))}
                      </div>
                    </div>

                    {/* Recommended tools */}
                    <div>
                      <h4 className="text-xs font-semibold text-slate-500 uppercase mb-2">{t('推薦工具', 'Recommended Tools')}</h4>
                      <div className="flex flex-wrap gap-2">
                        {arch.tools.map((tool, i) => (
                          <span key={i} className="text-xs px-3 py-1.5 bg-purple-50 text-purple-700 rounded-lg">
                            {tool}
                          </span>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>

            {/* Your answers summary */}
            <div className="mt-8 bg-white rounded-xl shadow-sm border border-slate-200 p-6">
              <h3 className="text-sm font-semibold text-slate-800 mb-3">{t('你的回答', 'Your Answers')}</h3>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
                {QUESTIONS.map(q => {
                  const opt = q.options.find(o => o.id === answers[q.id]);
                  return (
                    <div key={q.id} className="flex items-center gap-2 p-2 bg-slate-50 rounded-lg">
                      <span className="text-slate-400">{q.question.replace('？', '').replace('?', '')}:</span>
                      <span className="font-medium text-slate-700">{opt?.icon} {opt?.label}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
