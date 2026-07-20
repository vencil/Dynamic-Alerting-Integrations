---
title: "Getting Started Wizard"
tags: [onboarding, guided, 3 min]
audience: [tenant, platform-engineer, domain-expert]
version: v2.7.0
lang: en
related: [onboarding-checklist, architecture-quiz, rule-pack-selector]
---

import { useState, useEffect, useRef } from "react";
import {
  docUrl,
  pathsForRole,
  diffDocPaths,
  readHash,
  writeHash,
  recommendationKeyFor,
} from "./wizard/engine.js";
import {
  GLOSSARY,
  ROLES,
  GOALS,
  DATABASES,
  NEEDS,
  BUCKET_LABELS,
  ROLE_AXIS,
  HANDOFF_TARGETS,
  RECOMMENDATIONS,
} from "./wizard/fixtures.js";

// Style tokens (v2.7.0 Phase .a0 DEC-A Option A migration):
// - Core gray/blue/focus palette migrated to Tailwind arbitrary values
//   (bg-[color:var(--da-color-*)] / text-[color:...] / border-[color:...])
//   so theme switching via [data-theme="dark"] works automatically.
// - State-specific colors (green = completed/success, amber = priority,
//   indigo/purple = path comparison) remain as Tailwind utilities pending
//   introduction of domain-specific semantic tokens in a future audit.
// - See design-critique notes in docs/internal/design-reviews/v2.7.0/wizard.md.

// i18n helper — picks zh or en based on jsx-loader's detected language.
//
// This tool ships as a PRE-BUILT esbuild dist bundle (docs/assets/dist/
// wizard.js). The language toggle in jsx-loader.html does a FULL PAGE
// RELOAD (not an in-place React re-mount) — see jsx-loader.html
// setLanguage(). On reload the module is re-evaluated, so this module-level
// `const t` is captured with `window.__t` already set at bootstrap. That is
// why reading `window.__t` once here (rather than via a hook on every
// render) is correct: there is no in-session language flip to react to.
const t = window.__t || ((zh, en) => en);

const GlossaryTip = ({ term }) => {
  const [show, setShow] = useState(false);
  const def = GLOSSARY[term];
  if (!def) return <span className="font-semibold">{term}</span>;
  return (
    <span className="relative inline-block">
      <button
        type="button"
        onClick={() => setShow(!show)}
        aria-expanded={show}
        className="font-semibold text-[color:var(--da-color-accent)] underline decoration-dotted underline-offset-4 cursor-help"
      >
        {term}
      </button>
      {show && (
        <span className="absolute z-10 left-0 top-full mt-1 w-64 p-3 bg-[color:var(--da-color-toast-bg)] text-[color:var(--da-color-hero-fg)] text-xs rounded-lg shadow-lg leading-relaxed">
          {def}
          <button type="button" onClick={() => setShow(false)} className="block mt-1 text-[color:var(--da-color-link-on-dark)] text-xs hover:underline">{t("關閉", "close")}</button>
        </span>
      )}
    </span>
  );
};

const ProgressIndicator = ({ step, totalSteps }) => {
  // Step names so the state is also conveyed in the accessible name (a11y:
  // the active step is not signalled by colour/ring alone — aria-current +
  // a "(done)/(current)" suffix carry it for screen readers).
  const stepNames = [t("角色", "Role"), t("選項", "Options"), t("文件", "Docs")];
  const items = [];
  for (let i = 0; i < totalSteps; i++) {
    const stateLabel = i < step ? t("已完成", "done") : i === step ? t("目前", "current") : t("未開始", "upcoming");
    const name = stepNames[i] || (i + 1);
    items.push(
      <div
        key={`circle-${i}`}
        aria-current={i === step ? "step" : undefined}
        aria-label={`${i + 1}. ${name} (${stateLabel})`}
        className={`flex-shrink-0 flex items-center justify-center w-10 h-10 rounded-full font-bold transition-all ${
          i < step
            ? "bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)]"
            : i === step
              ? "bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)] ring-4 ring-[color:var(--da-color-focus-ring)]"
              : "bg-[color:var(--da-color-surface-border)] text-[color:var(--da-color-muted)]"
        }`}
      >
        {i < step ? <span aria-hidden="true">✓</span> : i + 1}
      </div>
    );
    if (i < totalSteps - 1) {
      items.push(
        <div
          key={`bar-${i}`}
          aria-hidden="true"
          className={`flex-1 h-1 mx-2 rounded transition-all ${
            i < step ? "bg-[color:var(--da-color-accent)]" : "bg-[color:var(--da-color-surface-border)]"
          }`}
        />
      );
    }
  }
  return (
    <nav aria-label={t("進度", "Progress")} className="flex items-center mb-8">
      {items}
    </nav>
  );
};

