---
title: "Migration ROI Calculator — estimation models"
purpose: |
  Pure functions behind the migration ROI calculator: platform coverage,
  migration effort (rule-count × blended minutes), post-migration maintenance
  savings (O(N×M)→O(M)), break-even months, and annual USD savings.

  Pre-PR-portal-14 these were inline in migration-roi-calculator.jsx (669 LOC)
  with 0% coverage. `estimatePlatformCoverage` previously read the module-scope
  `TOTAL_RULES` global (derived from platform-data); it now takes `platformRules`
  as an argument so the model is fully pure and unit-testable. The orchestrator
  passes TOTAL_RULES at the call site — behavior is unchanged.

  Public API:
    estimatePlatformCoverage({ totalRules, platformRules })
    estimateMigrationEffort({ totalRules, recordingRules, alertRules })
    estimateMaintenanceSavings({ currentMonthlyHours, tenants })
    estimateBreakEven({ migrationHours, monthlySavingsHours })
    estimateAnnualSavings({ monthlySavingsHours, hourlyRate })

  Closure deps: none. Pure functions; receive config (incl. platformRules) as args.
---

/**
 * Platform coverage estimation.
 * Average rules per pack: 16 (238 total / 15 packs).
 * For common DB monitoring: 60-80% overlap with platform rules.
 * `platformRules` is the platform's total rule count (TOTAL_RULES at the call site).
 */
function estimatePlatformCoverage({ totalRules, platformRules }) {
  const baseOverlap = 0.70; // 70% average overlap for standard DB monitoring
  const maxCoverage = Math.min((platformRules / totalRules) * 100, 100);
  const coverage = Math.min(Math.round(baseOverlap * maxCoverage), 100);
  return coverage;
}

/**
 * Migration effort estimation.
 * Simple threshold mapping: ~5 minutes per rule.
 * Complex PromQL expressions: ~15 minutes per rule.
 * Distribution: 70% simple / 30% complex.
 */
function estimateMigrationEffort({ totalRules, recordingRules, alertRules }) {
  const simpleMinutes = 5;
  const complexMinutes = 15;
  const simpleRatio = 0.70;
  const complexRatio = 0.30;

  const avgMinutesPerRule = simpleMinutes * simpleRatio + complexMinutes * complexRatio;
  const totalMinutes = totalRules * avgMinutesPerRule;
  const hours = Math.round(totalMinutes / 60);

  return hours;
}

/**
 * Post-migration maintenance reduction.
 * From O(N×M) to O(M): N tenants × M packs → M packs.
 * Estimate monthly reduction based on tenant count.
 */
function estimateMaintenanceSavings({ currentMonthlyHours, tenants }) {
  if (tenants <= 1) return 0;

  // With N tenants, maintenance scales O(N×M)
  // With platform, it scales O(M)
  // Savings ≈ current_hours × (1 - 1/tenants)
  const savings = Math.round(currentMonthlyHours * (1 - 1/tenants));
  return Math.max(savings, 0);
}

/**
 * Break-even calculation.
 * Break-even months = migration_hours / monthly_savings.
 */
function estimateBreakEven({ migrationHours, monthlySavingsHours }) {
  if (monthlySavingsHours <= 0) return Infinity;
  return Math.round(migrationHours / monthlySavingsHours * 10) / 10;
}

/**
 * Annual savings in USD.
 */
function estimateAnnualSavings({ monthlySavingsHours, hourlyRate }) {
  const annualHours = monthlySavingsHours * 12;
  return Math.round(annualHours * hourlyRate);
}

// Legacy jsx-loader path: expose as window globals (see PR-portal-12 / TD-030z).

export {
  estimatePlatformCoverage,
  estimateMigrationEffort,
  estimateMaintenanceSavings,
  estimateBreakEven,
  estimateAnnualSavings,
};
