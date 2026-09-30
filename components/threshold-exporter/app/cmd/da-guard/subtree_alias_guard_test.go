package main

// #2414: subtree `_defaults.yaml` levels × the #1231 alias spellings. The
// chain is root → sub → sub/deep, the tenant file sits in sub/deep, and each
// level writes the threshold in the canonical spelling, the retired one, both,
// one of them as null beside the other, "disable", or not at all. The oracle
// is /metrics (LoadDir + Resolve):
//
//   - the tenant's own value is what is served whenever it writes one
//     spelling — a subtree level written under the OTHER spelling must not
//     displace it;
//   - with no tenant key, the deepest level that writes the threshold under
//     any spelling is served (canonical wins inside one level);
//   - da-guard's redundant-override advice never names a key whose deletion
//     moves /metrics.
//
// Measured on main ca7adc10 before the fix: the tenant writing the retired
// spelling at 30 under a subtree writing the canonical one at 40 was served
// 40; and a subtree writing the retired spelling at 40 under a root writing
// the canonical one at 30 made the guard call the tenant's canonical 30
// redundant, while deleting it moved /metrics from 30 to 40.
//
// Round 2: a level writing `canonical: null` beside a legacy value, under a
// shallower level writing the canonical spelling, was "writes both" to the
// overlay and "writes the legacy one" to the guard — wrong advice, and the
// shallower value served. The null / disable rows hold that.

import (
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// subtreeAliasServed is the canonical threads_running warning value /metrics
// serves tx with sub/deep/tx.yaml set to body (noRow when there is none).
func subtreeAliasServed(t *testing.T, dir, body string) string {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "sub", "deep"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "sub", "deep", "tx.yaml"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, _, err := config.LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("LoadDir: %v", err)
	}
	for _, r := range cfg.Resolve() {
		if r.Tenant == "tx" && r.Component == "mysql" && r.Metric == "threads_running" && r.Severity == "warning" {
			return strconv.FormatFloat(r.Value, 'f', -1, 64)
		}
	}
	return noRow
}

const noRow = "<no row>"

func TestGuard_SubtreeDefaultsAcrossAliasSpellings(t *testing.T) {
	t.Parallel()
	C, L := aliasCanon, aliasLegacy
	// The root always declares the threshold: a key no root default names is
	// not served at all (#1976), which is a different question.
	root := []aliasLayer{{"rC30", [][2]string{{C, "30"}}}, {"rL30", [][2]string{{L, "30"}}}}
	// null and "disable" level values (#2414 round 2): a null writes
	// nothing, so the other spelling beside it is that level's only write;
	// "disable" is a value like any other and turns the threshold off.
	sub := []aliasLayer{{"s-", nil}, {"sC40", [][2]string{{C, "40"}}}, {"sL40", [][2]string{{L, "40"}}},
		{"sBoth", [][2]string{{C, "40"}, {L, "41"}}}, {"sCnullL41", [][2]string{{C, "null"}, {L, "41"}}},
		{"sLdis", [][2]string{{L, "disable"}}}}
	deep := []aliasLayer{{"d-", nil}, {"dC50", [][2]string{{C, "50"}}}, {"dL50", [][2]string{{L, "50"}}},
		{"dCnullL50", [][2]string{{C, "null"}, {L, "50"}}}, {"dLnullC50", [][2]string{{L, "null"}, {C, "50"}}},
		{"dCdis", [][2]string{{C, "disable"}}}, {"dLdis", [][2]string{{L, "disable"}}}}
	values := []string{"30", "40", "41", "50", "60"}

	// served is what the deepest level writing the threshold hands down:
	// canonical first inside one level. A null is no write; "disable"
	// serves no row.
	served := func(levels ...aliasLayer) string {
		for i := len(levels) - 1; i >= 0; i-- {
			var canon, legacy string
			for _, kv := range levels[i].kv {
				switch {
				case kv[1] == "null":
				case kv[0] == C:
					canon = kv[1]
				default:
					legacy = kv[1]
				}
			}
			v := canon
			if v == "" {
				v = legacy
			}
			if v == "disable" {
				return noRow
			}
			if v != "" {
				return v
			}
		}
		return noRow
	}

	var cells, missed int
	for _, r := range root {
		for _, s := range sub {
			for _, d := range deep {
				files := map[string]string{
					"_defaults.yaml":          "defaults:\n  pg_connections: 100\n" + r.lines("  "),
					"sub/_defaults.yaml":      "defaults:\n" + s.lines("  "),
					"sub/deep/_defaults.yaml": "defaults:\n" + d.lines("  "),
				}
				if s.kv == nil {
					delete(files, "sub/_defaults.yaml")
				}
				if d.kv == nil {
					delete(files, "sub/deep/_defaults.yaml")
				}
				head := "tenants:\n  tx:\n    pg_connections: 100\n"
				chain := served(r, s, d)
				for _, field := range []string{C, L} {
					for _, v := range values {
						dir := t.TempDir()
						testutil.WriteTree(t, dir, files)
						name := fmt.Sprintf("%s/%s/%s/%s=%s", r.name, s.name, d.name, field, v)
						without := subtreeAliasServed(t, dir, head)
						with := subtreeAliasServed(t, dir, head+"    "+field+": "+v+"\n")
						red := aliasRedundant(t, dir)
						cells++
						if with != v {
							t.Errorf("%s: /metrics served %q, want the tenant's own %s", name, with, v)
						}
						if without != chain {
							t.Errorf("%s: with no tenant key /metrics served %q, want the deepest level's %s", name, without, chain)
						}
						if red[field] && with != without {
							t.Errorf("WRONG ADVICE %s: %s called redundant, but deleting it moves /metrics %s → %s", name, field, with, without)
						}
						if !red[field] && with == without {
							missed++
						}
					}
				}
			}
		}
	}
	t.Logf("cells=%d missed-hint=%d (logged, not failed)", cells, missed)
}

