package main

// served_config_facts_test.go — served-values --config-facts (#3918): each
// tenant's fields the exporter reads at no instant, read without resolving
// or gathering anything. A reader whose verdict must not depend on when it
// runs (the route generator's custom-alert delivery check, the retire-drift
// lint) reads them here: the default
// mode exits 2 at an instant /metrics cannot be gathered at.

import (
	"encoding/json"
	"os"
	"os/exec"
	"reflect"
	"slices"
	"strings"
	"testing"
)

// configFactsTree is windowCollisionTree (a 01:00–03:00 part of the day
// /metrics cannot be gathered in) plus a tenant with a custom alert and
// `_metadata`, one whose `_custom_alerts` does not decode, and one whose
// `_metadata` does not decode.
var configFactsTree = map[string]string{
	"_defaults.yaml": windowCollisionTree["_defaults.yaml"],
	"tx.yaml":        windowCollisionTree["tx.yaml"],
	"tc.yaml": "tenants:\n  tc:\n    _metadata:\n      db_type: mariadb\n      owner: team-c\n    _custom_alerts:\n" +
		"      - recipe: threshold\n        name: q_high\n        metric: q_high_depth\n        op: \">\"\n" +
		"        window: 5m\n        threshold: \"1000:warning\"\n",
	"tbad.yaml":  "tenants:\n  tbad:\n    _custom_alerts: \"oops\"\n",
	"tmeta.yaml": "tenants:\n  tmeta:\n    _metadata:\n      owner:\n        x: y\n",
	"tr.yaml": "tenants:\n  tr:\n    _routing:\n      receiver:\n        type: webhook\n" +
		"        url: \"https://hooks.example.com/tr\"\n      group_wait: 10s\n",
}

// factsFields are the fields --config-facts writes for a tenant, compared
// with the default mode's: custom_alerts_error appears only beside a block
// that does not decode, so it is compared as present-or-absent too.
var factsFields = []string{"custom_alerts", "custom_alerts_error", "custom_alerts_finding", "routing", "metadata"}

