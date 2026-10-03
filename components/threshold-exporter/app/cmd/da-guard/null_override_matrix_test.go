package main

// #2518: a threshold written as YAML null, at every layer that can carry one,
// in both #1231 spellings. Owner's ruling (option a): a null means "this
// layer has no value — use the next layer down", the walker's rule.
//
// The product: root `_defaults.yaml` × `sub/_defaults.yaml` × the elected
// profile × a root platform file's `tenants:` entry, each absent / a value
// in either spelling / null in either spelling, × the tenant file (in sub/)
// absent / null in either spelling / one of the values in either spelling.
// Per cell:
//
//  1. the walker's per-threshold value (`da-guard effective`: the
//     effective_config spelling whose key_sources layer is highest) is the
//     model's: the topmost layer writing a non-null value;
//  2. /metrics (servedValues over LoadDirReport — exactly `da-guard
//     served-values`, the collector's own Gather) serves that same value
//     when the root declares the threshold. When the root does not (absent,
//     or — the #2518 root cell — null), nothing is served, and a value the
//     tenant's effective config still shows must be NAMED: by the
//     subtree_default_undeliverable finding, or in served-values `unserved`
//     — and, when the root writes null, by exactly one da-guard finding
//     (subtree_default_undeliverable for a subtree-only value,
//     root_default_null_undeclared for a tenant-side one);
//  3. no wrong advice: when da-guard calls the tenant's key
//     redundant_override, deleting it (the same cell with no tenant key)
//     serves the same.
//
// Every layer's null is therefore held to "same as absent" by (1)+(2): the
// model has no other way to see it.
//
// Measured when written (the full run): 8125 cells. On main f283c4e9 (the
// pkg/config half of the fix reverted) 3942 wrong: 3250 root-null cells
// serving 0, 464 served values that fell back past a sub / profile /
// platform value to the root, 900 walker values that did the same (a
// platform or profile null counted as owning the key), 96 wrong
// redundant_override advice. With the fix 0. `-short` runs the tenant
// values 40 and 90 only.

import (
	"fmt"
	"io"
	"log"
	"sync/atomic"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// nullCell is one layer's state: absent (key ""), or key written as value
// ("null" = YAML null).
type nullCell struct{ key, value string }

func (c nullCell) name(tag string) string {
	switch c.key {
	case "":
		return tag + "-"
	case aliasCanon:
		return tag + "C" + c.value
	default:
		return tag + "L" + c.value
	}
}

func (c nullCell) line(indent string) string {
	if c.key == "" {
		return ""
	}
	return indent + c.key + ": " + c.value + "\n"
}

// writes reports the layer's value when it writes a non-null one.
func (c nullCell) writes() (string, bool) {
	if c.key == "" || c.value == "null" {
		return "", false
	}
	return c.value, true
}

// nullLayerStates is absent, value in either spelling, null in either.
func nullLayerStates(v string) []nullCell {
	return []nullCell{{}, {aliasCanon, v}, {aliasLegacy, v}, {aliasCanon, "null"}, {aliasLegacy, "null"}}
}

// nullMatrixObs is one tenant-file state's readings.
type nullMatrixObs struct {
	served, walker string // the value, or noRow
	named          bool   // an unserved effective value is named (rule 2)
	findings       int    // subtree_default_undeliverable + root_default_null_undeclared for the key
	redundant      bool   // da-guard calls the tenant's key redundant
}

func nullMatrixObserve(t *testing.T, files map[string]string, tenantKey string) nullMatrixObs {
	t.Helper()
	dir := t.TempDir()
	testutil.WriteTree(t, dir, files)
	var o nullMatrixObs

	cfg, rep, err := config.LoadDirReport(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("LoadDirReport: %v", err)
	}
	sv, err := servedValues(cfg, time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC), rep.Undeliverable, false)
	if err != nil {
		t.Fatalf("servedValues: %v", err)
	}
	o.served = noRow
	if v, ok := sv["tx"].Values[aliasCanon]; ok {
		o.served = fmt.Sprint(v)
	}
	for _, k := range []string{aliasCanon, aliasLegacy} {
		if _, ok := sv["tx"].Unserved[k]; ok {
			o.named = true
		}
	}

	tree, err := config.EffectiveTree(dir)
	if err != nil {
		t.Fatalf("EffectiveTree: %v", err)
	}
	var ec *config.EffectiveConfig
	for _, e := range tree.Tenants {
		if e.TenantID == "tx" {
			ec = e
		}
	}
	if ec == nil {
		t.Fatalf("tx not in the effective tree")
	}
	o.walker = noRow
	best := -1
	for _, k := range []string{aliasCanon, aliasLegacy} {
		v, ok := ec.EffectiveConfig[k]
		if !ok {
			continue
		}
		if r := nullLayerRank(ec.KeySources[k]); r > best {
			best = r
			o.walker = fmt.Sprint(v)
		}
	}

	report, err := guard.CheckDefaultsImpact(buildCheckInput(tree, &flags{}))
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range report.Findings {
		if f.TenantID != "tx" {
			continue
		}
		switch {
		case f.Kind == guard.FindingRedundantOverride && f.Field == tenantKey && tenantKey != "":
			o.redundant = true
		case (f.Kind == guard.FindingSubtreeDefaultUndeliverable || f.Kind == guard.FindingRootDefaultNullUndeclared) &&
			(f.Field == aliasCanon || f.Field == aliasLegacy):
			o.named = true
			o.findings++
		}
	}
	return o
}

