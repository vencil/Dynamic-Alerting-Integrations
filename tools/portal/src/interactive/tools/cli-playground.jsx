---
title: "da-tools CLI Playground"
tags: [cli, da-tools, docker]
audience: ["platform-engineer"]
version: v2.7.0
lang: en
related: [wizard, onboarding-checklist, glossary]
---

import React, { useState, useCallback } from 'react';
import { Copy, RefreshCw } from 'lucide-react';
import { useCopyToClipboard } from './_common/hooks/useCopyToClipboard.js';

import { COMMANDS, NETWORK_MODES } from './cli-playground/commands.js';
import { initCommandState, readHashCmd, buildCommand } from './cli-playground/engine.js';
const t = window.__t || ((zh, en) => en);

export default function CLIPlayground() {
  const initialCmd = readHashCmd();
  const initial = initCommandState(initialCmd);
  const [selectedCommand, setSelectedCommand] = useState(initialCmd);
  const [isDocker, setIsDocker] = useState(true);
  const [networkMode, setNetworkMode] = useState('linux');
  const [args, setArgs] = useState(initial.args);
  const [flags, setFlags] = useState(initial.flags);
  const { copied, copy } = useCopyToClipboard();
  const [searchFilter, setSearchFilter] = useState('');
  const [showPopularOnly, setShowPopularOnly] = useState(false);

  const command = COMMANDS[selectedCommand];
  const network = NETWORK_MODES[networkMode];

  // Initialize args/flags when command changes
  const handleCommandChange = (cmdKey) => {
    setSelectedCommand(cmdKey);
    const state = initCommandState(cmdKey);
    setArgs(state.args);
    setFlags(state.flags);
    window.history.replaceState(null, '', '#cmd=' + cmdKey);
  };

  const updateArg = (name, value) => {
    setArgs(prev => ({ ...prev, [name]: value }));
  };

  const updateFlag = (name, value) => {
    setFlags(prev => ({ ...prev, [name]: value }));
  };

  // Build the command string once per render from the current selection.
  // buildCommand is the pure engine helper (state is stable within a render).
  const builtCommand = buildCommand({ isDocker, network, selectedCommand, command, args, flags });

  const copyCommand = () => copy(builtCommand);

  const commandsByCategory = {};
  Object.entries(COMMANDS).forEach(([key, cmd]) => {
    // Apply search filter
    const q = searchFilter.toLowerCase();
    if (q && !cmd.label.toLowerCase().includes(q) && !cmd.description.toLowerCase().includes(q) && !cmd.category.toLowerCase().includes(q)) return;
    // Apply popular filter
    if (showPopularOnly && !cmd.popular) return;
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
          <h1 className="text-4xl font-bold text-slate-900 mb-2">{t('da-tools CLI 遊樂場', 'da-tools CLI Playground')}</h1>
          <p className="text-lg text-slate-600">{t('使用視覺介面建立和複製 da-tools 命令', 'Build and copy da-tools commands with a visual interface')}</p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
          {/* Command Selector */}
          <div className="lg:col-span-2">
            <div className="bg-white rounded-lg shadow-lg p-6 space-y-6">
              {/* Execution Mode Toggle */}
              <div>
                <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('執行模式', 'Execution Mode')}</h3>
                <div className="flex gap-3">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      checked={isDocker}
                      onChange={() => setIsDocker(true)}
                      className="w-4 h-4"
                    />
                    <span className="text-slate-700">{t('Docker 容器', 'Docker Container')}</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      checked={!isDocker}
                      onChange={() => setIsDocker(false)}
                      className="w-4 h-4"
                    />
                    <span className="text-slate-700">{t('直接 CLI', 'Direct CLI')}</span>
                  </label>
                </div>
              </div>

              {/* Network Mode (Docker only) */}
              {isDocker && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('網路配置', 'Network Configuration')}</h3>
                  <select
                    value={networkMode}
                    onChange={(e) => setNetworkMode(e.target.value)}
                    aria-label={t('網路配置', 'Network Configuration')}
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-900"
                  >
                    {Object.entries(NETWORK_MODES).map(([key, mode]) => (
                      <option key={key} value={key}>{mode.label}</option>
                    ))}
                  </select>
                  <p className="text-xs text-slate-500 mt-2">
                    {t('Prometheus:', 'Prometheus:')} <code className="bg-slate-100 px-1 rounded">{network.prometheus}</code>
                  </p>
                </div>
              )}

              {/* Command Selection */}
              <div>
                <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('選擇命令', 'Select Command')}</h3>
                <div className="flex gap-2 mb-3">
                  <input
                    type="text"
                    value={searchFilter}
                    onChange={(e) => setSearchFilter(e.target.value)}
                    placeholder={t('搜尋命令...', 'Search commands...')}
                    className="flex-1 px-3 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-900 text-sm"
                  />
                  <button
                    onClick={() => setShowPopularOnly(!showPopularOnly)}
                    className={`px-3 py-2 rounded-lg text-xs font-medium transition-colors whitespace-nowrap ${
                      showPopularOnly ? 'bg-amber-100 text-amber-800 border border-amber-300' : 'bg-slate-100 text-slate-600 border border-slate-300 hover:bg-slate-200'
                    }`}
                  >
                    ★ {t('熱門', 'Popular')}
                  </button>
                </div>
                {Object.keys(commandsByCategory).length === 0 && (
                  <p className="text-sm text-slate-500 py-4 text-center">{t('找不到與您的搜尋相符的命令。', 'No commands match your search.')}</p>
                )}
                <div className="space-y-2">
                  {Object.entries(commandsByCategory).map(([category, cmds]) => (
                    <div key={category}>
                      <p className="text-xs font-semibold text-slate-600 uppercase tracking-wide mb-2">{category}</p>
                      <div className="space-y-1 mb-4">
                        {cmds.map(cmd => (
                          <button
                            key={cmd.key}
                            onClick={() => handleCommandChange(cmd.key)}
                            className={`w-full text-left px-3 py-2 rounded text-sm transition-colors flex items-center gap-2 ${
                              selectedCommand === cmd.key
                                ? 'bg-blue-600 text-white font-medium'
                                : 'bg-slate-100 text-slate-900 hover:bg-slate-200'
                            }`}
                          >
                            <span className="flex-1">{cmd.label}</span>
                            {cmd.popular && <span className="text-amber-500 text-xs" title={t('常用命令', 'Commonly used')}>★</span>}
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
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('引數', 'Arguments')}</h3>
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
                  <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('選項', 'Options')}</h3>
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
                <h3 className="text-lg font-semibold text-slate-900 mb-4">{t('命令', 'Command')}</h3>
                <div className="relative">
                  <pre className="bg-slate-900 text-slate-100 p-4 rounded text-xs overflow-x-auto break-words whitespace-pre-wrap max-h-64 overflow-y-auto font-mono">
                    {builtCommand}
                  </pre>
                  <button
                    onClick={copyCommand}
                    disabled={requiredArgsEmpty || requiredFlagsEmpty}
                    className={`absolute top-2 right-2 p-2 rounded transition-colors ${
                      copied
                        ? 'bg-green-500 text-white'
                        : 'bg-slate-700 text-slate-200 hover:bg-slate-600 disabled:opacity-50 disabled:cursor-not-allowed'
                    }`}
                    title={t('複製到剪貼板', 'Copy to clipboard')}
                  >
                    <Copy size={16} />
                  </button>
                </div>
                {copied && (
                  <p className="mt-2 text-sm text-green-600 font-medium"><span aria-hidden="true">✓</span> {t('已複製到剪貼板', 'Copied to clipboard')}</p>
                )}
                {(requiredArgsEmpty || requiredFlagsEmpty) && (
                  <p className="mt-2 text-xs text-amber-600">{t('填寫必填欄位以啟用複製', 'Fill required fields to enable copy')}</p>
                )}
              </div>

              {/* Sample Output Preview */}
              {command.preview && (
                <div className="bg-white rounded-lg shadow-lg p-6">
                  <h3 className="text-sm font-semibold text-slate-900 mb-3 flex items-center gap-2">
                    <span className="text-green-500">▶</span> {t('範例輸出', 'Sample Output')}
                  </h3>
                  <pre className="bg-slate-900 text-green-400 p-4 rounded text-xs overflow-x-auto whitespace-pre-wrap max-h-56 overflow-y-auto font-mono leading-relaxed">
                    {command.preview}
                  </pre>
                  <p className="text-xs text-slate-400 mt-2 italic">{t('模擬輸出 — 實際結果取決於您的環境。', 'Simulated output — actual results depend on your environment.')}</p>
                </div>
              )}

              {/* Environment Info */}
              <div className="bg-white rounded-lg shadow-lg p-6 text-sm">
                <h3 className="font-semibold text-slate-900 mb-3">{t('環境', 'Environment')}</h3>
                <div className="space-y-2 text-slate-600 text-xs">
                  <div>
                    <span className="font-medium text-slate-900">{t('模式:', 'Mode:')}</span> {isDocker ? t('Docker 容器', 'Docker Container') : t('直接 CLI', 'Direct CLI')}
                  </div>
                  {isDocker && (
                    <>
                      <div>
                        <span className="font-medium text-slate-900">{t('映像:', 'Image:')}</span> ghcr.io/vencil/da-tools:v2.7.0
                      </div>
                      <div>
                        <span className="font-medium text-slate-900">{t('網路:', 'Network:')}</span> {network.label}
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