const RoleCard = ({ role, isSelected, onClick }) => {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={isSelected}
      className={`p-6 rounded-lg border-2 transition-all text-left hover:shadow-lg ${
        isSelected
          ? "border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] shadow-lg"
          : "border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] hover:border-[color:var(--da-color-accent)]"
      }`}
    >
      <div className="text-3xl mb-3" aria-hidden="true">{role.icon}</div>
      <h3 className="text-lg font-bold text-[color:var(--da-color-fg)] mb-2">{role.label}</h3>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-2">{role.desc}</p>
      {role.hint && <p className="text-xs text-[color:var(--da-color-muted)] italic">{role.hint}</p>}
    </button>
  );
};

const OptionCard = ({ option, isSelected, onClick, icon = null }) => {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={isSelected}
      className={`p-4 rounded-lg border-2 transition-all text-left hover:shadow-md ${
        isSelected
          ? "border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)]"
          : "border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] hover:border-[color:var(--da-color-accent)]"
      }`}
    >
      {icon && <div className="text-2xl mb-2" aria-hidden="true">{icon}</div>}
      <h3 className="text-base font-semibold text-[color:var(--da-color-fg)] mb-1">
        {option.label}
      </h3>
      {option.desc && (
        <p className="text-sm text-[color:var(--da-color-muted)]">{option.desc}</p>
      )}
    </button>
  );
};

const DocumentLink = ({ doc, isRead, onToggleRead }) => {
  const isPriority = doc.priority === "start-here";
  const href = docUrl(doc.path);
  return (
    <div className={`flex items-center gap-3 p-4 rounded-lg border transition-all hover:shadow-md ${
      isRead ? "border-[color:var(--da-color-success)] bg-[color:var(--da-color-success-soft)]" : isPriority ? "border-[color:var(--da-color-warning)] bg-[color:var(--da-color-warning-soft)] hover:bg-[color:var(--da-color-warning-soft)]" : "border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] hover:bg-[color:var(--da-color-surface-hover)]"
    }`}>
      <button
        type="button"
        onClick={(e) => { e.preventDefault(); onToggleRead(doc.path); }}
        className={`flex-shrink-0 w-6 h-6 rounded border-2 flex items-center justify-center transition-colors ${
          isRead ? "bg-[color:var(--da-color-success)] border-[color:var(--da-color-success)] text-[color:var(--da-color-accent-fg)]" : "border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-accent)]"
        }`}
        title={isRead ? t("標記為未讀", "Mark as unread") : t("標記為已讀", "Mark as read")}
      >
        {isRead && <span className="text-xs font-bold"><span aria-hidden="true">✓</span></span>}
      </button>
      <a href={href} target="_blank" rel="noopener noreferrer" className="flex-1 min-w-0">
        <div className="flex items-start justify-between">
          <div className="flex-1">
            <h4 className={`font-semibold text-sm ${isRead ? "text-[color:var(--da-color-success)] line-through" : "text-[color:var(--da-color-fg)]"}`}>
              {doc.name}
              {isPriority && !isRead && (
                <span className="ml-2 inline-block px-2 py-1 bg-[color:var(--da-color-warning-soft)] text-[color:var(--da-color-warning)] text-xs font-bold rounded">
                  {t("從這開始", "START HERE")}
                </span>
              )}
            </h4>
            {doc.summary && (
              <p className="text-xs text-[color:var(--da-color-muted)] mt-1 leading-relaxed">{doc.summary}</p>
            )}
          </div>
          <div className="ml-3 text-lg">→</div>
        </div>
      </a>
    </div>
  );
};

