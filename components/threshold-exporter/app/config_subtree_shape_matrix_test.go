package main

// Sweep B-3 of #1521 (PR #1569): the fixture SHAPE matrix.
//
// ⛔ WHY SHAPE AND NOT MORE VALUES. Every round of this ticket had "boundary"
// tests, and every round a reviewer found a defect anyway. Looking back at the
// 25 findings, the boundaries those tests enumerated were VALUE variations —
// disable, a schedule, an inline severity, an expiry — while the defects lived
// in SHAPE: one level deeper, no root defaults file at all, two tenants under
// one subtree, the key declared at the middle level instead of the top. A
// single-level fixture with one tenant and a root `_defaults.yaml` cannot see
// any of those, and that was the fixture almost every test used.
//
// So this file enumerates the shape space — 3 depths x {root defaults, none} x
// 4 declaration patterns x {tenant authors, does not} x {1, 2} tenants per
// level = 96 subtests — and asserts ONE property over all of it, the ticket's
// own contract stated generically:
//
//	for every tenant and every key: either the two planes agree on the value,
//	or something NAMES the disagreement — the divergence report, or a WARN
//	from whichever component refused the key.
//
// Silent disagreement is the only outcome forbidden, which is exactly what
// #1521 is. Anything this property permits is either correct or loudly
// declared, and no individual case has to be foreseen.
//
// ⛔ WHAT THIS MATRIX DOES NOT COVER, stated because a reader will assume it
// does: every shape here is a cold `Load()`. It never calls `IncrementalLoad`,
// so no defect on the reload fast path is visible to it — reverting the
// orphan-value fix in `patchTenants` leaves all 96 subtests green. That half
// is `config_incremental_differential_test.go`'s job.
//
// ⚠️ THE WARN CHANNEL IS NOT A LOOPHOLE ADDED TO GET TO GREEN, and the order
// matters: the property was written with the report as its only escape hatch,
// it failed 24 of the 96 shapes, and only then was each failure MEASURED. All 24
// were one shape — a tree with no root `_defaults.yaml` (`cfg.Defaults` empty,
// which is also the state a tree lands in when its root defaults file fails to
// parse) where a tenant authors a key nothing declares. `resolveBaseRows`
// walks `cfg.Defaults`, so no row is built; `/effective` renders the authored
// value; and `ValidateTenantKeys` logs `unknown key … not in defaults` naming
// both tenant and key. Loud through a different component, exactly like
// `_critical` with no base default in the reach matrix. The forbidden outcome
// is unchanged: disagree AND nothing says so.

import (
	"bytes"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// where the key that the tenant does NOT author is declared.
type declPattern int

const (
	declNowhere declPattern = iota
	declEveryLevel
	declShallowestSubtreeOnly
	declDeepestOnly
)

func (d declPattern) String() string {
	switch d {
	case declNowhere:
		return "declared nowhere"
	case declEveryLevel:
		return "declared at every level"
	case declShallowestSubtreeOnly:
		return "declared only at the shallowest subtree level"
	default:
		return "declared only at the deepest level"
	}
}

func TestThePlanesAgreeOrTheReportSaysWhy(t *testing.T) {
	const key = "mysql_connections"

	for _, depth := range []int{1, 2, 3} {
		for _, rootDefaults := range []bool{true, false} {
			for _, pattern := range []declPattern{declNowhere, declEveryLevel, declShallowestSubtreeOnly, declDeepestOnly} {
				for _, tenantAuthors := range []bool{true, false} {
					for _, tenantsPerLevel := range []int{1, 2} {
						name := fmt.Sprintf("depth=%d root=%v %s authored=%v tenants=%d",
							depth, rootDefaults, pattern, tenantAuthors, tenantsPerLevel)
						t.Run(name, func(t *testing.T) {
							dir, tenants := buildShapeTree(t, depth, rootDefaults, pattern, tenantAuthors, tenantsPerLevel, key)
							var logBuf bytes.Buffer
							m := NewConfigManager(dir)
							m.SetLogger(log.New(&logBuf, "", 0))
							if err := m.Load(); err != nil {
								t.Fatalf("Load: %v", err)
							}
							cfg := m.GetConfig()
							m.mu.RLock()
							unreachable := m.hierarchy.unreachableInherited
							m.mu.RUnlock()

							for _, tenant := range tenants {
								assertPlanesAgreeOrReported(t, cfg, unreachable, logBuf.String(), dir, tenant, key)
							}
						})
					}
				}
			}
		}
	}
}

// assertPlanesAgreeOrReported is the whole property, for one tenant and one key.
func assertPlanesAgreeOrReported(
	t *testing.T, cfg *ThresholdConfig,
	unreachable map[string][]string, logs, dir, tenant, key string,
) {
	t.Helper()

	eff, err := config.ResolveEffective(dir, tenant)
	if err != nil {
		t.Fatalf("tenant %s: ResolveEffective: %v", tenant, err)
	}
	hierValue, hierHas := eff.EffectiveConfig[key]

	var seriesValue float64
	seriesHas := false
	for _, row := range cfg.ResolveAt(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)) {
		if row.Tenant == tenant && row.Component+"_"+row.Metric == key && len(row.CustomLabels) == 0 {
			seriesValue, seriesHas = row.Value, true
		}
	}

	reported := false
	for _, k := range unreachable[tenant] {
		if k == key {
			reported = true
		}
	}
	if reported {
		return // the planes are ALLOWED to disagree here, because it is declared
	}
	// The other loud channel: a WARN naming BOTH this tenant and this key.
	// Both, deliberately — a WARN about someone else, or about another key of
	// this tenant, must not license this disagreement.
	//
	// ⛔ THE TENANT MATCH IS DELIMITED, NOT A SUBSTRING. `t-L1-1` is a prefix
	// of `t-L1-10`, so a plain Contains would let one tenant's WARN license a
	// DIFFERENT tenant's silent disagreement the moment this matrix grows past
	// nine tenants per level. It does not today, which is exactly why it would
	// have gone in unnoticed and detonated for whoever widened the matrix.
	for _, line := range strings.Split(logs, "\n") {
		if strings.Contains(line, "WARN") &&
			mentionsExactly(line, tenant) && strings.Contains(line, key) {
			return
		}
	}

	switch {
	case !hierHas && !seriesHas:
		return // absent from both, consistently
	case hierHas != seriesHas:
		t.Fatalf("tenant %s key %s: one plane has a value and the other does not, and nothing reported it\n"+
			"  /effective: has=%v value=%v\n  series:     has=%v value=%v\n--- log ---\n%s",
			tenant, key, hierHas, hierValue, seriesHas, seriesValue, logs)
	}

	// Both have it — the numbers must match. `/effective` renders whatever the
	// YAML held (an int, a float, or a string for the tenant-authored side), so
	// compare through a common rendering rather than by Go type.
	if renderShapeValue(hierValue) != renderShapeValue(seriesValue) {
		t.Fatalf("tenant %s key %s: the two planes report DIFFERENT numbers and nothing reported it —\n"+
			"this is #1521 itself: the operator's diagnostic says one thing and the alert fires on another\n"+
			"  /effective: %v\n  series:     %v\n--- log ---\n%s",
			tenant, key, hierValue, seriesValue, logs)
	}
}

