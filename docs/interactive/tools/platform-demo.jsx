---
title: "Platform Demo"
tags: [demo, walkthrough, interactive]
audience: [platform-engineer, domain-expert, tenant]
version: v2.1.0
lang: en
related: [wizard, cli-playground, onboarding-checklist]
---

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { ChevronRight, Play, RotateCcw, Zap } from 'lucide-react';

const t = window.__t || ((zh, en) => en);

const PHASE_CONFIG = [
  {
    id: 'scaffold',
    title: t('Scaffold Tenant', 'Scaffold Tenant'),
    description: t('Create demo-tenant.yaml with dynamic alerting config', 'Create demo-tenant.yaml with dynamic alerting config'),
    command: 'da-tools scaffold --tenant demo-tenant --output conf.d/demo-tenant.yaml',
    terminal: [
      '$ da-tools scaffold --tenant demo-tenant --output conf.d/demo-tenant.yaml',
      '',
      '▶ Scaffold: demo-tenant',
      '  Creating conf.d/demo-tenant.yaml...',
      '  ✓ Applied defaults (3-state mode: Normal)',
      '  ✓ Set replicas=2 (HA)',
      '  ✓ Configured webhook domain policy',
      '  ✓ Validated schema (18 keys)',
      '',
      'Output: conf.d/demo-tenant.yaml (847 bytes)',
      'Status: SUCCESS',
    ],
    sample: `apiVersion: alertmanager.io/v1
kind: TenantConfig
metadata:
  name: demo-tenant
  namespace: db-a
spec:
  _replicas: 2
  _state: normal
  _routing_defaults:
    group_wait: 10s
    group_interval: 30s
    receiver: webhook
  receivers:
    - name: webhook
      webhook_configs:
        - url: "https://incident.demo.local/hook"`,
  },
  {
    id: 'migrate',
    title: t('Migrate Rules', 'Migrate Rules'),
    description: t('Convert legacy Prometheus rules to Dynamic Alerting format', 'Convert legacy Prometheus rules to Dynamic Alerting format'),
    command: 'da-tools migrate --from legacy_rules.yaml --to rules.new.yaml',
    terminal: [
      '$ da-tools migrate --from legacy_rules.yaml --to rules.new.yaml',
      '',
      '▶ Migrate: Scanning legacy rules...',
      '  Found 3 legacy rules:',
      '    • HighMemoryUsage (severity: warning)',
      '    • DiskSpaceAlert (severity: critical)',
      '    • APILatencyWarning (severity: warning)',
      '',
      '  Converting HighMemoryUsage → MemoryUtilizationHigh',
      '  ✓ Added _re label support for regex dimensions',
      '  ✓ Mapped severity to 3-state model',
      '  ✓ Generated sentinel alert pattern',
      '',
      '  Converting DiskSpaceAlert → DiskUtilizationCritical',
      '  ✓ Added cardinality limits (500 per tenant)',
      '',
      '  Converting APILatencyWarning → APILatencySlow',
      '  ✓ Applied duration thresholds',
      '',
      'Output: rules.new.yaml (1.2 KB)',
      'Status: SUCCESS (3 rules migrated)',
    ],
    sample: `groups:
  - name: demo-tenant-rules
    rules:
      - alert: MemoryUtilizationHigh
        expr: node_memory_util > 85
        for: 2m
        annotations:
          summary: "Memory > 85%"
          _severity: warning`,
  },
  {
    id: 'validate',
    title: t('Validate Config', 'Validate Config'),
    description: t('Comprehensive schema, routing, and policy validation', 'Comprehensive schema, routing, and policy validation'),
    command: 'da-tools validate --config conf.d/ --policy policy.yaml',
    terminal: [
      '$ da-tools validate --config conf.d/ --policy policy.yaml',
      '',
      '▶ Validate: Running 4 checks...',
      '  ✓ Schema check (18/18 keys valid)',
      '    - _replicas: 2 (HA)',
      '    - _state: normal',
      '    - _routing_defaults: ok',
      '',
      '  ✓ Routing check (3 routes, 2 receivers)',
      '    - webhook policy: fnmatch allowed',
      '    - group_wait: 10s (5s–5m guardrail OK)',
      '    - group_interval: 30s (5s–5m guardrail OK)',
      '',
      '  ✓ Policy check (webhook domain allowlist)',
      '    - incident.demo.local: allowed',
      '',
      '  ✓ Version check',
      '    - Platform: v2.1.0',
      '    - Exporter: v2.1.0',
      '',
      'Status: ALL CHECKS PASSED',
    ],
  },
  {
    id: 'routes',
    title: t('Generate Routes', 'Generate Routes'),
    description: t('Dynamic Alertmanager route/receiver/inhibit generation', 'Dynamic Alertmanager route/receiver/inhibit generation'),
    command: 'da-tools generate-routes --config conf.d/ --output am-config.yaml',
    terminal: [
      '$ da-tools generate-routes --config conf.d/ --output am-config.yaml',
      '',
      '▶ Generate Routes: Processing tenants...',
      '  ✓ demo-tenant',
      '    - Routes: 3 (webhook × 1, email × 1, slack × 1)',
      '    - Receivers: 2 (webhook, email)',
      '    - Inhibit rules: 4',
      '      • info + warning → inhibit',
      '      • warning + critical → inhibit',
      '      • sentinel + any → inhibit',
      '',
      '  Route tree:',
      '    root (demo-tenant)',
      '    ├─ webhook (priority: high)',
      '    ├─ email (priority: normal)',
      '    └─ escalation (priority: critical)',
      '',
      'Output: am-config.yaml (2.8 KB)',
      'Status: SUCCESS',
    ],
  },
  {
    id: 'baseline',
    title: t('Baseline Discovery', 'Baseline Discovery'),
    description: t('Discover metrics and suggest thresholds', 'Discover metrics and suggest thresholds'),
    command: 'da-tools baseline --prometheus http://localhost:9090 --tenant demo-tenant',
    terminal: [
      '$ da-tools baseline --prometheus http://localhost:9090 --tenant demo-tenant',
      '',
      '▶ Baseline: Scanning prometheus...',
      '  ✓ Connected to http://localhost:9090',
      '',
      '  Discovered 12 metrics:',
      '    • node_memory_util (samples: 1200)',
      '    • node_cpu_util (samples: 1200)',
      '    • disk_utilization (samples: 600)',
      '    • api_request_duration_seconds (samples: 4800)',
      '    • http_requests_total (samples: 2400)',
      '    • pg_connections_used (samples: 480)',
      '',
      '  Suggested thresholds (p95 / p99 / max):',
      '  ┌────────────────────────┬──────┬──────┬──────┐',
      '  │ Metric                 │ P95  │ P99  │ Max  │',
      '  ├────────────────────────┼──────┼──────┼──────┤',
      '  │ node_memory_util       │ 82%  │ 88%  │ 91%  │',
      '  │ node_cpu_util          │ 75%  │ 85%  │ 92%  │',
      '  │ api_request_duration_s │ 145ms│ 250ms│ 890ms│',
      '  └────────────────────────┴──────┴──────┴──────┘',
      '',
      'Status: SUCCESS (12 metrics, 3 threshold suggestions)',
    ],
  },
];