const PathCompare = ({ currentKey, role, onClose }) => {
  const [compareKey, setCompareKey] = useState(null);
  const currentRec = RECOMMENDATIONS[currentKey];
  const compareRec = compareKey ? RECOMMENDATIONS[compareKey] : null;

  const { shared: sharedDocs, onlyA, onlyB } = diffDocPaths(currentRec, compareRec);

  return (
    <div className="bg-[color:var(--da-color-surface)] rounded-lg shadow-sm border border-[color:var(--da-color-accent-border-soft)] p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-bold text-[color:var(--da-color-fg)]">{t('路徑比較', 'Compare Paths')}</h3>
        <button type="button" onClick={onClose} className="text-[color:var(--da-color-muted)] hover:text-[color:var(--da-color-muted)] text-sm"><span aria-hidden="true">✕</span> {t('關閉', 'Close')}</button>
      </div>
      <div>
        <label className="text-sm font-medium text-[color:var(--da-color-fg)] block mb-2">{t('選擇另一條路徑比較：', 'Compare with another path:')}</label>
        <select
          aria-label={t('選擇另一條路徑比較', 'Compare with another path')}
          value={compareKey || ''}
          onChange={(e) => setCompareKey(e.target.value || null)}
          className="w-full px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
        >
          <option value="">{t('-- 選擇路徑 --', '-- Select a path --')}</option>
          {pathsForRole(role, RECOMMENDATIONS).filter(p => p.key !== currentKey).map(p => (
            <option key={p.key} value={p.key}>{p.label}</option>
          ))}
        </select>
      </div>
      {compareRec && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
          <div>
            <h4 className="font-semibold text-[color:var(--da-color-accent-hover)] mb-2">{t('僅在當前路徑', 'Only in your path')} ({onlyA.length})</h4>
            {onlyA.map(d => (
              <div key={d.path} className="py-1 text-[color:var(--da-color-fg)]">{d.name}</div>
            ))}
            {onlyA.length === 0 && <div className="text-[color:var(--da-color-muted)] italic">{t('無', 'None')}</div>}
          </div>
          <div>
            <h4 className="font-semibold text-[color:var(--da-color-success)] mb-2">{t('共同文件', 'Shared')} ({sharedDocs.length})</h4>
            {sharedDocs.map(d => (
              <div key={d.path} className="py-1 text-[color:var(--da-color-fg)]">{d.name}</div>
            ))}
          </div>
          <div>
            <h4 className="font-semibold text-[color:var(--da-color-semantic-other)] mb-2">{t('僅在比較路徑', 'Only in compared path')} ({onlyB.length})</h4>
            {onlyB.map(d => (
              <div key={d.path} className="py-1 text-[color:var(--da-color-fg)]">{d.name}</div>
            ))}
            {onlyB.length === 0 && <div className="text-[color:var(--da-color-muted)] italic">{t('無', 'None')}</div>}
          </div>
        </div>
      )}
    </div>
  );
};

const RecommendationsSummary = ({ recommendations, readDocs, onToggleRead, headingRef }) => {
  const total = recommendations.docs.length;
  const done = recommendations.docs.filter(d => readDocs.has(d.path)).length;
  const progressStyle = { width: (total > 0 ? (done / total) * 100 : 0) + '%' };
  return (
    <div className="space-y-6">
      <div className="bg-[color:var(--da-color-accent-soft)] border border-[color:var(--da-color-accent)] rounded-lg p-6">
        <h2 ref={headingRef} tabIndex={-1} className="text-2xl font-bold text-[color:var(--da-color-fg)] mb-2 focus:outline-none">
          {recommendations.title}
        </h2>
        <p className="text-[color:var(--da-color-muted)]">
          {t('點擊任一文件開始閱讀，優先從「START HERE」開始。', 'Click any document to read. Start with "START HERE" first.')}
        </p>
        <div className="mt-3 flex items-center gap-3">
          <div
            className="flex-1 h-2 bg-[color:var(--da-color-accent-soft)] rounded-full overflow-hidden"
            role="progressbar"
            aria-valuenow={done}
            aria-valuemin={0}
            aria-valuemax={total}
            aria-label={t('閱讀進度', 'Reading progress')}
          >
            <div className="h-full bg-[color:var(--da-color-success)] rounded-full transition-all" style={progressStyle}></div>
          </div>
          <span className="text-sm font-medium text-[color:var(--da-color-muted)]">{done}/{total}</span>
        </div>
      </div>

      <div className="space-y-3">
        {recommendations.docs.map((doc, idx) => (
          <DocumentLink key={idx} doc={doc} isRead={readDocs.has(doc.path)} onToggleRead={onToggleRead} />
        ))}
      </div>
    </div>
  );
};

