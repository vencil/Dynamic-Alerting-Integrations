---
title: "Platform Demo"
version: v2.9.0
lang: en
related: [wizard, cli-playground, onboarding-checklist]
---

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { ChevronRight, Play, RotateCcw, Zap } from 'lucide-react';
import { DEMO_TRANSCRIPTS } from './platform-demo/fixtures/transcripts.js';
import { CHARS_PER_TICK, transcriptChars, runDurationMs } from './platform-demo/utils/typing.js';

const t = window.__t || ((zh, en) => en);

// command / terminal / sample are recorded from real da-tools runs — see the
// fixture's header for how they were captured and must be re-captured.
const PHASE_CONFIG = [
  {
    id: 'scaffold',
    title: t('建立租戶設定', 'Scaffold Tenant'),
    description: t('以非互動模式產生 demo-tenant.yaml 與 _defaults.yaml（含 webhook 路由）', 'Generate demo-tenant.yaml and _defaults.yaml non-interactively (with webhook routing)'),
    ...DEMO_TRANSCRIPTS.scaffold,
  },
  {
    id: 'migrate',
    title: t('遷移規則', 'Migrate Rules'),
    description: t('把傳統 Prometheus 警報規則轉成動態多租戶三件套（平台規則 + 租戶閾值）', 'Convert legacy Prometheus alert rules into the dynamic multi-tenant set (platform rules + tenant thresholds)'),
    ...DEMO_TRANSCRIPTS.migrate,
  },
  {
    id: 'validate',
    title: t('驗證設定', 'Validate Config'),
    description: t('一站式驗證 conf.d/：YAML 語法、schema、路由、profile、policy DSL、租戶唯一性', 'One-stop validation of conf.d/: YAML syntax, schema, routes, profiles, policy DSL, tenant uniqueness'),
    ...DEMO_TRANSCRIPTS.validate,
  },
  {
    id: 'routes',
    title: t('產生路由', 'Generate Routes'),
    description: t('從租戶設定產生 Alertmanager route / receiver / inhibit 片段', 'Generate the Alertmanager route / receiver / inhibit fragment from tenant config'),
    ...DEMO_TRANSCRIPTS.routes,
  },
  {
    id: 'baseline',
    title: t('基線探索', 'Baseline Discovery'),
    description: t('觀測指標負載並建議閾值——此處數值取自本機 stub Prometheus，為合成資料，不是任何租戶的實測', 'Observe metric load and suggest thresholds — the values here come from a local stub Prometheus: synthetic, not measured on any tenant'),
    ...DEMO_TRANSCRIPTS.baseline,
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
              {isCompleted ? <span aria-hidden="true">✓</span> : idx + 1}
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
    const totalChars = transcriptChars(phase.terminal);
    const id = setInterval(() => {
      setTypingIdx(prev => {
        if (prev >= totalChars) { clearInterval(id); return prev; }
        return prev + CHARS_PER_TICK;
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
    }, runDurationMs(currentPhase.terminal, typingSpeed));
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
            {t('導覽平台工作流（scaffold → migrate → validate → routes → baseline）— 真實 da-tools 指令、預錄輸出（瀏覽器內重播），無需叢集', 'A walkthrough of the platform workflow (scaffold → migrate → validate → routes → baseline) — real da-tools commands, recorded output replayed in the browser, no cluster required')}
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
                aria-label={t('打字速度', 'Typing speed')}
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
                  {t('您已走完整個平台工作流導覽（真實指令、預錄輸出）。', "You've walked through the entire platform workflow tour (real commands, recorded output).")}
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