// TestGuard_SubtreeNonThresholdSpellingIsNoWrite pins the walker half of
// levelWritesSpelling (#2414 round 3): a level carrying one spelling as a
// value that is not a threshold (`abc`) beside the other spelling's value
// writes only the latter — on /metrics the overlay skips `abc` and the level's
// other spelling displaces the shallower one. So a tenant writing that
// same value under that spelling IS redundant, and the guard must say so.
//
// ⛔ The product test above cannot see this: it fails only on wrong advice,
// and counting the non-threshold value as a write here costs a hint, not a
// wrong one. Measured with the walker's old predicate (nil only): the hint
// went missing and nothing else changed.
func TestGuard_SubtreeNonThresholdSpellingIsNoWrite(t *testing.T) {
	t.Parallel()
	C, L := aliasCanon, aliasLegacy
	for _, sub := range []string{"", C + ": 40\n", L + ": 40\n"} {
		for _, pair := range [][2]string{{C, L}, {L, C}} {
			junk, kept := pair[0], pair[1]
			name := fmt.Sprintf("sub=%q/deep=%s:abc,%s:50", sub, junk, kept)
			files := map[string]string{
				"_defaults.yaml":          "defaults:\n  pg_connections: 100\n  " + C + ": 30\n",
				"sub/deep/_defaults.yaml": "defaults:\n  " + junk + ": abc\n  " + kept + ": 50\n",
			}
			if sub != "" {
				files["sub/_defaults.yaml"] = "defaults:\n  " + sub
			}
			dir := t.TempDir()
			testutil.WriteTree(t, dir, files)
			head := "tenants:\n  tx:\n    pg_connections: 100\n"
			without := subtreeAliasServed(t, dir, head)
			with := subtreeAliasServed(t, dir, head+"    "+kept+": 50\n")
			if with != "50" || without != "50" {
				t.Fatalf("%s: precondition — /metrics with=%s without=%s, want 50 both", name, with, without)
			}
			if !aliasRedundant(t, dir)[kept] {
				t.Errorf("%s: %s at 50 is redundant (deleting it keeps /metrics at 50) but was not reported", name, kept)
			}
		}
	}
}