// Grow-ops handoff card — the "Ready to act?" seam at the end of a role path.
// Renders the role's HANDOFF_TARGETS as anchor cards opening existing tools.
const GrowOpsHandoff = ({ role }) => {
  const targets = HANDOFF_TARGETS[role] || [];
  if (targets.length === 0) return null;
  return (
    <div className="bg-[color:var(--da-color-surface)] rounded-lg shadow-sm border border-[color:var(--da-color-accent-border-soft)] p-6">
      <h3 className="text-lg font-bold text-[color:var(--da-color-fg)] mb-1">
        {t("準備動手了嗎？", "Ready to act?")}
      </h3>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
        {t("讀完後，用這些互動工具把所學付諸實作：", "When you've read enough, put it into practice with these interactive tools:")}
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {targets.map((target) => (
          <a
            key={target.href}
            href={target.href}
            className="block p-4 rounded-lg border border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] hover:border-[color:var(--da-color-accent)] hover:shadow-md transition-all focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"
          >
            <div className="flex items-center justify-between">
              <span className="text-sm font-semibold text-[color:var(--da-color-accent)]">{target.label()}</span>
              <span className="text-[color:var(--da-color-accent)]" aria-hidden="true">→</span>
            </div>
            <p className="text-xs text-[color:var(--da-color-muted)] mt-1 leading-relaxed">{target.desc()}</p>
          </a>
        ))}
      </div>
    </div>
  );
};

