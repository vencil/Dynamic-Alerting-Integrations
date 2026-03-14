---
title: "da-tools CLI Playground"
tags: [cli, interactive, tools, docker]
audience: [all]
version: v2.0.0-preview.2
lang: en
---

import React, { useState, useCallback } from 'react';
import { Copy, RefreshCw } from 'lucide-react';

const COMMANDS = {
  'check-alert': {
    label: 'check-alert',
    description: 'Query alert status for a specific tenant',
    category: 'Prometheus API Tools',
    args: [
      { name: 'alert_name', label: 'Alert Name', required: true, placeholder: 'e.g., HighMemoryUsage' },
      { name: 'tenant', label: 'Tenant ID', required: true, placeholder: 'e.g., db-a' }
    ],
    flags: [
      { name: '--prometheus', label: 'Prometheus URL', required: true, placeholder: 'http://localhost:9090' }
    ]
  },
  'diagnose': {
    label: 'diagnose',
    description: 'Single-tenant comprehensive health check',
    category: 'Prometheus API Tools',
    args: [],
    flags: [
      { name: '--tenant', label: 'Tenant ID', required: true, placeholder: 'e.g., db-a' },
      { name: '--prometheus', label: 'Prometheus URL', required: false, placeholder: 'http://localhost:9090' },
      { name: '--config-dir', label: 'Config Directory', required: false, placeholder: '/etc/config' },
      { name: '--namespace', label: 'Kubernetes Namespace', required: false, placeholder: 'monitoring' }
    ]
  },
  'batch-diagnose': {
    label: 'batch-diagnose',
    description: 'Parallel health check for all tenants',
    category: 'Prometheus API Tools',
    args: [],
    flags: [
      { name: '--prometheus', label: 'Prometheus URL', required: true, placeholder: 'http://localhost:9090' },
      { name: '--tenants', label: 'Tenant List', required: false, placeholder: 'db-a,db-b,cache' },
      { name: '--workers', label: 'Worker Threads', required: false, placeholder: '4' }
    ]
  },
  'baseline': {
    label: 'baseline',
    description: 'Observe metrics, calculate stats (p50/p90/p95/p99/max), suggest thresholds',
    category: 'Prometheus API Tools',
    args: [],
    flags: [
      { name: '--tenant', label: 'Tenant ID', required: true, placeholder: 'e.g., db-a' },
      { name: '--prometheus', label: 'Prometheus URL', required: true, placeholder: 'http://localhost:9090' },
      { name: '--duration', label: 'Observation Duration (s)', required: false, placeholder: '3600' }
    ]
  },
  'validate': {
    label: 'validate',
    description: 'Shadow Monitoring validation: compare old vs new recording rules',
    category: 'Prometheus API Tools',
    args: [],
    flags: [
      { name: '--prometheus', label: 'Prometheus URL', required: true, placeholder: 'http://localhost:9090' },
      { name: '--watch', label: 'Watch Mode', required: false, type: 'checkbox' },
      { name: '--interval', label: 'Check Interval (s)', required: false, placeholder: '60' }
    ]
  },
  'cutover': {
    label: 'cutover',
    description: 'Shadow Monitoring one-click switch: disable old, enable new, verify health',
    category: 'Prometheus API Tools',
    args: [],
    flags: [
      { name: '--tenant', label: 'Tenant ID', required: true, placeholder: 'e.g., db-a' },
      { name: '--prometheus', label: 'Prometheus URL', required: true, placeholder: 'http://localhost:9090' },
      { name: '--readiness-json', label: 'Readiness JSON File', required: false, placeholder: '/tmp/readiness.json' }
    ]
  },
  'scaffold': {
    label: 'scaffold',
    description: 'Generate new tenant configuration',
    category: 'Filesystem Tools',
    args: [],
    flags: [
      { name: '--non-interactive', label: 'Non-Interactive Mode', required: false, type: 'checkbox' },
      { name: '--tenant', label: 'Tenant ID', required: false, placeholder: 'e.g., db-c' },
      { name: '--db', label: 'Database Type', required: false, placeholder: 'mysql,postgres,redis' }
    ]
  },
  'migrate': {
    label: 'migrate',
    description: 'Convert traditional Prometheus rules to dynamic format',
    category: 'Filesystem Tools',
    args: [
      { name: 'input', label: 'Input Rules File', required: true, placeholder: '/path/to/rules.yaml' }
    ],
    flags: [
      { name: '--output', label: 'Output Directory', required: false, placeholder: '/tmp/migrated' },
      { name: '--dry-run', label: 'Dry Run', required: false, type: 'checkbox' },
      { name: '--triage', label: 'Triage Mode', required: false, type: 'checkbox' }
    ]
  },
  'validate-config': {
    label: 'validate-config',
    description: 'One-stop config validation: YAML + schema + routing + policy + versions',
    category: 'Filesystem Tools',
    args: [],
    flags: [
      { name: '--config-dir', label: 'Config Directory', required: true, placeholder: '/etc/config' },
      { name: '--policy', label: 'Webhook Domain Policy', required: false, placeholder: '*.example.com' },
      { name: '--ci', label: 'CI Mode (exit codes)', required: false, type: 'checkbox' }
    ]
  },
  'generate-routes': {
    label: 'generate-routes',
    description: 'Generate Alertmanager routes + receivers + inhibit from tenant YAML',
    category: 'Configuration Generation',
    args: [],
    flags: [
      { name: '--config-dir', label: 'Config Directory', required: true, placeholder: '/etc/config' },
      { name: '--output', label: 'Output File', required: false, placeholder: '/tmp/routes.yaml' },
      { name: '--output-configmap', label: 'Output as ConfigMap', required: false, type: 'checkbox' }
    ]
  },
  'patch-config': {
    label: 'patch-config',
    description: 'Partial ConfigMap update with preview (--diff) and apply',
    category: 'Configuration Generation',
    args: [],
    flags: [
      { name: '--namespace', label: 'Kubernetes Namespace', required: true, placeholder: 'monitoring' },
      { name: '--configmap', label: 'ConfigMap Name', required: true, placeholder: 'alertmanager-config' },
      { name: '--dry-run', label: 'Dry Run / Diff Preview', required: false, type: 'checkbox' }
    ]
  }
};

