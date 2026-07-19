/**
 * severityBadgeClass — the Tailwind colour-pair for a severity badge.
 *
 * Extracted verbatim from the four tools that hand-duplicated the exact same
 * binary ternary (alert-simulator / health-dashboard / migration-simulator /
 * runbook-viewer): `severity === 'critical' ? 'bg-red-100 text-red-700'
 * : 'bg-amber-100 text-amber-700'`.
 *
 * Behaviour is preserved verbatim — 'critical' → red, every other value
 * (warning / info / undefined) → amber. The warning-and-info-both-amber
 * limitation is intentional here: giving them distinct colours is a
 * visual/design change, not a refactor, and is tracked separately.
 *
 * Returns ONLY the colour classes; callers keep their own padding / rounding
 * / font wrappers (they differ per site), so this helper never dictates
 * layout.
 */
function severityBadgeClass(severity) {
  return severity === 'critical' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700';
}

export { severityBadgeClass };