function Terminal({ lines, isTyping, typingIndex }) {
  const displayedLines = useMemo(() => {
    if (!isTyping || typingIndex < 0) return lines;

    const result = [];
    let charCount = 0;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const lineLen = line.length;

      if (charCount + lineLen < typingIndex) {
        result.push(line);
        charCount += lineLen + 1;
      } else if (charCount < typingIndex) {
        const remaining = typingIndex - charCount;
        result.push(line.substring(0, remaining));
        break;
      } else {
        break;
      }
    }

    return result;
  }, [lines, isTyping, typingIndex]);

  return (
    <div className="bg-slate-900 rounded-lg overflow-hidden border border-slate-700 shadow-xl">
      {/* Terminal Title Bar */}
      <div className="bg-slate-800 px-4 py-3 border-b border-slate-700 flex items-center gap-2">
        <div className="flex gap-2">
          <div className="w-3 h-3 rounded-full bg-red-500"></div>
          <div className="w-3 h-3 rounded-full bg-yellow-500"></div>
          <div className="w-3 h-3 rounded-full bg-green-500"></div>
        </div>
        <span className="text-slate-400 text-sm ml-2 font-mono">demo-terminal</span>
      </div>

      {/* Terminal Content */}
      <div className="p-4 font-mono text-sm text-green-400 overflow-y-auto max-h-96 bg-slate-950">
        {displayedLines.map((line, idx) => (
          <div key={idx} className="whitespace-pre-wrap break-words">
            {line}
          </div>
        ))}
        {isTyping && displayedLines.length < lines.length && (
          <span className="animate-pulse">▌</span>
        )}
      </div>
    </div>
  );
}