// nullLayerRank orders the walker's attribution: tenant over platform over
// profile over the defaults chain, a deeper chain level over a shallower.
func nullLayerRank(ks config.KeySource) int {
	switch ks.Layer {
	case config.KeyLayerTenant:
		return 1000
	case config.KeyLayerPlatform:
		return 900
	case config.KeyLayerProfile:
		return 800
	}
	if ks.Level != nil {
		return *ks.Level
	}
	return 0
}

func TestGuard_NullOverrideIsNoWriteOnEveryLayer(t *testing.T) {
	t.Parallel()
	C, L := aliasCanon, aliasLegacy
	values := []string{"30", "40", "50", "70", "90"}
	if testing.Short() {
		values = []string{"40", "90"}
	}
	tenants := []nullCell{{}, {C, "null"}, {L, "null"}}
	for _, v := range values {
		tenants = append(tenants, nullCell{C, v}, nullCell{L, v})
	}

	var cells, wrong atomic.Int64
	for _, root := range nullLayerStates("30") {
		for _, sub := range nullLayerStates("40") {
			t.Run(root.name("r")+"/"+sub.name("s"), func(t *testing.T) {
				t.Parallel()
				for _, prof := range nullLayerStates("50") {
					for _, plat := range nullLayerStates("70") {
						files := map[string]string{
							"_defaults.yaml":     "defaults:\n  pg_connections: 100\n" + root.line("  "),
							"_profiles.yaml":     "profiles:\n  std:\n    pg_connections: 100\n" + prof.line("    "),
							"sub/_defaults.yaml": "defaults:\n  pg_connections: 100\n" + sub.line("  "),
						}
						if plat.key != "" {
							files["_platform.yaml"] = "tenants:\n  tx:\n" + plat.line("    ")
						}
						var without string // served with no tenant key
						for _, ten := range tenants {
							files["sub/tx.yaml"] = "tenants:\n  tx:\n    _profile: std\n    pg_connections: 100\n" + ten.line("    ")
							o := nullMatrixObserve(t, files, ten.key)
							if ten.key == "" {
								without = o.served
							}
							name := fmt.Sprintf("%s/%s/%s/%s/%s", root.name("r"), sub.name("s"), prof.name("p"), plat.name("a"), ten.name("t"))

							// The model: the topmost layer writing a non-null value.
							want := noRow
							for _, layer := range []nullCell{ten, plat, prof, sub, root} {
								if v, ok := layer.writes(); ok {
									want = v
									break
								}
							}
							_, rootDeclares := root.writes()

							bad := false
							if o.walker != want {
								bad = true
								t.Errorf("%s: walker value %s, want %s (the topmost non-null layer)", name, o.walker, want)
							}
							switch {
							case rootDeclares && o.served != want:
								bad = true
								t.Errorf("%s: /metrics served %s, want %s (the topmost non-null layer)", name, o.served, want)
							case !rootDeclares && o.served != noRow:
								bad = true
								t.Errorf("%s: /metrics served %s for a threshold the root does not declare", name, o.served)
							case !rootDeclares && want != noRow && !o.named:
								bad = true
								t.Errorf("%s: effective shows %s, /metrics serves nothing, and nothing names it", name, want)
							case root.value == "null" && want != noRow && o.findings != 1:
								// The root null is the cause: exactly one finding names it
								// (subtree_default_undeliverable for a subtree-only value,
								// root_default_null_undeclared for a tenant-side one).
								bad = true
								t.Errorf("%s: root null with effective %s: %d findings name it, want exactly 1", name, want, o.findings)
							}
							if o.redundant && o.served != without {
								bad = true
								t.Errorf("WRONG ADVICE %s: %s called redundant, but deleting it moves /metrics %s → %s", name, ten.key, o.served, without)
							}
							cells.Add(1)
							if bad {
								wrong.Add(1)
							}
						}
					}
				}
			})
		}
	}
	t.Cleanup(func() {
		t.Logf("cells=%d wrong=%d", cells.Load(), wrong.Load())
	})
}