export default function GettingStartedWizard() {
  const initial = readHash();
  const hasInitialOption = RECOMMENDATIONS[recommendationKeyFor(initial.role, initial.option)];
  const [step, setStep] = useState(hasInitialOption ? 2 : initial.role ? 1 : 0);
  const [selectedRole, setSelectedRole] = useState(initial.role);
  const [selectedOption, setSelectedOption] = useState(initial.option);
  const [recommendationKey, setRecommendationKey] = useState(
    hasInitialOption ? recommendationKeyFor(initial.role, initial.option) : null
  );
  const [readDocs, setReadDocs] = useState(initial.readDocs || new Set());
  const [showCompare, setShowCompare] = useState(false);

  // A11y: move keyboard focus to the new step's heading on every step change
  // so screen-reader / keyboard users are not stranded at the top of the page
  // after the visible content swaps. `headingRef` is attached to each step's
  // <h2>; the heading carries tabIndex={-1} so it is programmatically
  // focusable without entering the tab order.
  const headingRef = useRef(null);
  const didMountRef = useRef(false);
  useEffect(() => {
    // Skip the very first paint (don't steal focus on initial load / deep
    // link); only move focus on subsequent step transitions.
    if (!didMountRef.current) { didMountRef.current = true; return; }
    if (headingRef.current) headingRef.current.focus();
  }, [step]);

  const toggleReadDoc = (docPath) => {
    setReadDocs(prev => {
      const next = new Set(prev);
      if (next.has(docPath)) next.delete(docPath); else next.add(docPath);
      writeHash(selectedRole, selectedOption, next);
      return next;
    });
  };

  const getOptionsList = () => {
    if (selectedRole === "platform") return GOALS.platform;
    if (selectedRole === "domain") return DATABASES.domain;
    if (selectedRole === "tenant") return NEEDS.tenant;
    return [];
  };

  const handleRoleSelect = (roleId) => {
    setSelectedRole(roleId);
    setSelectedOption(null);
    setRecommendationKey(null);
    setStep(1);
    writeHash(roleId, null, readDocs);
    // Persist role to flow state for cross-step data passing
    if (window.__flowSave) window.__flowSave({ role: roleId });
  };

  const handleOptionSelect = (optionId) => {
    setSelectedOption(optionId);
    const key = recommendationKeyFor(selectedRole, optionId);
    setRecommendationKey(key);
    setStep(2);
    writeHash(selectedRole, optionId, readDocs);
  };

  const handleStartOver = () => {
    setStep(0);
    setSelectedRole(null);
    setSelectedOption(null);
    setRecommendationKey(null);
    setReadDocs(new Set());
    writeHash(null, null, null);
  };

  const selectedRoleObj = ROLES.find((r) => r.id === selectedRole);
  const optionsList = getOptionsList();
  // Per-role lifecycle axis for step-1 grouping (defaults to flat for an
  // unknown role so a stale deep link can never crash the render).
  const axisForRole = ROLE_AXIS[selectedRole] || { axis: "flat" };
  const selectedOptionObj = optionsList.find((o) => o.id === selectedOption);
  const recommendations = recommendationKey
    ? RECOMMENDATIONS[recommendationKey]
    : null;

  return (
    <div className="min-h-screen bg-[image:var(--da-color-hero-gradient)]">
      <div className="max-w-4xl mx-auto px-4 py-8 sm:py-12">
        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="text-4xl sm:text-5xl font-bold text-[color:var(--da-color-fg)] mb-3">
            {t("動態警報平台", "Dynamic Alerting Platform")}
          </h1>
          <p className="text-lg text-[color:var(--da-color-muted)] mb-4">
            {t("幾秒內找到你的專屬學習路徑", "Find your personalized learning path in seconds")}
          </p>
          {step === 0 && (
            <div className="inline-flex flex-wrap justify-center gap-2 text-xs text-[color:var(--da-color-muted)]">
              <span>{t("第一次接觸？點擊術語了解更多：", "New to the platform? Tap any term to learn more:")}</span>
              {Object.keys(GLOSSARY).map(term => (
                <GlossaryTip key={term} term={term} />
              ))}
            </div>
          )}
        </div>

        {/* Progress Indicator */}
        <ProgressIndicator step={step} totalSteps={3} />

        {/* Step 1: Role Selection */}
        {step === 0 && (
          <div className="space-y-6">
            <div className="bg-[color:var(--da-color-surface)] rounded-lg shadow-sm p-8">
              <h2 ref={headingRef} tabIndex={-1} className="text-2xl font-bold text-[color:var(--da-color-fg)] mb-8 focus:outline-none">
                {t("你的角色是？", "Who are you?")}
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {ROLES.map((role) => (
                  <RoleCard
                    key={role.id}
                    role={role}
                    isSelected={selectedRole === role.id}
                    onClick={() => handleRoleSelect(role.id)}
                  />
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Step 2: Role-Specific Questions */}
        {step === 1 && selectedRoleObj && (
          <div className="space-y-6">
            <div className="bg-[color:var(--da-color-surface)] rounded-lg shadow-sm p-8">
              <h2 ref={headingRef} tabIndex={-1} className="text-2xl font-bold text-[color:var(--da-color-fg)] mb-2 focus:outline-none">
                {selectedRoleObj.label}
              </h2>
              <p className="text-[color:var(--da-color-muted)] mb-8">
                {selectedRoleObj.desc}
              </p>

              <div className="mb-6">
                <h3 className="text-lg font-semibold text-[color:var(--da-color-fg)] mb-4">
                  {selectedRole === "platform" && t("你的目標是？", "What's your goal?")}
                  {selectedRole === "domain" && t("你管理哪種資料庫？", "What database do you manage?")}
                  {selectedRole === "tenant" && t("你需要什麼幫助？", "What do you need help with?")}
                </h3>
                {axisForRole.axis === "lifecycle" ? (
                  // Lifecycle axis: render each NON-EMPTY bucket as an <h4>
                  // sub-heading + a grid of that bucket's OptionCards (the
                  // role's option list filtered to the bucket's optionIds).
                  // This is the ONLY place the lifecycle axis appears —
                  // RECOMMENDATIONS data + the option= deep link are untouched.
                  <div className="space-y-6">
                    {axisForRole.buckets.map((bucket) => {
                      const bucketOptions = optionsList.filter((o) => bucket.optionIds.includes(o.id));
                      if (bucketOptions.length === 0) return null; // skip empty buckets
                      return (
                        <div key={bucket.id}>
                          <h4 className="text-sm font-semibold uppercase tracking-wide text-[color:var(--da-color-muted)] mb-3">
                            {(BUCKET_LABELS[bucket.id] || (() => bucket.id))()}
                          </h4>
                          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                            {bucketOptions.map((option) => (
                              <OptionCard
                                key={option.id}
                                option={option}
                                icon={option.icon}
                                isSelected={selectedOption === option.id}
                                onClick={() => handleOptionSelect(option.id)}
                              />
                            ))}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  // Flat axis (domain): keep the original flat OptionCard grid —
                  // db-type branches are types, not lifecycle stages.
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {optionsList.map((option) => (
                      <OptionCard
                        key={option.id}
                        option={option}
                        icon={option.icon}
                        isSelected={selectedOption === option.id}
                        onClick={() => handleOptionSelect(option.id)}
                      />
                    ))}
                  </div>
                )}
              </div>

              <button
                type="button"
                onClick={handleStartOver}
                className="w-full px-4 py-2 text-sm font-medium text-[color:var(--da-color-fg)] bg-[color:var(--da-color-surface-hover)] rounded-lg hover:bg-[color:var(--da-color-surface-border)] transition-colors"
              >
                {t("返回", "Back")}
              </button>
            </div>
          </div>
        )}

        {/* Step 3: Recommendations */}
        {step === 2 && recommendations && (
          <div className="space-y-6">
            <RecommendationsSummary recommendations={recommendations} readDocs={readDocs} onToggleRead={toggleReadDoc} headingRef={headingRef} />

            {/* Grow-ops handoff: the "Ready to act?" seam to the role's tools */}
            <GrowOpsHandoff role={selectedRole} />

            {showCompare && recommendationKey && (
              <PathCompare currentKey={recommendationKey} role={selectedRole} onClose={() => setShowCompare(false)} />
            )}

            <div className="flex flex-col sm:flex-row gap-3">
              <button
                type="button"
                onClick={() => setStep(1)}
                className="flex-1 px-4 py-3 text-sm font-medium text-[color:var(--da-color-fg)] bg-[color:var(--da-color-surface-hover)] rounded-lg hover:bg-[color:var(--da-color-surface-border)] transition-colors"
              >
                {t("返回", "Back")}
              </button>
              <button
                type="button"
                onClick={() => setShowCompare(!showCompare)}
                aria-pressed={showCompare}
                className={`flex-1 px-4 py-3 text-sm font-medium rounded-lg transition-colors ${
                  showCompare
                    ? 'bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)]'
                    : 'bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)] hover:bg-[color:var(--da-color-accent-soft)]'
                }`}
              >
                {showCompare ? t('隱藏比較', 'Hide Compare') : t('比較路徑', 'Compare Paths')}
              </button>
              <button
                type="button"
                onClick={handleStartOver}
                className="flex-1 px-4 py-3 text-sm font-medium text-[color:var(--da-color-accent-fg)] bg-[color:var(--da-color-accent)] rounded-lg hover:bg-[color:var(--da-color-accent-hover)] transition-colors"
              >
                {t("重新開始", "Start Over")}
              </button>
            </div>
          </div>
        )}

        {/* Footer */}
        <div className="mt-12 pt-8 border-t border-[color:var(--da-color-surface-border)]">
          <p className="text-center text-sm text-[color:var(--da-color-muted)]">
            {t("有問題嗎？查看", "Questions? Check the")}{" "}
            <a href={docUrl("../troubleshooting.md")} target="_blank" rel="noopener noreferrer" className="text-[color:var(--da-color-accent)] hover:underline">
              {t("疑難排解指南", "Troubleshooting Guide")}
            </a>
            {" "}{t("或", "or")}{" "}
            <a href={docUrl("../context-diagram.md")} target="_blank" rel="noopener noreferrer" className="text-[color:var(--da-color-accent)] hover:underline">
              {t("情境關係圖", "Context Diagram")}
            </a>
          </p>
        </div>
      </div>
    </div>
  );
}