function Stepper({ phases, currentPhase, completedPhases }) {
  return (
    <div className="space-y-4">
      {phases.map((phase, idx) => {
        const isActive = phase.id === currentPhase;
        const isCompleted = completedPhases.includes(phase.id);
        const isPending = !isActive && !isCompleted;

        let bgColor = 'bg-slate-200 text-slate-700';
        let borderColor = 'border-slate-300';

        if (isActive) {
          bgColor = 'bg-blue-500 text-white animate-pulse';
          borderColor = 'border-blue-400';
        } else if (isCompleted) {
          bgColor = 'bg-green-500 text-white';
          borderColor = 'border-green-400';
        }

        return (
          <div
            key={phase.id}
            className={`flex items-start gap-4 cursor-pointer transition-all ${
              isActive ? 'scale-105' : ''
            }`}
          >
            <div
              className={`flex-shrink-0 w-10 h-10 rounded-full flex items-center justify-center font-bold border-2 ${bgColor} ${borderColor}`}
            >
              {isCompleted ? '✓' : idx + 1}
            </div>
            <div className="flex-1">
              <h3
                className={`font-semibold ${
                  isActive ? 'text-blue-600' : isCompleted ? 'text-green-600' : 'text-slate-500'
                }`}
              >
                {phase.title}
              </h3>
              <p className="text-sm text-slate-500">{phase.description}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function CodeBlock({ code, language = 'yaml' }) {
  return (
    <div className="bg-slate-100 rounded-lg p-4 overflow-x-auto border border-slate-200">
      <pre className="font-mono text-sm text-slate-800">
        <code>{code}</code>
      </pre>
    </div>
  );
}

function SampleSection({ sample, language = 'yaml' }) {
  const [isExpanded, setIsExpanded] = useState(false);

  return (
    <div className="mt-6">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="text-sm font-medium text-blue-600 hover:text-blue-700 flex items-center gap-2"
      >
        <ChevronRight
          className={`w-4 h-4 transition-transform ${isExpanded ? 'rotate-90' : ''}`}
        />
        {t('查看範例輸出', 'View Sample Output')}
      </button>
      {isExpanded && (
        <div className="mt-3">
          <CodeBlock code={sample} language={language} />
        </div>
      )}
    </div>
  );
}

function PhaseContent({ phase, isActive, isRunning, onRun, typingSpeed }) {
  const [typingIdx, setTypingIdx] = useState(-1);

  // Drive typing animation: increment typingIdx while isRunning
  useEffect(() => {
    if (!isRunning) { setTypingIdx(-1); return; }
    setTypingIdx(0);
    const totalChars = phase.terminal.reduce((s, l) => s + l.length + 1, 0);
    const id = setInterval(() => {
      setTypingIdx(prev => {
        if (prev >= totalChars) { clearInterval(id); return prev; }
        return prev + 2; // 2 chars per tick for smooth progress
      });
    }, typingSpeed || 20);
    return () => clearInterval(id);
  }, [isRunning, phase.terminal, typingSpeed]);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-3xl font-bold text-slate-900">{phase.title}</h2>
        <p className="text-slate-600 mt-2">{phase.description}</p>
      </div>

      <div>
        <div className="text-sm font-mono text-slate-600 bg-slate-100 p-3 rounded-lg border border-slate-200">
          {phase.command}
        </div>
      </div>

      <Terminal
        lines={phase.terminal}
        isTyping={isActive && isRunning}
        typingIndex={typingIdx}
      />

      {phase.sample && <SampleSection sample={phase.sample} />}

      {isActive && (
        <button
          onClick={onRun}
          disabled={isRunning}
          className="px-6 py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:bg-slate-400 flex items-center gap-2 transition-all"
        >
          <Play className="w-4 h-4" />
          {isRunning ? t('執行中...', 'Running...') : t('執行階段', 'Run Phase')}
        </button>
      )}
    </div>
  );
}

export default function PlatformDemo() {
  const [currentPhaseIdx, setCurrentPhaseIdx] = useState(0);
  const [completedPhases, setCompletedPhases] = useState([]);
  const [isRunning, setIsRunning] = useState(false);
  const [autoPlay, setAutoPlay] = useState(false);
  const [typingSpeed, setTypingSpeed] = useState(20);

  const currentPhase = PHASE_CONFIG[currentPhaseIdx];

  const handleRun = useCallback(() => {
    if (isRunning) return;

    setIsRunning(true);

    setTimeout(() => {
      setIsRunning(false);
      if (!completedPhases.includes(currentPhase.id)) {
        setCompletedPhases([...completedPhases, currentPhase.id]);
      }

      if (autoPlay && currentPhaseIdx < PHASE_CONFIG.length - 1) {
        setTimeout(() => {
          setCurrentPhaseIdx(currentPhaseIdx + 1);
        }, 500);
      }
    }, (currentPhase.terminal.length * typingSpeed + 1000));
  }, [isRunning, completedPhases, currentPhase.id, autoPlay, currentPhaseIdx, typingSpeed]);

  const handleNext = useCallback(() => {
    if (currentPhaseIdx < PHASE_CONFIG.length - 1) {
      setCurrentPhaseIdx(currentPhaseIdx + 1);
    }
  }, [currentPhaseIdx]);

  const handlePrev = useCallback(() => {
    if (currentPhaseIdx > 0) {
      setCurrentPhaseIdx(currentPhaseIdx - 1);
    }
  }, [currentPhaseIdx]);

  const handleReset = useCallback(() => {
    setCurrentPhaseIdx(0);
    setCompletedPhases([]);
    setIsRunning(false);
    setAutoPlay(false);
  }, []);

  const allCompleted = PHASE_CONFIG.every((p) => completedPhases.includes(p.id));

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100">
      {/* Header */}
      <div className="bg-white border-b border-slate-200 shadow-sm">
        <div className="max-w-7xl mx-auto px-6 py-8">
          <h1 className="text-4xl font-bold text-slate-900">{t('平台展示', 'Platform Demo')}</h1>
          <p className="text-slate-600 mt-2 text-lg">
            {t('體驗 ', 'Experience the ')}<code className="bg-slate-100 px-2 py-1 rounded text-sm font-mono">make demo</code>{t(' 工作流 — 無需叢集', ' workflow — no cluster required')}
          </p>
        </div>
      </div>

      {/* Main Content */}
      <div className="max-w-7xl mx-auto px-6 py-8">
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-8">
          {/* Left Sidebar: Stepper */}
          <div className="lg:col-span-1">
            <div className="sticky top-8">
              <Stepper
                phases={PHASE_CONFIG}
                currentPhase={currentPhase.id}
                completedPhases={completedPhases}
              />
            </div>
          </div>

          {/* Right Content: Phase Details */}
          <div className="lg:col-span-3 space-y-8">
            {/* Controls */}
            <div className="flex flex-wrap gap-3 items-center">
              <button
                onClick={handleRun}
                disabled={isRunning}
                className="px-4 py-2 bg-green-600 text-white rounded-lg font-medium hover:bg-green-700 disabled:bg-slate-400 flex items-center gap-2 transition-all"
              >
                <Play className="w-4 h-4" />
                {t('執行', 'Run')}
              </button>

              <button
                onClick={() => setAutoPlay(!autoPlay)}
                className={`px-4 py-2 rounded-lg font-medium flex items-center gap-2 transition-all ${
                  autoPlay
                    ? 'bg-purple-600 text-white hover:bg-purple-700'
                    : 'bg-slate-200 text-slate-700 hover:bg-slate-300'
                }`}
              >
                <Zap className="w-4 h-4" />
                {autoPlay ? t('自動播放: 開啟', 'Auto-Play: ON') : t('自動播放: 關閉', 'Auto-Play: OFF')}
              </button>

              <button
                onClick={handleReset}
                className="px-4 py-2 bg-slate-200 text-slate-700 rounded-lg font-medium hover:bg-slate-300 flex items-center gap-2 transition-all"
              >
                <RotateCcw className="w-4 h-4" />
                {t('重置', 'Reset')}
              </button>

              <select
                value={typingSpeed}
                onChange={(e) => setTypingSpeed(parseInt(e.target.value))}
                className="px-3 py-2 bg-slate-200 text-slate-700 rounded-lg font-medium text-sm"
              >
                <option value={5}>{t('速度: 快', 'Speed: Fast')}</option>
                <option value={20}>{t('速度: 正常', 'Speed: Normal')}</option>
                <option value={50}>{t('速度: 慢', 'Speed: Slow')}</option>
              </select>
            </div>

            {/* Phase Content */}
            <PhaseContent
              phase={currentPhase}
              isActive={true}
              isRunning={isRunning}
              onRun={handleRun}
              typingSpeed={typingSpeed}
            />

            {/* Navigation */}
            <div className="flex gap-4 justify-between">
              <button
                onClick={handlePrev}
                disabled={currentPhaseIdx === 0}
                className="px-6 py-3 bg-slate-200 text-slate-700 rounded-lg font-medium hover:bg-slate-300 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
              >
                {t('← 上一個', '← Previous')}
              </button>

              <div className="text-center text-slate-600 font-medium">
                {t(`階段 ${currentPhaseIdx + 1} / ${PHASE_CONFIG.length}`, `Phase ${currentPhaseIdx + 1} of ${PHASE_CONFIG.length}`)}
              </div>

              <button
                onClick={handleNext}
                disabled={currentPhaseIdx === PHASE_CONFIG.length - 1}
                className="px-6 py-3 bg-slate-200 text-slate-700 rounded-lg font-medium hover:bg-slate-300 disabled:opacity-50 disabled:cursor-not-allowed transition-all flex items-center gap-2"
              >
                {t('下一個 →', 'Next →')}
              </button>
            </div>

            {/* Completion Summary */}
            {allCompleted && (
              <div className="bg-gradient-to-r from-green-50 to-emerald-50 border border-green-200 rounded-lg p-6 shadow-sm">
                <h3 className="text-xl font-bold text-green-900">{t('展示完成!', 'Demo Complete!')}</h3>
                <p className="text-green-800 mt-2">
                  {t('您已成功完成整個 ', "You've successfully walked through the entire ")}<code className="bg-green-100 px-2 py-1 rounded text-sm font-mono">make demo</code>{t(' 工作流。', ' workflow.')}
                </p>
                <div className="mt-4 space-y-2">
                  <p className="font-semibold text-green-900">{t('後續步驟:', 'Next Steps:')}</p>
                  <ul className="list-disc list-inside text-green-800 text-sm space-y-1">
                    <li>
                      <a href="/docs/getting-started/for-platform-engineers.md" className="underline hover:text-green-700">
                        {t('部署到您的 Kubernetes 叢集', 'Deploy to your Kubernetes cluster')}
                      </a>
                    </li>
                    <li>
                      <a href="/docs/cli-reference.md" className="underline hover:text-green-700">
                        {t('探索完整的 CLI 參考', 'Explore the full CLI reference')}
                      </a>
                    </li>
                    <li>
                      <a href="/docs/scenarios/" className="underline hover:text-green-700">
                        {t('嘗試互動式場景', 'Try interactive scenarios')}
                      </a>
                    </li>
                    <li>
                      <a href="/docs/architecture-and-design.md" className="underline hover:text-green-700">
                        {t('了解架構', 'Understand the architecture')}
                      </a>
                    </li>
                  </ul>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
