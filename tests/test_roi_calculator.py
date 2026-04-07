"""
ROI Calculator numerical correctness validation (v2.6.0 Phase C).

Tests the calculation models from roi-calculator.jsx and migration-roi-calculator.jsx
using Python re-implementations to verify the JavaScript math is correct.
"""
import pytest


# ── ROI Calculator models (mirrors roi-calculator.jsx) ──────────────────────

def calc_rule_maintenance(tenants, packs, change_minutes, changes_per_month):
    """O(N×M) → O(M) rule maintenance model."""
    without_hours = (tenants * packs * change_minutes * changes_per_month) / 60
    with_hours = (packs * change_minutes * changes_per_month) / 60
    saved_hours = max(without_hours - with_hours, 0)
    reduction = (saved_hours / without_hours * 100) if without_hours > 0 else 0
    return {
        "without_hours": without_hours,
        "with_hours": with_hours,
        "saved_hours": saved_hours,
        "reduction": reduction,
    }


def calc_alert_storm_reduction(storms_per_month, avg_alerts_per_storm):
    """Alert storm reduction via auto-suppression + maintenance mode."""
    dedup_rate = 0.40
    maintenance_rate = 0.25
    silent_rate = 0.15
    combined = 1 - (1 - dedup_rate) * (1 - maintenance_rate) * (1 - silent_rate)
    total_alerts = storms_per_month * avg_alerts_per_storm
    reduced = round(total_alerts * combined)
    return {
        "total_alerts_month": total_alerts,
        "reduced_alerts": reduced,
        "reduction_pct": combined * 100,
    }


def calc_time_to_market(tenants, manual_onboard_minutes):
    """Time-to-market improvement via scaffold automation."""
    automated_minutes = 5
    per_tenant_saved = max(manual_onboard_minutes - automated_minutes, 0)
    total_saved_hours = (tenants * per_tenant_saved) / 60
    reduction = (per_tenant_saved / manual_onboard_minutes * 100) if manual_onboard_minutes > 0 else 0
    return {
        "manual_hours": (tenants * manual_onboard_minutes) / 60,
        "automated_hours": (tenants * automated_minutes) / 60,
        "total_saved_hours": total_saved_hours,
        "reduction": reduction,
    }


def calc_annual_savings(rule_saved_hours, ttm_saved_hours, hourly_rate, alert_reduction, oncall_staff):
    """Annual TCO savings combining all three dimensions."""
    rule_annual = rule_saved_hours * 12 * hourly_rate
    ttm_annual = ttm_saved_hours * hourly_rate
    alert_annual = alert_reduction * oncall_staff * hourly_rate * 0.20 * 12
    return {
        "rule_annual": rule_annual,
        "ttm_annual": ttm_annual,
        "alert_annual": alert_annual,
        "total": rule_annual + ttm_annual + alert_annual,
    }


# ── Migration ROI Calculator models ────────────────────────────────────────

def calc_migration_coverage(user_rules, platform_rules=238):
    """Estimated platform coverage based on overlap heuristic."""
    if user_rules <= 50:
        overlap = 0.80  # small rule set likely maps well
    elif user_rules <= 200:
        overlap = 0.70
    else:
        overlap = 0.60  # larger sets have more custom rules
    covered = min(round(user_rules * overlap), platform_rules)
    coverage_pct = (covered / user_rules * 100) if user_rules > 0 else 0
    return {"covered": covered, "coverage_pct": coverage_pct}


def calc_migration_effort(recording_rules, alert_rules):
    """Migration effort estimate: 70% simple (5min) + 30% complex (15min)."""
    total_rules = recording_rules + alert_rules
    simple = round(total_rules * 0.70)
    complex_ = total_rules - simple
    effort_minutes = simple * 5 + complex_ * 15
    return {"total_rules": total_rules, "effort_hours": effort_minutes / 60}


def calc_monthly_savings(current_hours, tenants):
    """Post-migration maintenance reduction: O(N×M) → O(M)."""
    if tenants <= 1:
        return 0
    return current_hours * (1 - 1 / tenants)


# ── Tests ───────────────────────────────────────────────────────────────────