func factsTenants(t *testing.T, stdout string) map[string]map[string]json.RawMessage {
	t.Helper()
	var doc struct {
		Tenants map[string]map[string]json.RawMessage `json:"tenants"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatal(err)
	}
	return doc.Tenants
}

// TestServedConfigFacts_NoInstantAndTheDefaultModesFields: --config-facts
// exits 0 on a tree whose /metrics cannot be gathered in part of the day
// (the default mode exits 2 at an instant there), writes nothing but its
// fields, and each field is what the default mode writes at an instant
// /metrics can be gathered at — `_metadata` as `values._metadata` — also for
// a `_custom_alerts` and a `_metadata` that do not decode.
func TestServedConfigFacts_NoInstantAndTheDefaultModesFields(t *testing.T) {
	t.Parallel()
	dir := writeConfD(t, configFactsTree)
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--config-facts")
	mustOK(t, code, stderr)
	facts := factsTenants(t, stdout)

	// The default mode at an instant inside the colliding part: exit 2.
	if code, _, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", "2026-11-14T01:30:00Z"); code != exitCallerErr || !strings.Contains(stderr, "HTTP 500") {
		t.Fatalf("default mode inside the window: exit %d, stderr %q, want exit %d", code, stderr, exitCallerErr)
	}
	code, full, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", "2026-11-14T05:00:00Z")
	mustOK(t, code, stderr)
	values := factsTenants(t, full)
	if !reflect.DeepEqual(keysOf(facts), keysOf(values)) {
		t.Fatalf("--config-facts has tenants %v, the default mode %v", keysOf(facts), keysOf(values))
	}
	for tenant, f := range facts {
		for field := range f {
			if !slices.Contains(factsFields, field) {
				t.Errorf("tenant %s carries %q, not one of %v", tenant, field, factsFields)
			}
		}
		var vals struct {
			Metadata json.RawMessage `json:"_metadata"`
		}
		if err := json.Unmarshal(values[tenant]["values"], &vals); err != nil {
			t.Fatal(err)
		}
		theirs := map[string]json.RawMessage{"metadata": vals.Metadata}
		for _, field := range factsFields[:4] {
			theirs[field] = values[tenant][field]
		}
		for _, field := range factsFields {
			mine, okMine := f[field]
			other, okOther := theirs[field]
			if okMine != (okOther && other != nil) {
				t.Errorf("tenant %s: %s present %v in --config-facts, %v in the default mode", tenant, field, okMine, okOther)
				continue
			}
			var x, y any
			if okMine {
				if err := json.Unmarshal(mine, &x); err != nil {
					t.Fatal(err)
				}
				if err := json.Unmarshal(other, &y); err != nil {
					t.Fatal(err)
				}
			}
			if !reflect.DeepEqual(x, y) {
				t.Errorf("tenant %s: %s differs:\n--config-facts %s\ndefault mode   %s", tenant, field, mine, other)
			}
		}
	}
	// The cases are there to be compared: a served alert, a block that does
	// not decode, and a `_metadata` read as written.
	if !strings.Contains(string(facts["tc"]["custom_alerts"]), `"served"`) ||
		facts["tbad"]["custom_alerts_error"] == nil ||
		!strings.Contains(string(facts["tc"]["metadata"]), `"mariadb"`) ||
		!strings.Contains(string(facts["tr"]["routing"]), `"https://hooks.example.com/tr"`) {
		t.Fatalf("fixture did not exercise what it is for: %s", stdout)
	}
}

// TestServedConfigFacts_RefusesFlagsItWouldNotRead: --config-facts reads no
// instant and no day, so --at, --schedules, --schedules-only and
// --disable-effects beside it are caller errors (exit 2), never ignored.
func TestServedConfigFacts_RefusesFlagsItWouldNotRead(t *testing.T) {
	t.Parallel()
	dir := writeConfD(t, configFactsTree)
	for _, extra := range [][]string{{"--at", "2026-11-14T05:00:00Z"}, {"--schedules"}, {"--schedules-only"}, {"--disable-effects"}} {
		args := append([]string{servedValuesCmd, "--config-dir", dir, "--config-facts"}, extra...)
		code, stdout, stderr := runOnce(t, args...)
		if code != exitCallerErr || stdout != "" || !strings.Contains(stderr, extra[0]+" has nothing to do") {
			t.Errorf("%v: exit %d, stdout %q, stderr %q; want exit %d naming %s", extra, code, stdout, stderr, exitCallerErr, extra[0])
		}
	}
}

// TestServedConfigFacts_TenantSubsetAndOneWarnAndNoInstant: --tenant
// narrows --config-facts as it narrows the default mode (the same tenants,
// tenants_requested and tenants_absent); the resolver's WARN on a
// `_metadata` that does not decode is printed once, as the default mode
// prints it; and the document carries no `at` — it reads no instant.
func TestServedConfigFacts_TenantSubsetAndOneWarnAndNoInstant(t *testing.T) {
	t.Parallel()
	dir := writeConfD(t, configFactsTree)
	type subsetDoc struct {
		Tenants   map[string]json.RawMessage `json:"tenants"`
		Requested []string                   `json:"tenants_requested"`
		Absent    []string                   `json:"tenants_absent"`
	}
	read := func(args ...string) (subsetDoc, string) {
		code, stdout, stderr := runOnce(t, append([]string{servedValuesCmd, "--config-dir", dir}, args...)...)
		mustOK(t, code, stderr)
		var d subsetDoc
		if err := json.Unmarshal([]byte(stdout), &d); err != nil {
			t.Fatal(err)
		}
		return d, stderr
	}
	facts, _ := read("--config-facts", "--tenant", "tc", "--tenant", "nope")
	values, _ := read("--at", "2026-11-14T05:00:00Z", "--tenant", "tc", "--tenant", "nope")
	if !reflect.DeepEqual(keysOf(facts.Tenants), []string{"tc"}) || !reflect.DeepEqual(keysOf(facts.Tenants), keysOf(values.Tenants)) ||
		!reflect.DeepEqual(facts.Requested, values.Requested) || !reflect.DeepEqual(facts.Absent, []string{"nope"}) ||
		!reflect.DeepEqual(facts.Absent, values.Absent) {
		t.Errorf("--config-facts subset %v/%v/%v, default mode %v/%v/%v", keysOf(facts.Tenants), facts.Requested, facts.Absent,
			keysOf(values.Tenants), values.Requested, values.Absent)
	}

	// The resolver logs through the process-global logger, which only a child
	// process's stderr shows (TestServedValuesChildProcess).
	cmd := exec.Command(os.Args[0], "-test.run=^TestServedValuesChildProcess$")
	cmd.Env = append(os.Environ(), servedValuesChildEnv+"="+strings.Join(
		[]string{servedValuesCmd, "--config-dir", dir, "--config-facts"}, "\n"))
	var stdout, stderr strings.Builder
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("child: %v\nstderr:\n%s", err, stderr.String())
	}
	if n := strings.Count(stderr.String(), "failed to parse _metadata"); n != 1 {
		t.Errorf("the `_metadata` WARN printed %d times, want once:\n%s", n, stderr.String())
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal([]byte(stdout.String()), &doc); err != nil {
		t.Fatal(err)
	}
	if _, ok := doc["at"]; ok {
		t.Errorf("--config-facts document carries at = %s; it reads no instant", doc["at"])
	}
}