const NETWORK_MODES = {
  'k8s': {
    label: 'K8s Internal (svc.cluster.local)',
    prometheus: 'http://prometheus.monitoring.svc.cluster.local:9090',
    network: ''
  },
  'docker-desktop': {
    label: 'Docker Desktop (host.docker.internal)',
    prometheus: 'http://host.docker.internal:9090',
    network: ''
  },
  'linux': {
    label: 'Linux Docker (--network=host)',
    prometheus: 'http://localhost:9090',
    network: '--network=host'
  }
};

// Build initial state for a command's args/flags
function initCommandState(cmdKey) {
  const cmd = COMMANDS[cmdKey];
  const a = {};
  const f = {};
  cmd.args.forEach(arg => { a[arg.name] = ''; });
  cmd.flags.forEach(flag => { f[flag.name] = flag.type === 'checkbox' ? false : ''; });
  return { args: a, flags: f };
}

export default function CLIPlayground() {
  const initial = initCommandState('check-alert');
  const [selectedCommand, setSelectedCommand] = useState('check-alert');
  const [isDocker, setIsDocker] = useState(true);
  const [networkMode, setNetworkMode] = useState('linux');
  const [args, setArgs] = useState(initial.args);
  const [flags, setFlags] = useState(initial.flags);
  const [copied, setCopied] = useState(false);

  const command = COMMANDS[selectedCommand];
  const network = NETWORK_MODES[networkMode];

  // Initialize args/flags when command changes
  const handleCommandChange = (cmdKey) => {
    setSelectedCommand(cmdKey);
    const state = initCommandState(cmdKey);
    setArgs(state.args);
    setFlags(state.flags);
  };

  const updateArg = (name, value) => {
    setArgs(prev => ({ ...prev, [name]: value }));
  };

  const updateFlag = (name, value) => {
    setFlags(prev => ({ ...prev, [name]: value }));
  };

  // Build the command string
  const buildCommand = () => {
    let cmd = '';

    if (isDocker) {
      cmd = 'docker run --rm ';
      if (network.network) cmd += network.network + ' ';
      cmd += `-e PROMETHEUS_URL=${network.prometheus} `;
      cmd += 'ghcr.io/vencil/da-tools:v1.11.0 ';
    } else {
      cmd = 'da-tools ';
    }

    cmd += selectedCommand;

    // Add positional arguments
    command.args.forEach(arg => {
      const value = args[arg.name];
      if (value) {
        cmd += ` ${value}`;
      }
    });

    // Add flags
    command.flags.forEach(flag => {
      const value = flags[flag.name];
      if (flag.type === 'checkbox') {
        if (value) cmd += ` ${flag.name}`;
      } else if (value) {
        // Skip Prometheus URL for docker mode (passed via env var)
        if (isDocker && flag.name === '--prometheus') return;
        cmd += ` ${flag.name} ${value}`;
      }
    });

    return cmd;
  };

  const copyCommand = () => {
    navigator.clipboard.writeText(buildCommand());
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const commandsByCategory = {};
  Object.entries(COMMANDS).forEach(([key, cmd]) => {
    if (!commandsByCategory[cmd.category]) {
      commandsByCategory[cmd.category] = [];
    }
    commandsByCategory[cmd.category].push({ key, ...cmd });
  });

  const requiredFlagsEmpty = command.flags
    .filter(f => f.required && f.type !== 'checkbox')
    .some(f => !flags[f.name]);
  const requiredArgsEmpty = command.args
    .filter(a => a.required)
    .some(a => !args[a.name]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-6xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold text-slate-900 mb-2">da-tools CLI Playground</h1>
          <p className="text-lg text-slate-600">Build and copy da-tools commands with a visual interface</p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
          {/* Command Selector */}
          <div className="lg:col-span-2">
            <div className="bg-white rounded-lg shadow-lg p-6 space-y-6">
              {/* Execution Mode Toggle */}
              <div>
                <h3 className="text-sm font-semibold text-slate-900 mb-3">Execution Mode</h3>
                <div className="flex gap-3">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      checked={isDocker}
                      onChange={() => setIsDocker(true)}
                      className="w-4 h-4"
                    />
                    <span className="text-slate-700">Docker Container</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      checked={!isDocker}
                      onChange={() => setIsDocker(false)}
                      className="w-4 h-4"
                    />
                    <span className="text-slate-700">Direct CLI</span>
                  </label>
                </div>
              </div>

              {/* Network Mode (Docker only) */}
              {isDocker && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">Network Configuration</h3>
                  <select
                    value={networkMode}
                    onChange={(e) => setNetworkMode(e.target.value)}
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-900"
                  >
                    {Object.entries(NETWORK_MODES).map(([key, mode]) => (
                      <option key={key} value={key}>{mode.label}</option>
                    ))}
                  </select>
                  <p className="text-xs text-slate-500 mt-2">
                    Prometheus: <code className="bg-slate-100 px-1 rounded">{network.prometheus}</code>
                  </p>
                </div>
              )}

              {/* Command Selection */}
              <div>
                <h3 className="text-sm font-semibold text-slate-900 mb-3">Select Command</h3>
                <div className="space-y-2">
                  {Object.entries(commandsByCategory).map(([category, cmds]) => (
                    <div key={category}>
                      <p className="text-xs font-semibold text-slate-600 uppercase tracking-wide mb-2">{category}</p>
                      <div className="space-y-1 mb-4">
                        {cmds.map(cmd => (
                          <button
                            key={cmd.key}
                            onClick={() => handleCommandChange(cmd.key)}
                            className={`w-full text-left px-3 py-2 rounded text-sm transition-colors ${
                              selectedCommand === cmd.key
                                ? 'bg-blue-600 text-white font-medium'
                                : 'bg-slate-100 text-slate-900 hover:bg-slate-200'
                            }`}
                          >
                            {cmd.label}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Command Description */}
              <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg">
                <p className="text-sm text-blue-900">{command.description}</p>
              </div>

              {/* Arguments */}
              {command.args.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">Arguments</h3>
                  <div className="space-y-3">
                    {command.args.map(arg => (
                      <div key={arg.name}>
                        <label className="text-xs font-medium text-slate-700 block mb-1">
                          {arg.label} {arg.required && <span className="text-red-600">*</span>}
                        </label>
                        <input
                          type="text"
                          value={args[arg.name] || ''}
                          onChange={(e) => updateArg(arg.name, e.target.value)}
                          placeholder={arg.placeholder}
                          className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-900 text-sm"
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Flags */}
              {command.flags.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">Options</h3>
                  <div className="space-y-3">
                    {command.flags.map(flag => (
                      <div key={flag.name}>
                        {flag.type === 'checkbox' ? (
                          <label className="flex items-center gap-2 cursor-pointer">
                            <input
                              type="checkbox"
                              checked={flags[flag.name] || false}
                              onChange={(e) => updateFlag(flag.name, e.target.checked)}
                              className="w-4 h-4 rounded"
                            />
                            <span className="text-sm text-slate-700">{flag.label}</span>
                          </label>
                        ) : (
                          <>
                            <label className="text-xs font-medium text-slate-700 block mb-1">
                              {flag.label} {flag.required && <span className="text-red-600">*</span>}
                            </label>
                            <input
                              type="text"
                              value={flags[flag.name] || ''}
                              onChange={(e) => updateFlag(flag.name, e.target.value)}
                              placeholder={flag.placeholder}
                              className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-900 text-sm"
                            />
                          </>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Command Output & Summary */}
          <div className="lg:col-span-1">
            <div className="sticky top-8 space-y-4">
              {/* Command Output */}
              <div className="bg-white rounded-lg shadow-lg p-6">
                <h3 className="text-lg font-semibold text-slate-900 mb-4">Command</h3>
                <div className="relative">
                  <pre className="bg-slate-900 text-slate-100 p-4 rounded text-xs overflow-x-auto break-words whitespace-pre-wrap max-h-64 overflow-y-auto font-mono">
                    {buildCommand()}
                  </pre>
                  <button
                    onClick={copyCommand}
                    disabled={requiredArgsEmpty || requiredFlagsEmpty}
                    className={`absolute top-2 right-2 p-2 rounded transition-colors ${
                      copied
                        ? 'bg-green-500 text-white'
                        : 'bg-slate-700 text-slate-200 hover:bg-slate-600 disabled:opacity-50 disabled:cursor-not-allowed'
                    }`}
                    title="Copy to clipboard"
                  >
                    <Copy size={16} />
                  </button>
                </div>
                {copied && (
                  <p className="mt-2 text-sm text-green-600 font-medium">✓ Copied to clipboard</p>
                )}
                {(requiredArgsEmpty || requiredFlagsEmpty) && (
                  <p className="mt-2 text-xs text-amber-600">Fill required fields to enable copy</p>
                )}
              </div>

              {/* Environment Info */}
              <div className="bg-white rounded-lg shadow-lg p-6 text-sm">
                <h3 className="font-semibold text-slate-900 mb-3">Environment</h3>
                <div className="space-y-2 text-slate-600 text-xs">
                  <div>
                    <span className="font-medium text-slate-900">Mode:</span> {isDocker ? 'Docker Container' : 'Direct CLI'}
                  </div>
                  {isDocker && (
                    <>
                      <div>
                        <span className="font-medium text-slate-900">Image:</span> ghcr.io/vencil/da-tools:v1.11.0
                      </div>
                      <div>
                        <span className="font-medium text-slate-900">Network:</span> {network.label}
                      </div>
                    </>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