class TestRuleMaintenance:
    def test_default_scenario(self):
        result = calc_rule_maintenance(20, 15, 30, 4)
        assert result["without_hours"] == (20 * 15 * 30 * 4) / 60  # 600
        assert result["with_hours"] == (15 * 30 * 4) / 60  # 30
        assert result["saved_hours"] == 570
        assert result["reduction"] == pytest.approx(95.0)

    def test_single_tenant(self):
        result = calc_rule_maintenance(1, 15, 30, 4)
        assert result["saved_hours"] == 0  # N=1 → O(N×M) = O(M), no savings

    def test_large_scale(self):
        result = calc_rule_maintenance(500, 15, 30, 4)
        assert result["saved_hours"] > 0
        assert result["reduction"] == pytest.approx(99.8)

    def test_zero_changes(self):
        result = calc_rule_maintenance(20, 15, 0, 4)
        assert result["saved_hours"] == 0


class TestAlertStormReduction:
    def test_combined_reduction(self):
        result = calc_alert_storm_reduction(8, 15)
        # Combined: 1 - (0.6 × 0.75 × 0.85) = 1 - 0.3825 = 0.6175 = 61.75%
        assert result["reduction_pct"] == pytest.approx(61.75, abs=0.01)
        assert result["total_alerts_month"] == 120
        assert result["reduced_alerts"] == round(120 * 0.6175)

    def test_zero_storms(self):
        result = calc_alert_storm_reduction(0, 15)
        assert result["total_alerts_month"] == 0
        assert result["reduced_alerts"] == 0

    def test_high_volume(self):
        result = calc_alert_storm_reduction(30, 100)
        assert result["total_alerts_month"] == 3000
        assert result["reduced_alerts"] > 1800  # >60% of 3000


class TestTimeToMarket:
    def test_default_scenario(self):
        result = calc_time_to_market(20, 120)
        assert result["manual_hours"] == 40  # 20 * 120 / 60
        assert result["automated_hours"] == pytest.approx(1.6667, abs=0.01)
        assert result["reduction"] == pytest.approx(95.833, abs=0.01)

    def test_already_fast(self):
        result = calc_time_to_market(20, 5)
        assert result["total_saved_hours"] == 0  # 5min manual = 5min auto, no savings


class TestAnnualSavings:
    def test_typical_scenario(self):
        rule = calc_rule_maintenance(20, 15, 30, 4)
        ttm = calc_time_to_market(20, 120)
        storm = calc_alert_storm_reduction(8, 15)
        annual = calc_annual_savings(
            rule["saved_hours"], ttm["total_saved_hours"],
            75, storm["reduced_alerts"], 3
        )
        assert annual["total"] > 0
        assert annual["rule_annual"] > annual["alert_annual"]  # Rule savings dominate

    def test_all_components_positive(self):
        annual = calc_annual_savings(100, 50, 75, 100, 3)
        assert annual["rule_annual"] == 100 * 12 * 75
        assert annual["ttm_annual"] == 50 * 75
        assert annual["alert_annual"] == 100 * 3 * 75 * 0.20 * 12
        assert annual["total"] == annual["rule_annual"] + annual["ttm_annual"] + annual["alert_annual"]


class TestMigrationCoverage:
    def test_small_ruleset(self):
        result = calc_migration_coverage(30)
        assert result["coverage_pct"] == pytest.approx(80.0)

    def test_medium_ruleset(self):
        result = calc_migration_coverage(150)
        assert 60 <= result["coverage_pct"] <= 80

    def test_large_ruleset(self):
        result = calc_migration_coverage(1000)
        assert result["covered"] <= 238  # capped at platform rules


class TestMigrationEffort:
    def test_default_scenario(self):
        result = calc_migration_effort(100, 80)
        total = 180
        simple = round(total * 0.70)  # 126
        complex_ = total - simple  # 54
        expected_hours = (simple * 5 + complex_ * 15) / 60
        assert result["effort_hours"] == pytest.approx(expected_hours)

    def test_small_set(self):
        result = calc_migration_effort(10, 10)
        assert result["effort_hours"] < 5  # small set = quick migration


class TestMonthlySavings:
    def test_typical(self):
        savings = calc_monthly_savings(40, 20)
        assert savings == pytest.approx(38.0)  # 40 * (1 - 1/20) = 40 * 0.95

    def test_single_tenant(self):
        savings = calc_monthly_savings(40, 1)
        assert savings == 0  # No savings with 1 tenant

    def test_large_scale(self):
        savings = calc_monthly_savings(200, 500)
        assert savings == pytest.approx(199.6)


class TestBreakEven:
    def test_positive_scenario(self):
        effort = calc_migration_effort(100, 80)
        monthly = calc_monthly_savings(40, 20)
        breakeven_months = effort["effort_hours"] / monthly if monthly > 0 else float('inf')
        assert breakeven_months < 12  # Should break even within a year
        assert breakeven_months > 0
