---
title: "Rule Pack Selector"
tags: [rule-packs, interactive, tools]
audience: [platform-engineer, sre]
version: v2.0.0-preview.2
lang: en
---

import React, { useState, useCallback } from 'react';
import { Copy, ChevronDown } from 'lucide-react';

// Rule pack metadata
const RULE_PACKS = {
  mariadb: {
    configMap: 'prometheus-rules-mariadb',
    label: 'MariaDB/MySQL',
    recordingRules: 11,
    alertRules: 8,
    category: 'database'
  },
  postgresql: {
    configMap: 'prometheus-rules-postgresql',
    label: 'PostgreSQL',
    recordingRules: 11,
    alertRules: 8,
    category: 'database'
  },
  redis: {
    configMap: 'prometheus-rules-redis',
    label: 'Redis',
    recordingRules: 11,
    alertRules: 6,
    category: 'database'
  },
  mongodb: {
    configMap: 'prometheus-rules-mongodb',
    label: 'MongoDB',
    recordingRules: 10,
    alertRules: 6,
    category: 'database'
  },
  elasticsearch: {
    configMap: 'prometheus-rules-elasticsearch',
    label: 'Elasticsearch',
    recordingRules: 11,
    alertRules: 7,
    category: 'database'
  },
  oracle: {
    configMap: 'prometheus-rules-oracle',
    label: 'Oracle',
    recordingRules: 11,
    alertRules: 7,
    category: 'database'
  },
  db2: {
    configMap: 'prometheus-rules-db2',
    label: 'DB2',
    recordingRules: 12,
    alertRules: 7,
    category: 'database'
  },
  clickhouse: {
    configMap: 'prometheus-rules-clickhouse',
    label: 'ClickHouse',
    recordingRules: 12,
    alertRules: 7,
    category: 'database'
  },
  kafka: {
    configMap: 'prometheus-rules-kafka',
    label: 'Kafka',
    recordingRules: 11,
    alertRules: 10,
    category: 'messaging'
  },
  rabbitmq: {
    configMap: 'prometheus-rules-rabbitmq',
    label: 'RabbitMQ',
    recordingRules: 11,
    alertRules: 10,
    category: 'messaging'
  },
  jvm: {
    configMap: 'prometheus-rules-jvm',
    label: 'JVM (Java applications)',
    recordingRules: 10,
    alertRules: 8,
    category: 'runtime'
  },
  nginx: {
    configMap: 'prometheus-rules-nginx',
    label: 'Nginx',
    recordingRules: 8,
    alertRules: 6,
    category: 'webserver'
  },
  kubernetes: {
    configMap: 'prometheus-rules-kubernetes',
    label: 'Kubernetes (infrastructure)',
    recordingRules: 7,
    alertRules: 4,
    category: 'infrastructure'
  },
  // Always included
  operational: {
    configMap: 'prometheus-rules-operational',
    label: 'Operational (Always included)',
    recordingRules: 0,
    alertRules: 2,
    category: 'infrastructure',
    required: true
  },
  platform: {
    configMap: 'prometheus-rules-platform',
    label: 'Platform (Always included)',
    recordingRules: 0,
    alertRules: 4,
    category: 'infrastructure',
    required: true
  }
};

const CATEGORIES = {
  database: 'Databases',
  messaging: 'Messaging',
  runtime: 'Runtime Environments',
  webserver: 'Web Servers',
  infrastructure: 'Infrastructure'
};

