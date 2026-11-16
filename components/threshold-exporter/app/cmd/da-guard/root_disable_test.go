package main

// root_disable_test.go — the root opt-out of a built-in key (#3832 decision
// 3): the conf.d root `defaults:` writes a default-armed key as `disable`,
// and every tenant inherits it as it inherits a subtree level's — the key's
// whole family off until a subtree, a profile or the tenant gives the key a
// value. Judged end to end through da-guard's own subcommands, so /metrics
// (served-values), /effective (effective), the key verdict and the main gate
// answer from the same tree.

import (
	"encoding/json"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// familyRows is every key of base's family (base itself, its `_critical`,
// its dimensional keys) /metrics serves tenant a row for, sorted.
func familyRows(doc servedOut, tenant, base string) []string {
	var out []string
	for k := range doc.Tenants[tenant].Values {
		if k == base || k == base+"_critical" || strings.HasPrefix(k, base+"{") {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

const rootDisableDefaults = "defaults:\n  pg_replication_lag: disable\n  redis_connected_clients: 1000\n" +
	"dimensions:\n  pg_replication_lag: [db]\n"

func TestRootDisable_SwitchesTheFamilyOffForEveryTenantThatGivesNoValue(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": rootDisableDefaults,
		"_profiles.yaml": "profiles:\n  p:\n    pg_replication_lag: \"40\"\n",
		// t-inherit: writes only the family's other keys — they go with the base.
		"t-inherit.yaml": "tenants:\n  t-inherit:\n    pg_replication_lag_critical: \"60\"\n" +
			"    pg_replication_lag{db=\"a\"}: \"20\"\n",
		// t-own: gives the base a value — the whole family comes back.
		"t-own.yaml": "tenants:\n  t-own:\n    pg_replication_lag: \"25\"\n    pg_replication_lag_critical: \"60\"\n" +
			"    pg_replication_lag{db=\"a\"}: \"20\"\n",
		// t-sub: a subtree level gives the base a value.
		"team/_defaults.yaml": "defaults:\n  pg_replication_lag: 35\n",
		"team/t-sub.yaml":     "tenants:\n  t-sub:\n    pg_replication_lag_critical: \"60\"\n",
		// t-prof: a profile gives the base a value.
		"t-prof.yaml": "tenants:\n  t-prof:\n    _profile: p\n    pg_replication_lag_critical: \"60\"\n",
	}
	code, doc, dir, stderr := servedWith(t, files, "")
	mustOK(t, code, stderr)

	if got := familyRows(doc, "t-inherit", "pg_replication_lag"); len(got) != 0 {
		t.Errorf("t-inherit: the root disable left family rows %v", got)
	}
	// The root disable is no refusal: nothing to name.
	if len(doc.RootDefaultsRefused) != 0 {
		t.Errorf("root_defaults_refused = %+v, want none: `disable` of a built-in key is read", doc.RootDefaultsRefused)
	}
	for tenant, base := range map[string]float64{"t-own": 25, "t-sub": 35, "t-prof": 40} {
		wantValue(t, doc, tenant, "pg_replication_lag", base)
		if sev := doc.Tenants[tenant].Severities["pg_replication_lag_critical"]; sev != "critical" {
			t.Errorf("%s: pg_replication_lag_critical severity %q, want critical (the family is back)", tenant, sev)
		}
	}
	if got := familyRows(doc, "t-own", "pg_replication_lag"); len(got) != 3 {
		t.Errorf("t-own: family rows %v, want the base, its _critical and its dimensional key", got)
	}
	// Other built-in keys are untouched.
	want, _ := config.BuiltinDefault("container_cpu")
	wantValue(t, doc, "t-inherit", "container_cpu", want)

	et := builtinEffectiveOf(t, dir, "t-inherit")
	if got := et.EffectiveConfig["pg_replication_lag"]; got != "disable" {
		t.Errorf("effective pg_replication_lag = %v, want disable (the root's)", got)
	}
	if src := et.KeySources["pg_replication_lag"]; src.Layer != config.KeyLayerDefaults || src.File != "_defaults.yaml" {
		t.Errorf("key_sources[pg_replication_lag] = %+v, want the root file", src)
	}

	// The main gate: a root disable is no error.
	if gcode, gout, _ := runOnce(t, "--config-dir", dir); gcode != exitOK {
		t.Errorf("guard: exit %d, want %d:\n%s", gcode, exitOK, gout)
	}
}

func TestRootDisable_EffectiveNamesTheKeyOptedOut(t *testing.T) {
	t.Parallel()
	dir := builtinTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  container_memory: disable\n  pg_connections: 70\n",
		"t1.yaml":        "tenants:\n  t1: {}\n",
	})
	code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir)
	if code != exitOK {
		t.Fatalf("effective: exit %d; stderr=%q", code, stderr)
	}
	var doc struct {
		BuiltinDefaults struct {
			Keys map[string]struct {
				State     string   `json:"state"`
				RootValue *float64 `json:"root_value"`
				Tenants   *int     `json:"tenants"`
			} `json:"keys"`
		} `json:"builtin_defaults"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("effective: %v\n%s", err, stdout)
	}
	bd := doc.BuiltinDefaults
	cm := bd.Keys["container_memory"]
	if cm.State != config.BuiltinStateOptedOut || cm.RootValue != nil || cm.Tenants != nil {
		t.Errorf("container_memory = %+v, want state opted_out with no root_value and no tenants", cm)
	}
	if st := bd.Keys["pg_connections"].State; st != config.BuiltinStateOverridden {
		t.Errorf("control: pg_connections state %q, want overridden", st)
	}
	if st := bd.Keys["container_cpu"].State; st != config.BuiltinStateBuiltin {
		t.Errorf("control: container_cpu state %q, want builtin", st)
	}
}

func TestRootDisable_TheKeyVerdictReadsItAsAnInheritedDisable(t *testing.T) {
	t.Parallel()
	dir := builtinTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  pg_replication_lag: disable\n",
		"t1.yaml":        "tenants:\n  t1:\n    pg_replication_lag_critical: \"60\"\n",
	})
	_, stdout, stderr := runOnce(t, keyVerdictCmd, "--config-dir", dir)
	var doc struct {
		Blocks  bool `json:"blocks"`
		Tenants map[string][]struct {
			Key             string  `json:"key"`
			Verdict         string  `json:"verdict"`
			Disable         *string `json:"disable"`
			SwitchedOffWith *string `json:"switched_off_with"`
		} `json:"tenants"`
		Findings []struct {
			Kind     string `json:"kind"`
			Key      string `json:"key"`
			Severity string `json:"severity"`
		} `json:"findings"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("key-verdict: %v\n%s\n%s", err, stdout, stderr)
	}
	if doc.Blocks {
		t.Errorf("blocks: a root disable is an opt-out, not an unread key:\n%s", stdout)
	}
	byKey := map[string]int{}
	for i, v := range doc.Tenants["t1"] {
		byKey[v.Key] = i
	}
	base := doc.Tenants["t1"][byKey["pg_replication_lag"]]
	if base.Verdict != "read" || base.Disable == nil || *base.Disable != disableEffective {
		t.Errorf("pg_replication_lag: verdict %q disable %v, want read and effective", base.Verdict, base.Disable)
	}
	crit := doc.Tenants["t1"][byKey["pg_replication_lag_critical"]]
	if crit.SwitchedOffWith == nil || *crit.SwitchedOffWith != "pg_replication_lag" {
		t.Errorf("pg_replication_lag_critical: switched_off_with %v, want pg_replication_lag", crit.SwitchedOffWith)
	}
	cascade := false
	for _, f := range doc.Findings {
		if f.Kind == "disable_cascade" && f.Key == "pg_replication_lag_critical" && f.Severity == "warning" {
			cascade = true
		}
	}
	if !cascade {
		t.Errorf("no disable_cascade warning for the tenant's _critical: %+v", doc.Findings)
	}
}