// mentionsExactly reports whether line names id as a whole token, so that
// `t-L1-1` does not match inside `t-L1-10`. Log lines quote the id
// (`tenant=t-L1-1:`, `"t-L1-1"`, `t-L1-1 (`), so the delimiter is "anything
// that cannot continue an identifier".
func mentionsExactly(line, id string) bool {
	for i := 0; ; {
		j := strings.Index(line[i:], id)
		if j < 0 {
			return false
		}
		start, end := i+j, i+j+len(id)
		beforeOK := start == 0 || !isIdentRune(rune(line[start-1]))
		afterOK := end == len(line) || !isIdentRune(rune(line[end]))
		if beforeOK && afterOK {
			return true
		}
		i = start + 1
	}
}

func isIdentRune(r rune) bool {
	return r == '-' || r == '_' ||
		(r >= '0' && r <= '9') || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z')
}

func renderShapeValue(v any) string {
	switch x := v.(type) {
	case string:
		return strings.TrimSpace(x)
	case float64:
		return strings.TrimSuffix(strings.TrimRight(fmt.Sprintf("%.6f", x), "0"), ".")
	case int:
		return fmt.Sprintf("%d", x)
	default:
		return fmt.Sprintf("%v", v)
	}
}

// buildShapeTree writes one tree of the requested shape and returns its root
// plus every tenant id in it.
//
// Levels are named L1..Ln under the conf.d root. Each level holds
// tenantsPerLevel tenant files, so a defect that only shows when two tenants
// share a subtree's defaults has somewhere to appear — the single-tenant
// fixtures every earlier round used could not see the "one tenant's edit
// deleted another's series" class at all.
func buildShapeTree(
	t *testing.T, depth int, rootDefaults bool, pattern declPattern,
	tenantAuthors bool, tenantsPerLevel int, key string,
) (string, []string) {
	t.Helper()
	dir := t.TempDir()

	if rootDefaults {
		writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
			fmt.Sprintf("defaults:\n  %s: 80\n", key))
	} else {
		// A tree with no root defaults file at all — the shape that made
		// `chain[0]` the shallowest SUBTREE file and broke the skip-the-root
		// logic. Something still has to exist at the root or the tree is not
		// hierarchical, so an unrelated tenant lives here.
		writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  t-root: {}\n")
	}

	var tenants []string
	if !rootDefaults {
		tenants = append(tenants, "t-root")
	}

	path := dir
	for level := 1; level <= depth; level++ {
		path = filepath.Join(path, fmt.Sprintf("L%d", level))
		if err := os.MkdirAll(path, 0o755); err != nil {
			t.Fatalf("MkdirAll: %v", err)
		}
		if declare := levelDeclares(pattern, level, depth); declare {
			writeTestYAML(t, filepath.Join(path, "_defaults.yaml"),
				fmt.Sprintf("defaults:\n  %s: %d\n", key, 10*level))
		}
		for n := 1; n <= tenantsPerLevel; n++ {
			id := fmt.Sprintf("t-L%d-%d", level, n)
			body := "tenants:\n  " + id + ": {}\n"
			if tenantAuthors {
				body = fmt.Sprintf("tenants:\n  %s:\n    %s: \"%d\"\n", id, key, 500+level)
			}
			writeTestYAML(t, filepath.Join(path, id+".yaml"), body)
			tenants = append(tenants, id)
		}
	}
	return dir, tenants
}

func levelDeclares(pattern declPattern, level, depth int) bool {
	switch pattern {
	case declEveryLevel:
		return true
	case declShallowestSubtreeOnly:
		return level == 1
	case declDeepestOnly:
		return level == depth
	default:
		return false
	}
}
