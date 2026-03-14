---
title: "Runbook Viewer"
tags: [runbook, alerts, operations, interactive]
audience: [tenant, platform-engineer, domain-expert]
version: v2.0.0-preview.2
lang: en
related: [alert-simulator, alert-timeline, health-dashboard]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Alert runbook data ── */
const RUNBOOKS = [
  {
    id: 'mariadb-conn-high',
    alert: 'MariaDBConnectionsHigh',
    severity: 'warning',
    pack: 'mariadb',
    summary: t('MariaDB 連線數超過 warning 閾值', 'MariaDB connections exceeded warning threshold'),
    meaning: t('活躍連線數持續高於設定的 warning 閾值，可能導致連線池耗盡', 'Active connections are above the warning threshold, risking connection pool exhaustion'),
    diagnose: [
      { step: t('檢查 SHOW PROCESSLIST 找出長時間 query', 'Run SHOW PROCESSLIST to find long-running queries'), cmd: 'mysql -e "SHOW FULL PROCESSLIST"' },
      { step: t('確認應用程式連線池設定', 'Check application connection pool configuration'), cmd: null },
      { step: t('查看 Prometheus 確認趨勢', 'Check Prometheus for connection trend'), cmd: 'curl localhost:9090/api/v1/query?query=mysql_global_status_threads_connected' },
    ],
    fix: [
      { action: t('Kill 長時間 idle 連線', 'Kill long-idle connections'), cmd: 'mysql -e "KILL <thread_id>"' },
      { action: t('調高 max_connections（臨時）', 'Increase max_connections (temporary)'), cmd: 'mysql -e "SET GLOBAL max_connections=300"' },
      { action: t('優化應用程式連線池大小', 'Optimize application connection pool size'), cmd: null },
    ],
    verify: [
      { check: t('確認連線數降回正常範圍', 'Verify connections dropped back to normal'), cmd: 'mysql -e "SHOW STATUS LIKE \'Threads_connected\'"' },
      { check: t('確認告警已恢復', 'Confirm alert has resolved'), cmd: null },
    ],
  },
  {
    id: 'mariadb-repl-lag',
    alert: 'MariaDBReplicationLagHigh',
    severity: 'critical',
    pack: 'mariadb',
    summary: t('MariaDB 複製延遲超過 critical 閾值', 'MariaDB replication lag exceeded critical threshold'),
    meaning: t('從庫延遲過大，讀取可能返回過時資料，影響資料一致性', 'Replica lag is too high — reads may return stale data, affecting consistency'),
    diagnose: [
      { step: t('檢查 SHOW SLAVE STATUS', 'Check SHOW SLAVE STATUS'), cmd: 'mysql -e "SHOW SLAVE STATUS\\G"' },
      { step: t('確認主庫寫入量是否異常', 'Check if primary write volume is abnormal'), cmd: null },
      { step: t('檢查網路延遲', 'Check network latency between primary and replica'), cmd: 'ping <primary_host>' },
    ],
    fix: [
      { action: t('暫停非關鍵寫入減輕主庫壓力', 'Pause non-critical writes to reduce primary load'), cmd: null },
      { action: t('調整 slave_parallel_workers', 'Adjust slave_parallel_workers'), cmd: 'mysql -e "SET GLOBAL slave_parallel_workers=4"' },
      { action: t('若差距過大考慮重建從庫', 'If gap is too large, consider rebuilding replica'), cmd: null },
    ],
    verify: [
      { check: t('確認 Seconds_Behind_Master 降低', 'Verify Seconds_Behind_Master is decreasing'), cmd: 'mysql -e "SHOW SLAVE STATUS\\G" | grep Seconds_Behind' },
    ],
  },
  {
    id: 'redis-memory-high',
    alert: 'RedisMemoryUsageHigh',
    severity: 'warning',
    pack: 'redis',
    summary: t('Redis 記憶體使用率超過 warning 閾值', 'Redis memory usage exceeded warning threshold'),
    meaning: t('Redis 記憶體接近上限，可能觸發 eviction 或 OOM', 'Redis memory approaching limit, may trigger eviction or OOM'),
    diagnose: [
      { step: t('查看 INFO memory', 'Check INFO memory'), cmd: 'redis-cli INFO memory' },
      { step: t('分析大 key', 'Analyze large keys'), cmd: 'redis-cli --bigkeys' },
      { step: t('檢查 eviction policy', 'Check eviction policy'), cmd: 'redis-cli CONFIG GET maxmemory-policy' },
    ],
    fix: [
      { action: t('清理過期/無用 key', 'Clean up expired/unused keys'), cmd: null },
      { action: t('調整 maxmemory', 'Adjust maxmemory'), cmd: 'redis-cli CONFIG SET maxmemory 2gb' },
      { action: t('優化資料結構（Hash 取代多個 String）', 'Optimize data structures (Hash instead of many Strings)'), cmd: null },
    ],
    verify: [
      { check: t('確認 used_memory 降低', 'Verify used_memory decreased'), cmd: 'redis-cli INFO memory | grep used_memory_human' },
    ],
  },
  {
    id: 'k8s-pod-restart',
    alert: 'KubernetesPodRestartHigh',
    severity: 'warning',
    pack: 'kubernetes',
    summary: t('Pod 重啟次數超過 warning 閾值', 'Pod restart count exceeded warning threshold'),
    meaning: t('Pod 頻繁重啟，可能是 OOMKill、CrashLoopBackOff 或 liveness probe 失敗', 'Pod restarting frequently — may be OOMKill, CrashLoopBackOff, or failed liveness probe'),
    diagnose: [
      { step: t('查看 Pod 狀態和事件', 'Check pod status and events'), cmd: 'kubectl describe pod <pod_name> -n <namespace>' },
      { step: t('查看容器日誌', 'Check container logs'), cmd: 'kubectl logs <pod_name> -n <namespace> --previous' },
      { step: t('檢查 OOMKilled', 'Check for OOMKilled'), cmd: 'kubectl get pod <pod_name> -n <namespace> -o jsonpath="{.status.containerStatuses[*].lastState}"' },
    ],
    fix: [
      { action: t('若 OOM：增加 memory limit', 'If OOM: increase memory limit'), cmd: 'kubectl edit deployment <name> -n <namespace>' },
      { action: t('若 CrashLoop：修復應用程式錯誤', 'If CrashLoop: fix application error'), cmd: null },
      { action: t('若 probe 失敗：調整 liveness probe 參數', 'If probe failure: adjust liveness probe parameters'), cmd: null },
    ],
    verify: [
      { check: t('確認 Pod 穩定運行', 'Confirm pod is running stably'), cmd: 'kubectl get pod <pod_name> -n <namespace> -w' },
    ],
  },
  {
    id: 'kafka-consumer-lag',
    alert: 'KafkaConsumerLagHigh',
    severity: 'warning',
    pack: 'kafka',
    summary: t('Kafka consumer lag 超過 warning 閾值', 'Kafka consumer lag exceeded warning threshold'),
    meaning: t('Consumer 消費速度跟不上 producer，訊息處理延遲增加', 'Consumer cannot keep up with producer — message processing latency increasing'),
    diagnose: [
      { step: t('查看 consumer group lag', 'Check consumer group lag'), cmd: 'kafka-consumer-groups.sh --describe --group <group> --bootstrap-server <broker>' },
      { step: t('確認 consumer 實例數', 'Check consumer instance count'), cmd: null },
      { step: t('查看 partition 分佈是否均衡', 'Check if partition distribution is balanced'), cmd: null },
    ],
    fix: [
      { action: t('增加 consumer 實例數', 'Increase consumer instance count'), cmd: null },
      { action: t('優化 consumer 處理邏輯', 'Optimize consumer processing logic'), cmd: null },
      { action: t('增加 partition 數（需重新平衡）', 'Increase partition count (requires rebalance)'), cmd: null },
    ],
    verify: [
      { check: t('確認 lag 持續降低', 'Verify lag is consistently decreasing'), cmd: 'kafka-consumer-groups.sh --describe --group <group> --bootstrap-server <broker>' },
    ],
  },
  {
    id: 'node-disk-high',
    alert: 'NodeDiskUsageHigh',
    severity: 'critical',
    pack: 'node',
    summary: t('節點磁碟使用率超過 critical 閾值', 'Node disk usage exceeded critical threshold'),
    meaning: t('磁碟即將滿載，可能導致服務中斷、資料遺失', 'Disk nearly full — may cause service outage or data loss'),
    diagnose: [
      { step: t('查看磁碟使用情況', 'Check disk usage'), cmd: 'df -h' },
      { step: t('找出大檔案', 'Find large files'), cmd: 'du -sh /* | sort -rh | head -20' },
      { step: t('檢查 log 目錄', 'Check log directories'), cmd: 'du -sh /var/log/*' },
    ],
    fix: [
      { action: t('清理日誌和暫存檔', 'Clean up logs and temp files'), cmd: 'journalctl --vacuum-size=500M' },
      { action: t('刪除不需要的 Docker images', 'Remove unused Docker images'), cmd: 'docker system prune -af' },
      { action: t('擴充磁碟或遷移資料', 'Expand disk or migrate data'), cmd: null },
    ],
    verify: [
      { check: t('確認磁碟使用率降低', 'Verify disk usage has decreased'), cmd: 'df -h' },
    ],
  },
];

