---
title: "Alert Timeline Replay"
tags: [animation, timeline, dedup]
audience: ["domain-expert", tenant]
version: v2.0.0-preview.2
lang: en
related: [alert-simulator, runbook-viewer, glossary]
---

import React, { useState, useEffect, useRef, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── simulated metric scenarios ── */
const SCENARIOS = [
  {
    id: 'cpu-spike',
    name: t('CPU 尖峰', 'CPU Spike'),
    desc: t('CPU 使用率從 40% 飆升到 95% 再恢復', 'CPU usage spikes from 40% to 95% then recovers'),
    metric: 'cpu_usage_percent',
    unit: '%',
    warn: 80,
    crit: 90,
    duration: 20,
    points: [40,42,45,50,58,72,85,92,95,94,91,88,82,75,68,55,48,42,40,39],
  },
  {
    id: 'memory-leak',
    name: t('記憶體洩漏', 'Memory Leak'),
    desc: t('記憶體穩定上升直到觸發 critical', 'Memory steadily rises until critical fires'),
    metric: 'memory_usage_percent',
    unit: '%',
    warn: 70,
    crit: 85,
    duration: 20,
    points: [35,38,42,46,50,54,58,63,67,72,76,80,83,87,89,90,88,84,78,70],
  },
  {
    id: 'conn-pool',
    name: t('連線池耗盡', 'Connection Pool Exhaustion'),
    desc: t('連線數逐步上升觸發 warning 後自動回收', 'Connections climb past warning then auto-reclaim'),
    metric: 'active_connections',
    unit: '',
    warn: 150,
    crit: 200,
    duration: 20,
    points: [60,70,85,100,115,130,148,160,172,155,140,125,110,100,90,80,75,70,65,60],
  },
  {
    id: 'replication-lag',
    name: t('複製延遲', 'Replication Lag'),
    desc: t('從庫延遲飆升觸發 critical 後恢復', 'Replica lag spikes to critical then resolves'),
    metric: 'replication_lag_seconds',
    unit: 's',
    warn: 5,
    crit: 10,
    duration: 20,
    points: [1,1.2,1.5,2,3,4.5,6,8,11,13,12,9,7,5,3.5,2.5,2,1.5,1.2,1],
  },
];

function classifyState(val, warn, crit) {
  if (val >= crit) return 'critical';
  if (val >= warn) return 'warning';
  return 'ok';
}

/* ── small SVG chart ── */
function MiniChart({ points, warn, crit, currentIdx, unit }) {
  const w = 600, h = 200, pad = 40;
  const max = Math.max(...points, crit * 1.2);
  const min = Math.min(...points, 0);
  const xStep = (w - pad * 2) / (points.length - 1);
  const yScale = (v) => h - pad - ((v - min) / (max - min)) * (h - pad * 2);

  const pathD = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${pad + i * xStep},${yScale(p)}`).join(' ');
  const warnY = yScale(warn);
  const critY = yScale(crit);

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full" style={{ maxHeight: 220 }}>
      {/* grid */}
      <line x1={pad} y1={warnY} x2={w - pad} y2={warnY} stroke="#f59e0b" strokeDasharray="6 4" strokeWidth="1.5" />
      <line x1={pad} y1={critY} x2={w - pad} y2={critY} stroke="#ef4444" strokeDasharray="6 4" strokeWidth="1.5" />
      <text x={pad - 4} y={warnY + 4} textAnchor="end" className="text-xs" fill="#f59e0b" fontSize="11">warn {warn}{unit}</text>
      <text x={pad - 4} y={critY + 4} textAnchor="end" className="text-xs" fill="#ef4444" fontSize="11">crit {crit}{unit}</text>
      {/* metric line */}
      <path d={pathD} fill="none" stroke="#3b82f6" strokeWidth="2.5" strokeLinejoin="round" />
      {/* current point */}
      {currentIdx >= 0 && currentIdx < points.length && (
        <>
          <circle cx={pad + currentIdx * xStep} cy={yScale(points[currentIdx])} r="6"
            fill={classifyState(points[currentIdx], warn, crit) === 'critical' ? '#ef4444' : classifyState(points[currentIdx], warn, crit) === 'warning' ? '#f59e0b' : '#22c55e'}
            stroke="#fff" strokeWidth="2" />
          <text x={pad + currentIdx * xStep} y={yScale(points[currentIdx]) - 12} textAnchor="middle" fontSize="12" fontWeight="bold"
            fill={classifyState(points[currentIdx], warn, crit) === 'critical' ? '#ef4444' : classifyState(points[currentIdx], warn, crit) === 'warning' ? '#f59e0b' : '#22c55e'}>
            {points[currentIdx]}{unit}
          </text>
        </>
      )}
      {/* x-axis labels */}
      {points.map((_, i) => i % 5 === 0 ? (
        <text key={i} x={pad + i * xStep} y={h - 8} textAnchor="middle" fontSize="10" fill="#94a3b8">{i}s</text>
      ) : null)}
    </svg>
  );
}

/* ── event log ── */
function EventLog({ events, currentIdx }) {
  const ref = useRef(null);
  const visible = events.filter(e => e.tick <= currentIdx);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [visible.length]);

  return (
    <div ref={ref} className="h-48 overflow-y-auto border border-slate-200 rounded-lg bg-slate-50 p-3 font-mono text-xs space-y-1">
      {visible.length === 0 && <div className="text-slate-400 italic">{t('等待事件...', 'Waiting for events...')}</div>}
      {visible.map((ev, i) => (
        <div key={i} className="flex gap-2">
          <span className="text-slate-400 w-8 text-right flex-shrink-0">{ev.tick}s</span>
          <span className={`font-bold flex-shrink-0 ${ev.color}`}>{ev.badge}</span>
          <span className="text-slate-700">{ev.text}</span>
        </div>
      ))}
    </div>
  );
}

export default function AlertTimeline() {
  const [scenarioId, setScenarioId] = useState(SCENARIOS[0].id);
  const [playing, setPlaying] = useState(false);
  const [tick, setTick] = useState(-1);
  const [speed, setSpeed] = useState(1);
  const intervalRef = useRef(null);

  const scenario = SCENARIOS.find(s => s.id === scenarioId);

  // compute events for scenario
  const events = useMemo(() => {
    const evts = [];
    let prevState = 'ok';
    let warnPending = 0;
    let critPending = 0;
    const FOR_DURATION = 2; // simulated "for: 2s"

    scenario.points.forEach((val, i) => {
      const state = classifyState(val, scenario.warn, scenario.crit);

      if (state === 'warning' && prevState === 'ok') {
        warnPending = 1;
        evts.push({ tick: i, badge: 'PENDING', color: 'text-amber-600', text: `${scenario.metric} = ${val}${scenario.unit} ≥ warn(${scenario.warn}) — pending (${FOR_DURATION}s for)` });
      } else if (state === 'warning' && warnPending > 0 && warnPending < FOR_DURATION) {
        warnPending++;
      } else if (state === 'warning' && warnPending === FOR_DURATION) {
        warnPending = FOR_DURATION + 1;
        evts.push({ tick: i, badge: 'FIRING', color: 'text-amber-600', text: `⚠️ Warning alert FIRES — ${scenario.metric} = ${val}${scenario.unit}` });
      }

      if (state === 'critical' && prevState !== 'critical') {
        critPending = 1;
        evts.push({ tick: i, badge: 'PENDING', color: 'text-red-600', text: `${scenario.metric} = ${val}${scenario.unit} ≥ crit(${scenario.crit}) — pending` });
      } else if (state === 'critical' && critPending > 0 && critPending < FOR_DURATION) {
        critPending++;
      } else if (state === 'critical' && critPending === FOR_DURATION) {
        critPending = FOR_DURATION + 1;
        evts.push({ tick: i, badge: 'FIRING', color: 'text-red-600', text: `🔴 Critical alert FIRES — ${scenario.metric} = ${val}${scenario.unit}` });
        if (warnPending > FOR_DURATION) {
          evts.push({ tick: i, badge: 'DEDUP', color: 'text-purple-600', text: `Severity dedup: Warning alert SUPPRESSED by Critical` });
        }
      }

      if (state === 'warning' && prevState === 'critical') {
        evts.push({ tick: i, badge: 'RESOLVED', color: 'text-green-600', text: `Critical alert RESOLVED — dropped below ${scenario.crit}${scenario.unit}` });
        critPending = 0;
      }

      if (state === 'ok' && (prevState === 'warning' || prevState === 'critical')) {
        if (prevState === 'critical') {
          evts.push({ tick: i, badge: 'RESOLVED', color: 'text-green-600', text: `Critical alert RESOLVED — ${scenario.metric} = ${val}${scenario.unit}` });
        }
        if (warnPending > FOR_DURATION) {
          evts.push({ tick: i, badge: 'RESOLVED', color: 'text-green-600', text: `Warning alert RESOLVED — ${scenario.metric} = ${val}${scenario.unit}` });
        }
        warnPending = 0;
        critPending = 0;
      }

      prevState = state;
    });
    return evts;
  }, [scenarioId]);

  // playback control
  useEffect(() => {
    if (playing) {
      intervalRef.current = setInterval(() => {
        setTick(prev => {
          if (prev >= scenario.points.length - 1) {
            setPlaying(false);
            return prev;
          }
          return prev + 1;
        });
      }, 800 / speed);
    }
    return () => clearInterval(intervalRef.current);
  }, [playing, speed, scenarioId]);

  const handlePlay = () => {
    if (tick >= scenario.points.length - 1) setTick(-1);
    setPlaying(true);
  };
  const handlePause = () => setPlaying(false);
  const handleReset = () => { setPlaying(false); setTick(-1); };
  const handleScenarioChange = (id) => { setPlaying(false); setTick(-1); setScenarioId(id); };

  const currentState = tick >= 0 ? classifyState(scenario.points[tick], scenario.warn, scenario.crit) : 'ok';
  const stateBg = currentState === 'critical' ? 'bg-red-100 border-red-300' : currentState === 'warning' ? 'bg-amber-100 border-amber-300' : 'bg-green-100 border-green-300';
  const stateText = currentState === 'critical' ? 'text-red-700' : currentState === 'warning' ? 'text-amber-700' : 'text-green-700';

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-5xl mx-auto">
        <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('告警時間軸重播', 'Alert Timeline Replay')}</h1>
        <p className="text-slate-600 mb-6">{t('選擇場景，觀看 metric 變化如何觸發告警、dedup、和恢復', 'Pick a scenario and watch how metric changes trigger alerts, dedup, and recovery')}</p>

        {/* Scenario selector */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
          {SCENARIOS.map(s => (
            <button key={s.id} onClick={() => handleScenarioChange(s.id)}
              className={`text-left p-3 rounded-xl border transition-all ${s.id === scenarioId ? 'border-blue-500 bg-blue-50 shadow-sm' : 'border-slate-200 bg-white hover:border-blue-300'}`}>
              <div className="font-semibold text-sm text-slate-900">{s.name}</div>
              <div className="text-xs text-slate-500 mt-1">{s.desc}</div>
            </button>
          ))}
        </div>

        {/* Chart */}
        <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 mb-6">
          <div className="flex items-center justify-between mb-4">
            <div>
              <span className="font-mono text-sm text-slate-600">{scenario.metric}</span>
              <span className="ml-3 text-xs text-slate-400">warn={scenario.warn}{scenario.unit} crit={scenario.crit}{scenario.unit}</span>
            </div>
            <div className={`px-3 py-1 rounded-full border text-xs font-bold ${stateBg} ${stateText}`}>
              {tick < 0 ? t('就緒', 'READY') : currentState.toUpperCase()}
            </div>
          </div>
          <MiniChart points={scenario.points} warn={scenario.warn} crit={scenario.crit} currentIdx={tick} unit={scenario.unit} />
        </div>

        {/* Controls */}
        <div className="flex items-center gap-3 mb-6 flex-wrap">
          {!playing ? (
            <button onClick={handlePlay} className="px-5 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700">
              {tick < 0 ? '▶ Play' : '▶ Resume'}
            </button>
          ) : (
            <button onClick={handlePause} className="px-5 py-2 bg-amber-500 text-white rounded-lg text-sm font-medium hover:bg-amber-600">
              ⏸ Pause
            </button>
          )}
          <button onClick={handleReset} className="px-5 py-2 bg-slate-200 text-slate-700 rounded-lg text-sm font-medium hover:bg-slate-300">
            ⏮ Reset
          </button>
          <div className="flex items-center gap-2 ml-auto">
            <span className="text-xs text-slate-500">{t('速度', 'Speed')}:</span>
            {[0.5, 1, 2, 4].map(s => (
              <button key={s} onClick={() => setSpeed(s)}
                className={`px-2 py-1 rounded text-xs font-medium ${speed === s ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'}`}>
                {s}x
              </button>
            ))}
          </div>
          {/* Scrubber */}
          <input type="range" min={-1} max={scenario.points.length - 1} value={tick}
            onChange={(e) => { setPlaying(false); setTick(parseInt(e.target.value)); }}
            className="w-full mt-2" />
        </div>

        {/* Event log */}
        <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6">
          <h2 className="text-sm font-semibold text-slate-700 mb-3">{t('事件日誌', 'Event Log')}</h2>
          <EventLog events={events} currentIdx={tick} />
          <div className="mt-3 flex gap-4 text-xs text-slate-500">
            <span><span className="inline-block w-2 h-2 bg-amber-500 rounded-full mr-1"></span>{t('Warning 觸發/待定', 'Warning fire/pending')}</span>
            <span><span className="inline-block w-2 h-2 bg-red-500 rounded-full mr-1"></span>{t('Critical 觸發', 'Critical fire')}</span>
            <span><span className="inline-block w-2 h-2 bg-purple-500 rounded-full mr-1"></span>{t('Severity Dedup', 'Severity Dedup')}</span>
            <span><span className="inline-block w-2 h-2 bg-green-500 rounded-full mr-1"></span>{t('恢復', 'Resolved')}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
