package main

// #2368 round 2: the redundant-override advice across the #1231 alias
// spellings, over the PRODUCT of every layer that can carry a threshold —
// defaults chain × elected profile × two root platform `tenants:` entries ×
// the tenant writing one spelling or both × each spelling × a value set. The
// oracle is /metrics itself: the finding is wrong exactly when deleting the
// key moves what LoadDir + Resolve serves for the threshold.
//
// It asserts the one direction that hurts: NO wrong advice. A missed hint
// (a truly redundant key not reported) is counted and logged, not failed —
// MergedDefaults is conservative by design (it never renames a lone legacy
// spelling, and a threshold the tenant writes twice is not judged).
//
// Measured when written: 16128 cells; before round 2 (705c2045) 315 wrong,
// on main 218; after it 0.
//
// ⚠️ `-short` runs only the values 30 and 71, which cannot see the chain-
// and profile-layer both-spellings cells (the losing spelling is 31 / 51, so
// a tenant must write that value to be judged against it). The chain shape
// is held in every mode by the unit row F1-chain-writes-both-spellings (and
// the platform shape by F1-platform-entry-writes-both-spellings) in
// platform_alias_guard_test.go; the profile layer never carries both
// spellings (profileFor canonicalizes it — see the note in
// pkg/config/hierarchy.go), so only the full run exercises those cells.
//
// Origin: the blind reviewer's combinatorial probe, kept as the regression
// test it proved to be.

import (
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

const aliasCanon = "mysql_threads_running"

var aliasLegacy = func() string {
	l, ok := config.LegacySpellingFor(aliasCanon)
	if !ok {
		panic("no legacy spelling for " + aliasCanon + " — the #1231 alias window closed; retire this test")
	}
	return l
}()

// aliasServed is every mysql threads_running / cpu row /metrics serves for
// tx with tx.yaml set to body, as one comparable string.
func aliasServed(t *testing.T, dir, body string) string {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, "tx.yaml"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, _, err := config.LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("LoadDir: %v", err)
	}
	var out []string
	for _, r := range cfg.Resolve() {
		if r.Tenant == "tx" && r.Component == "mysql" && (r.Metric == "threads_running" || r.Metric == "cpu") {
			out = append(out, fmt.Sprintf("%s/%s=%v", r.Metric, r.Severity, r.Value))
		}
	}
	sort.Strings(out)
	return strings.Join(out, ",")
}

// aliasRedundant is da-guard's redundant_override fields for tx, through the
// same scope + input builder the CLI uses.
func aliasRedundant(t *testing.T, dir string) map[string]bool {
	t.Helper()
	scoped, err := config.ScopeEffective(dir, dir)
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	report, err := guard.CheckDefaultsImpact(buildCheckInput(scoped, &flags{}))
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]bool{}
	for _, f := range report.Findings {
		if f.Kind == guard.FindingRedundantOverride && f.TenantID == "tx" {
			out[f.Field] = true
		}
	}
	return out
}

type aliasLayer struct {
	name string
	kv   [][2]string // key, YAML value
}

func (l aliasLayer) lines(indent string) string {
	var b strings.Builder
	for _, p := range l.kv {
		b.WriteString(indent + p[0] + ": " + p[1] + "\n")
	}
	return b.String()
}

func TestGuard_RedundantAdviceNeverMovesMetricsAcrossAliasSpellings(t *testing.T) {
	t.Parallel()
	C, L := aliasCanon, aliasLegacy
	chains := []aliasLayer{{"cC30", [][2]string{{C, "30"}}}, {"cL30", [][2]string{{L, "30"}}}, {"cBoth", [][2]string{{C, "30"}, {L, "31"}}},
		// #2418: a null writes nothing, so the legacy 31 is the level's value.
		{"cCnullL31", [][2]string{{C, "null"}, {L, "31"}}}}
	profiles := []aliasLayer{{"p-", nil}, {"pC50", [][2]string{{C, "50"}}}, {"pL50", [][2]string{{L, "50"}}},
		{"pBoth", [][2]string{{C, "50"}, {L, "51"}}}, {"pCnull", [][2]string{{C, "null"}}}, {"pLnull", [][2]string{{L, "null"}}},
		{"pLdis", [][2]string{{L, "disable"}}}}
	platA := []aliasLayer{{"a-", nil}, {"aC70", [][2]string{{C, "70"}}}, {"aL70", [][2]string{{L, "70"}}},
		{"aBoth", [][2]string{{C, "70"}, {L, "71"}}}, {"aCnull", [][2]string{{C, "null"}}}, {"aLnull", [][2]string{{L, "null"}}},
		{"aCdis", [][2]string{{C, "disable"}}}, {"aLdis", [][2]string{{L, "disable"}}}}
	platB := []aliasLayer{{"b-", nil}, {"bC80", [][2]string{{C, "80"}}}, {"bL80", [][2]string{{L, "80"}}}}
	values := []string{"30", "31", "50", "51", "70", "71", "80", "disable"}
	if testing.Short() {
		values = []string{"30", "71"}
	}

	var cells, missed atomic.Int64
	for _, c := range chains {
		for _, p := range profiles {
			t.Run(c.name+"/"+p.name, func(t *testing.T) {
				t.Parallel()
				for _, a := range platA {
					for _, b := range platB {
						files := map[string]string{
							"_defaults.yaml": "defaults:\n" + c.lines("  ") + "  pg_connections: 100\n",
						}
						if p.kv != nil {
							files["_profiles.yaml"] = "profiles:\n  std:\n" + p.lines("    ")
						}
						if a.kv != nil {
							files["_a.yaml"] = "tenants:\n  tx:\n" + a.lines("    ")
						}
						if b.kv != nil {
							files["_b.yaml"] = "tenants:\n  tx:\n" + b.lines("    ")
						}
						head := "tenants:\n  tx:\n    pg_connections: 100\n"
						if p.kv != nil {
							head += "    _profile: std\n"
						}
						for _, both := range []bool{false, true} {
							for _, field := range []string{C, L} {
								other := ""
								if both {
									o := L
									if field == L {
										o = C
									}
									other = "    " + o + ": 99\n"
								}
								for _, v := range values {
									dir := t.TempDir()
									testutil.WriteTree(t, dir, files)
									without := aliasServed(t, dir, head+other)
									with := aliasServed(t, dir, head+other+"    "+field+": "+v+"\n")
									red := aliasRedundant(t, dir)
									cells.Add(1)
									name := fmt.Sprintf("%s/%s/%s/%s/both=%v/%s=%s", c.name, p.name, a.name, b.name, both, field, v)
									if red[field] && with != without {
										t.Errorf("WRONG ADVICE %s: %s called redundant, but deleting it moves /metrics [%s] → [%s]", name, field, with, without)
									}
									if !red[field] && with == without && !both {
										missed.Add(1)
									}
								}
							}
						}
					}
				}
			})
		}
	}
	t.Cleanup(func() {
		t.Logf("cells=%d missed-hint=%d (logged, not failed)", cells.Load(), missed.Load())
	})
}