const PACKS = ['all', ...new Set(RUNBOOKS.map(r => r.pack))];

function StepList({ items, stepKey, cmdKey, icon }) {
  return (
    <div className="space-y-3">
      {items.map((item, i) => (
        <div key={i} className="flex gap-3">
          <span className="flex-shrink-0 w-6 h-6 rounded-full bg-slate-100 text-slate-600 flex items-center justify-center text-xs font-bold">{i + 1}</span>
          <div className="flex-1">
            <p className="text-sm text-slate-700">{item[stepKey]}</p>
            {item[cmdKey] && (
              <code className="block mt-1 text-xs bg-slate-900 text-green-400 px-3 py-2 rounded-lg font-mono overflow-x-auto">
                $ {item[cmdKey]}
              </code>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function RunbookViewer() {
  const [search, setSearch] = useState('');
  const [filterPack, setFilterPack] = useState('all');
  const [expandedId, setExpandedId] = useState(null);

  const filtered = useMemo(() => {
    return RUNBOOKS.filter(r => {
      if (filterPack !== 'all' && r.pack !== filterPack) return false;
      if (search) {
        const q = search.toLowerCase();
        return r.alert.toLowerCase().includes(q) || r.summary.toLowerCase().includes(q) || r.pack.toLowerCase().includes(q);
      }
      return true;
    });
  }, [search, filterPack]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-4xl mx-auto">
        <h1 className="text-3xl font-bold text-slate-900 mb-2">{t('Runbook 檢視器', 'Runbook Viewer')}</h1>
        <p className="text-slate-600 mb-6">{t('每個告警的完整處理流程：診斷 → 修復 → 驗證', 'Complete handling flow for each alert: Diagnose → Fix → Verify')}</p>

        {/* Filters */}
        <div className="flex flex-wrap gap-3 mb-6">
          <input type="text" value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder={t('搜尋告警名稱...', 'Search alert name...')}
            className="flex-1 min-w-48 px-4 py-2 border border-slate-200 rounded-lg text-sm focus:ring-2 focus:ring-blue-500" />
          <select value={filterPack} onChange={(e) => setFilterPack(e.target.value)}
            className="px-3 py-2 border border-slate-200 rounded-lg text-sm bg-white">
            {PACKS.map(p => <option key={p} value={p}>{p === 'all' ? t('所有 Rule Pack', 'All Rule Packs') : p}</option>)}
          </select>
        </div>

        <p className="text-xs text-slate-500 mb-4">{t(`顯示 ${filtered.length} 個 Runbook`, `Showing ${filtered.length} runbooks`)}</p>

        {/* Runbook list */}
        <div className="space-y-4">
          {filtered.map(rb => {
            const isOpen = expandedId === rb.id;
            return (
              <div key={rb.id} className="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
                {/* Header */}
                <button onClick={() => setExpandedId(isOpen ? null : rb.id)}
                  className="w-full text-left px-6 py-4 flex items-center justify-between hover:bg-slate-50 transition-colors">
                  <div className="flex items-center gap-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-bold ${rb.severity === 'critical' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'}`}>
                      {rb.severity}
                    </span>
                    <div>
                      <div className="font-semibold text-slate-900 text-sm">{rb.alert}</div>
                      <div className="text-xs text-slate-500">{rb.pack} — {rb.summary}</div>
                    </div>
                  </div>
                  <span className="text-slate-400 text-lg">{isOpen ? '▾' : '▸'}</span>
                </button>

                {/* Expanded content */}
                {isOpen && (
                  <div className="px-6 pb-6 space-y-6 border-t border-slate-100">
                    {/* Meaning */}
                    <div className="mt-4 p-4 bg-slate-50 rounded-lg">
                      <h3 className="text-xs font-semibold text-slate-500 uppercase mb-1">{t('含義', 'What This Means')}</h3>
                      <p className="text-sm text-slate-700">{rb.meaning}</p>
                    </div>

                    {/* Diagnose */}
                    <div>
                      <h3 className="text-sm font-semibold text-blue-700 mb-3 flex items-center gap-2">
                        <span className="w-6 h-6 bg-blue-100 rounded-full flex items-center justify-center text-xs">1</span>
                        {t('診斷', 'Diagnose')}
                      </h3>
                      <StepList items={rb.diagnose} stepKey="step" cmdKey="cmd" />
                    </div>

                    {/* Fix */}
                    <div>
                      <h3 className="text-sm font-semibold text-amber-700 mb-3 flex items-center gap-2">
                        <span className="w-6 h-6 bg-amber-100 rounded-full flex items-center justify-center text-xs">2</span>
                        {t('修復', 'Fix')}
                      </h3>
                      <StepList items={rb.fix} stepKey="action" cmdKey="cmd" />
                    </div>

                    {/* Verify */}
                    <div>
                      <h3 className="text-sm font-semibold text-green-700 mb-3 flex items-center gap-2">
                        <span className="w-6 h-6 bg-green-100 rounded-full flex items-center justify-center text-xs">3</span>
                        {t('驗證', 'Verify')}
                      </h3>
                      <StepList items={rb.verify} stepKey="check" cmdKey="cmd" />
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