export default function RulePackSelector() {
  const [selected, setSelected] = useState(new Set());
  const [expandedPacks, setExpandedPacks] = useState(new Set());
  const [copied, setCopied] = useState(false);

  const toggleSelection = useCallback((packKey) => {
    const newSelected = new Set(selected);
    if (newSelected.has(packKey)) {
      newSelected.delete(packKey);
    } else {
      newSelected.add(packKey);
    }
    setSelected(newSelected);
  }, [selected]);

  const toggleExpandPack = useCallback((packKey) => {
    const newExpanded = new Set(expandedPacks);
    if (newExpanded.has(packKey)) {
      newExpanded.delete(packKey);
    } else {
      newExpanded.add(packKey);
    }
    setExpandedPacks(newExpanded);
  }, [expandedPacks]);

  // Calculate statistics
  const selectedPacks = Array.from(selected).map(key => RULE_PACKS[key]);
  const alwaysIncludedPacks = ['operational', 'platform'].map(key => RULE_PACKS[key]);
  const allActivePacks = [...selectedPacks, ...alwaysIncludedPacks];

  const totalRecordingRules = allActivePacks.reduce((sum, pack) => sum + pack.recordingRules, 0);
  const totalAlertRules = allActivePacks.reduce((sum, pack) => sum + pack.alertRules, 0);
  const totalRulePacks = allActivePacks.length;

  // Generate YAML snippets
  const generateProjectedVolume = () => {
    const configMapNames = Array.from(selected).map(key => RULE_PACKS[key].configMap)
      .concat(['prometheus-rules-operational', 'prometheus-rules-platform'])
      .sort();

    return `  - name: prometheus-rule-volumes
    projected:
      sources:
${configMapNames.map(cm => `        - configMap:
            name: ${cm}
            items:
              - key: rules
                path: ${cm}.yaml`).join('\n')}`;
  };

  const generatePrometheusRuleFiles = () => {
    const configMapNames = Array.from(selected).map(key => RULE_PACKS[key].configMap)
      .concat(['prometheus-rules-operational', 'prometheus-rules-platform'])
      .sort();

    return configMapNames.map(cm => `      - /etc/prometheus/rules/${cm}.yaml`)
      .join('\n');
  };

  const yamlOutput = `# Prometheus Rule Packs Configuration
# Auto-generated from Rule Pack Selector

# Add to prometheus-deployment.yaml spec.template.spec.volumes:
${generateProjectedVolume()}

# Add to prometheus.yml rule_files:
rule_files:
${generatePrometheusRuleFiles()}`;

  const copyToClipboard = () => {
    navigator.clipboard.writeText(yamlOutput);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const groupedPacks = Object.entries(CATEGORIES).reduce((acc, [category, label]) => {
    const packs = Object.entries(RULE_PACKS)
      .filter(([_, pack]) => pack.category === category && !pack.required)
      .map(([key, pack]) => ({ key, ...pack }));
    if (packs.length > 0) {
      acc[label] = packs;
    }
    return acc;
  }, {});

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-6xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold text-slate-900 mb-2">Rule Pack Selector</h1>
          <p className="text-lg text-slate-600">Configure Prometheus monitoring for your infrastructure</p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
          {/* Selector Panel */}
          <div className="lg:col-span-2">
            <div className="bg-white rounded-lg shadow-lg p-6">
              <h2 className="text-2xl font-semibold text-slate-900 mb-6">Select Services</h2>
              <p className="text-slate-600 mb-6">Choose which services and databases you monitor:</p>

              {/* Always Included Banner */}
              <div className="mb-8 p-4 bg-blue-50 border border-blue-200 rounded-lg">
                <p className="text-sm font-semibold text-blue-900">Always Included</p>
                <p className="text-sm text-blue-700">Operational and Platform rule packs are always included (infrastructure essentials)</p>
              </div>

              {/* Service Categories */}
              <div className="space-y-8">
                {Object.entries(groupedPacks).map(([categoryLabel, packs]) => (
                  <div key={categoryLabel}>
                    <h3 className="text-sm font-semibold text-slate-700 uppercase tracking-wide mb-4">{categoryLabel}</h3>
                    <div className="space-y-3">
                      {packs.map(pack => (
                        <div key={pack.key} className="flex items-start gap-3">
                          <input
                            type="checkbox"
                            id={pack.key}
                            checked={selected.has(pack.key)}
                            onChange={() => toggleSelection(pack.key)}
                            className="w-5 h-5 mt-0.5 text-blue-600 rounded border-slate-300 focus:ring-2 focus:ring-blue-500 cursor-pointer"
                          />
                          <label htmlFor={pack.key} className="flex-1 cursor-pointer">
                            <div className="flex items-center justify-between">
                              <span className="font-medium text-slate-900">{pack.label}</span>
                              <button
                                onClick={() => toggleExpandPack(pack.key)}
                                className="text-slate-500 hover:text-slate-700 transition-colors"
                                aria-label="Show details"
                              >
                                <ChevronDown
                                  size={16}
                                  style={{
                                    transition: 'transform 0.2s',
                                    transform: expandedPacks.has(pack.key) ? 'rotate(180deg)' : 'none'
                                  }}
                                />
                              </button>
                            </div>
                            {expandedPacks.has(pack.key) && (
                              <div className="mt-2 text-sm text-slate-600 bg-slate-50 p-3 rounded">
                                <div className="flex gap-4">
                                  <span>📋 Recording: <strong>{pack.recordingRules}</strong></span>
                                  <span>🚨 Alerts: <strong>{pack.alertRules}</strong></span>
                                </div>
                                <div className="mt-2 text-xs text-slate-500">
                                  ConfigMap: <code className="bg-slate-100 px-2 py-1 rounded">{pack.configMap}</code>
                                </div>
                              </div>
                            )}
                          </label>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Summary & Output Panel */}
          <div className="lg:col-span-1">
            <div className="sticky top-8 space-y-4">
              {/* Statistics Card */}
              <div className="bg-white rounded-lg shadow-lg p-6">
                <h3 className="text-lg font-semibold text-slate-900 mb-4">Summary</h3>
                <div className="space-y-3">
                  <div className="flex justify-between items-center">
                    <span className="text-slate-600">Rule Packs:</span>
                    <span className="text-2xl font-bold text-blue-600">{totalRulePacks}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-slate-600">Recording Rules:</span>
                    <span className="text-2xl font-bold text-green-600">{totalRecordingRules}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-slate-600">Alert Rules:</span>
                    <span className="text-2xl font-bold text-red-600">{totalAlertRules}</span>
                  </div>
                </div>

                {/* Rule Pack List */}
                {allActivePacks.length > 0 && (
                  <div className="mt-6 pt-6 border-t border-slate-200">
                    <p className="text-xs font-semibold text-slate-700 uppercase mb-3">Active Packs</p>
                    <div className="space-y-1">
                      {allActivePacks.map(pack => (
                        <div key={pack.configMap} className="flex justify-between text-xs">
                          <span className="text-slate-600">{pack.label}</span>
                          <span className="text-slate-500">
                            {pack.recordingRules}R + {pack.alertRules}A
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* YAML Output Card */}
              <div className="bg-white rounded-lg shadow-lg p-6">
                <h3 className="text-lg font-semibold text-slate-900 mb-4">YAML Configuration</h3>
                <div className="relative">
                  <pre className="bg-slate-900 text-slate-100 p-4 rounded text-xs overflow-x-auto max-h-64 overflow-y-auto">
{yamlOutput}
                  </pre>
                  <button
                    onClick={copyToClipboard}
                    className={`absolute top-2 right-2 p-2 rounded transition-colors ${
                      copied
                        ? 'bg-green-500 text-white'
                        : 'bg-slate-700 text-slate-200 hover:bg-slate-600'
                    }`}
                    title="Copy to clipboard"
                  >
                    <Copy size={16} />
                  </button>
                </div>
                {copied && (
                  <p className="mt-2 text-sm text-green-600 font-medium">✓ Copied to clipboard</p>
                )}
                <p className="mt-4 text-xs text-slate-600">
                  Apply this configuration to your Prometheus deployment and restart the service.
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