// An expired tenant `disable` falls back to what the tenant inherits — the
// root's `disable` — not to the built-in value.
func TestRootDisable_ATenantDisablePastItsExpiryFallsBackToIt(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  pg_replication_lag: disable\n",
		"t1.yaml": "tenants:\n  t1:\n    pg_replication_lag:\n      default: disable\n      expires: \"2020-01-01T00:00:00Z\"\n" +
			"    pg_replication_lag_critical: \"60\"\n",
	}
	code, doc, _, stderr := servedWith(t, files, "2026-01-01T00:00:00Z")
	mustOK(t, code, stderr)
	if got := familyRows(doc, "t1", "pg_replication_lag"); len(got) != 0 {
		t.Errorf("family rows %v, want none: the lapsed disable falls back to the root's", got)
	}
}

func TestRootDisable_OnlyTheWordDisableOfABuiltinKeyIsAnOptOut(t *testing.T) {
	t.Parallel()
	builtin, _ := config.BuiltinDefault("container_memory")
	for name, tc := range map[string]struct {
		line, key, says string
		served          bool // the key keeps a row
	}{
		"false":                     {"container_memory: false", "container_memory", "write `container_memory: disable`", true},
		"off":                       {"container_memory: \"off\"", "container_memory", "write `container_memory: disable`", true},
		"disabled":                  {"container_memory: disabled", "container_memory", "write `container_memory: disable`", true},
		"disable of a declared key": {"redis_connected_clients: disable", "redis_connected_clients", "is not one", false},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{
				"_defaults.yaml": "defaults:\n  " + tc.line + "\n",
				"t1.yaml":        "tenants:\n  t1: {}\n",
			}
			code, doc, dir, stderr := servedWith(t, files, "")
			mustOK(t, code, stderr)
			if tc.served {
				wantValue(t, doc, "t1", tc.key, builtin)
			}
			gcode, gout, _ := runOnce(t, "--config-dir", dir)
			if gcode != exitFindings || !strings.Contains(gout, "root_default_refused") || !strings.Contains(gout, tc.says) {
				t.Errorf("guard: exit %d, want %d naming root_default_refused and %q:\n%s", gcode, exitFindings, tc.says, gout)
			}
		})
	}
	// Any letter case of the word is the word.
	code, doc, _, stderr := servedWith(t, map[string]string{
		"_defaults.yaml": "defaults:\n  container_memory: Disable\n",
		"t1.yaml":        "tenants:\n  t1: {}\n",
	}, "")
	mustOK(t, code, stderr)
	if _, served := doc.Tenants["t1"].Values["container_memory"]; served {
		t.Errorf("`Disable` served container_memory: %v", doc.Tenants["t1"].Values["container_memory"])
	}
}

