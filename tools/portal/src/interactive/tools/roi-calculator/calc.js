---
title: "ROI Calculator — savings & reduction models"
purpose: |
  Pure functions behind the ROI calculator: the O(N×M)→O(M) rule-maintenance
  model, alert-storm reduction, time-to-market improvement, and the annual TCO
  roll-up. Each takes a plain config object and returns derived numbers only —
  no React, no DOM, no platform-data closure.

  Pre-PR-portal-13 these were inline in roi-calculator.jsx (689 LOC) with 0%
  coverage. Extracting them shrinks the component, isolates the empirical rates
  (dedup 40% / maintenance 25% / silent 15%, etc.) and makes the arithmetic
  directly unit-testable.

  Public API:
    calcRuleMaintenance({ tenants, packs, changeMinutes, changesPerMonth })
    calcAlertStormReduction({ stormsPerMonth, avgAlertsPerStorm })
    calcTimeToMarket({ tenants, manualOnboardMinutes })
    calcAnnualSavings({ ruleSavedHours, ttmSavedHours, hourlyRate, alertReduction, oncallStaff })

  Closure deps: none. Pure functions; receive config as args.
---

/**
 * O(N×M) → O(M) rule maintenance model.
 * Without platform: each tenant needs individual rule config = N tenants × M packs × T minutes.
 * With platform: configure once per pack = M packs × T minutes (tenant inherits via profiles).
 */
function calcRuleMaintenance({ tenants, packs, changeMinutes, changesPerMonth }) {
  const withoutHours = (tenants * packs * changeMinutes * changesPerMonth) / 60;
  const withHours = (packs * changeMinutes * changesPerMonth) / 60;
  const savedHours = Math.max(withoutHours - withHours, 0);
  const reduction = withoutHours > 0 ? (savedHours / withoutHours) * 100 : 0;
  return { withoutHours, withHours, savedHours, reduction };
}

/**
 * Alert storm reduction via auto-suppression + maintenance mode.
 * Empirical model: severity dedup removes ~40% noise, maintenance mode removes ~25%,
 * silent mode removes ~15%. Combined effect with overlap adjustment.
 */
function calcAlertStormReduction({ stormsPerMonth, avgAlertsPerStorm }) {
  const dedupRate = 0.40;
  const maintenanceRate = 0.25;
  const silentRate = 0.15;
  // Combined reduction with diminishing returns
  const combined = 1 - (1 - dedupRate) * (1 - maintenanceRate) * (1 - silentRate);
  const totalAlertsMonth = stormsPerMonth * avgAlertsPerStorm;
  const reducedAlerts = Math.round(totalAlertsMonth * combined);
  return { totalAlertsMonth, reducedAlerts, reductionPct: combined * 100 };
}

/**
 * Time-to-market improvement: scaffold + migration automation.
 * Without: manual YAML authoring + validation + route config.
 * With: `da-tools scaffold` + `validate-config` + `generate-routes`.
 */
function calcTimeToMarket({ tenants, manualOnboardMinutes }) {
  const automatedMinutes = 5; // scaffold + validate + generate-routes
  const perTenantSaved = Math.max(manualOnboardMinutes - automatedMinutes, 0);
  const totalSavedHours = (tenants * perTenantSaved) / 60;
  const reduction = manualOnboardMinutes > 0
    ? (perTenantSaved / manualOnboardMinutes) * 100 : 0;
  return { manualHours: (tenants * manualOnboardMinutes) / 60, automatedHours: (tenants * automatedMinutes) / 60, totalSavedHours, reduction };
}

/**
 * Annual TCO savings combining all three dimensions.
 */
function calcAnnualSavings({ ruleSavedHours, ttmSavedHours, hourlyRate, alertReduction, oncallStaff }) {
  const ruleAnnual = ruleSavedHours * 12 * hourlyRate;
  const ttmAnnual = ttmSavedHours * hourlyRate; // one-time per new tenant, amortized
  // Alert reduction → less on-call fatigue → estimated 20% productivity recovery
  const alertAnnual = alertReduction * oncallStaff * hourlyRate * 0.20 * 12;
  return { ruleAnnual, ttmAnnual, alertAnnual, total: ruleAnnual + ttmAnnual + alertAnnual };
}

// Legacy jsx-loader path: expose as window globals (see PR-portal-12 / TD-030z).

export {
  calcRuleMaintenance,
  calcAlertStormReduction,
  calcTimeToMarket,
  calcAnnualSavings,
};
