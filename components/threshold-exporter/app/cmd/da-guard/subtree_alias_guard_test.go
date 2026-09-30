package main

// #2414: subtree `_defaults.yaml` levels × the #1231 alias spellings. The
// chain is root → sub → sub/deep, the tenant file sits in sub/deep, and each
// level writes the threshold in the canonical spelling, the retired one, both,
// or not at all. The oracle is /metrics (LoadDir + Resolve):
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
// serves tx with sub/deep/tx.yaml set to body ("" = no row).
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
	return ""
}

func TestGuard_SubtreeDefaultsAcrossAliasSpellings(t *testing.T) {
	t.Parallel()
	C, L := aliasCanon, aliasLegacy
	// The root always declares the threshold: a key no root default names is
	// not served at all (#1976), which is a different question.
	root := []aliasLayer{{"rC30", [][2]string{{C, "30"}}}, {"rL30", [][2]string{{L, "30"}}}}
	sub := []aliasLayer{{"s-", nil}, {"sC40", [][2]string{{C, "40"}}}, {"sL40", [][2]string{{L, "40"}}},
		{"sBoth", [][2]string{{C, "40"}, {L, "41"}}}}
	deep := []aliasLayer{{"d-", nil}, {"dC50", [][2]string{{C, "50"}}}, {"dL50", [][2]string{{L, "50"}}}}
	values := []string{"30", "40", "41", "50", "60"}

	// served is what the deepest level writing the threshold hands down:
	// canonical first inside one level.
	served := func(levels ...aliasLayer) string {
		for i := len(levels) - 1; i >= 0; i-- {
			var legacy string
			for _, kv := range levels[i].kv {
				if kv[0] == C {
					return kv[1]
				}
				legacy = kv[1]
			}
			if legacy != "" {
				return legacy
			}
		}
		return ""
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
						if chain != "" && without != chain {
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