func TestRootDisable_AValueOfItsFamilyBesideItIsAnError(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		extra, field string
	}{
		"_critical":   {"  pg_replication_lag_critical: 90\n", "_defaults.yaml:defaults.pg_replication_lag_critical"},
		"dimensional": {"  pg_replication_lag{db=\"a\"}: 20\n", "_defaults.yaml:defaults.pg_replication_lag{db=\"a\"}"},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := builtinTree(t, map[string]string{
				"_defaults.yaml": "defaults:\n  pg_replication_lag: disable\n" + tc.extra +
					"dimensions:\n  pg_replication_lag: [db]\n",
				"t1.yaml": "tenants:\n  t1: {}\n",
			})
			code, out, _ := runOnce(t, "--config-dir", dir)
			if code != exitFindings || !strings.Contains(out, "root_disable_family_value") || !strings.Contains(out, tc.field) {
				t.Errorf("exit %d, want %d naming root_disable_family_value at %s:\n%s", code, exitFindings, tc.field, out)
			}
		})
	}
	// Control: a null family value, and a family value beside a number.
	for name, body := range map[string]string{
		"null critical":  "defaults:\n  pg_replication_lag: disable\n  pg_replication_lag_critical: null\n",
		"valued base":    "defaults:\n  pg_replication_lag: 30\n  pg_replication_lag{db=\"a\"}: 20\ndimensions:\n  pg_replication_lag: [db]\n",
		"other disabled": "defaults:\n  container_memory: disable\n  pg_replication_lag{db=\"a\"}: 20\ndimensions:\n  pg_replication_lag: [db]\n",
	} {
		dir := builtinTree(t, map[string]string{"_defaults.yaml": body, "t1.yaml": "tenants:\n  t1: {}\n"})
		if _, out, _ := runOnce(t, "--config-dir", dir); strings.Contains(out, "root_disable_family_value") {
			t.Errorf("control %s: reported:\n%s", name, out)
		}
	}
}

func TestRootDisable_TheOptionalOverridesFixNamesTheRootDisable(t *testing.T) {
	t.Parallel()
	dir := builtinTree(t, map[string]string{
		"_defaults.yaml": "defaults: {}\noptional_overrides:\n  - mysql_replication_lag\n",
		"t1.yaml":        "tenants:\n  t1: {}\n",
	})
	_, out, _ := runOnce(t, "--config-dir", dir)
	if !strings.Contains(out, "root_optional_builtin") ||
		!strings.Contains(out, "`mysql_replication_lag: disable` under the root `defaults:`") {
		t.Errorf("root_optional_builtin does not point at the root disable:\n%s", out)
	}
}

// served-values keys[] names an inherited root disable as the tenant's
// `disable`, and --disable-effects judges it as one a subtree level hands
// the tenant: effective, its `_critical` switched off with it.
func TestRootDisable_ServedValuesKeysNameTheInheritedDisable(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  pg_replication_lag: disable\n",
		"t1.yaml":        "tenants:\n  t1:\n    pg_replication_lag_critical: \"60\"\n",
	}
	code, doc, _, stderr := servedWith(t, files, "", "--disable-effects")
	mustOK(t, code, stderr)
	keys := doc.Tenants["t1"].Keys
	if e := keys["pg_replication_lag"]; e.Reason != keyReasonDisabled || e.Disable != disableEffective {
		t.Errorf("keys[pg_replication_lag] = %+v, want reason disabled, disable effective", e)
	}
	if e := keys["pg_replication_lag_critical"]; e.SwitchedOffWith != "pg_replication_lag" {
		t.Errorf("keys[pg_replication_lag_critical] = %+v, want switched_off_with pg_replication_lag", e)
	}
}
