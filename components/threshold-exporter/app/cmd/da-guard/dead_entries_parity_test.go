package main

// #3718: the write gate (config.ValidateTenantKeys, what tenant-api and
// `da-guard write` refuse) and the shared key verdict (keyverdict.Judge,
// what da-guard key-verdict blocks on) must agree on WHICH keys' dead
// schedule entries are reported — one question, "does the resolver read
// this key", answered twice. #3716's review found a layout where they did
// not (a dimensional key whose base is declared only under `dimensions:`);
// this matrix keeps every layout × key shape in step, so a new path that
// moves one of them turns it red. Tenant ids are fixture names (CLAUDE.md #9).

import (
	"io"
	"log"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// deadSched is a schedule whose second window is inside its first: one
// dead entry wherever the key is read.
const deadSched = ":\n      default: \"60\"\n      overrides:\n" +
	"        - window: \"01:00-03:00\"\n          value: \"50\"\n" +
	"        - window: \"01:00-02:00\"\n          value: \"40\"\n"

func TestDeadEntries_WriteGateAndKeyVerdictAgree(t *testing.T) {
	t.Parallel()
	layouts := []struct{ name, root string }{
		{"valued", "defaults:\n  mysql_connections: 80\n  container_cpu: 70\ndimensions:\n  container_cpu: [version]\n"},
		{"optional", "defaults:\n  mysql_connections: 80\noptional_overrides: [container_cpu]\ndimensions:\n  container_cpu: [version]\n"},
		{"dimensions_only", "defaults:\n  mysql_connections: 80\ndimensions:\n  container_cpu: [version]\n"},
		{"undeclared", "defaults:\n  mysql_connections: 80\n"},
		// A `<key>_critical` valued in its own right: the base phase reads it
		// as a plain key, whatever its base.
		{"critical_direct", "defaults:\n  mysql_connections: 80\n  container_cpu_critical: 90\n"},
		{"critical_direct_dims", "defaults:\n  mysql_connections: 80\n  container_cpu_critical: 90\n" +
			"dimensions:\n  container_cpu: [version]\n"},
	}
	reported := 0
	keys := []string{
		"container_cpu",
		"container_cpu_critical",
		`container_cpu{version="v2"}`,
		`container_cpu{region="x"}`, // a label `dimensions:` does not list
	}
	at := time.Date(2026, 7, 1, 1, 30, 0, 0, time.UTC)
	for _, l := range layouts {
		for _, k := range keys {
			name := l.name + "/" + k
			root := writeTree(t, map[string]string{
				"_defaults.yaml": l.root,
				"a.yaml":         "tenants:\n  t1:\n    '" + k + "'" + deadSched,
			})

			cfg, _, err := config.LoadDir(root, log.New(io.Discard, "", 0))
			if err != nil {
				t.Fatalf("%s: load: %v", name, err)
			}
			gate := 0
			for _, f := range cfg.ValidateTenantKeys().Values {
				if strings.Contains(f.Message, "writes a schedule with entries /metrics serves at no minute") {
					gate++
				}
			}

			opts, err := treeFacts(root)
			if err != nil {
				t.Fatalf("%s: tree facts: %v", name, err)
			}
			res, err := keyVerdictOf(root, opts, at)
			if err != nil || !res.judged {
				t.Fatalf("%s: key verdict: judged=%v err=%v", name, res.judged, err)
			}
			verdict := 0
			for _, f := range res.tree.Findings {
				if f.Kind == "dead_entry" {
					verdict++
				}
			}
			reported += gate
			if gate != verdict {
				t.Errorf("%s: the write gate reports %d dead-entry finding(s), the key verdict %d", name, gate, verdict)
			}
		}
	}
	// The schedule is dead wherever a key is read: a matrix whose every cell
	// reads 0 on both sides compares nothing.
	if reported == 0 {
		t.Fatal("no cell reported a dead entry: the fixture no longer has one")
	}
}
