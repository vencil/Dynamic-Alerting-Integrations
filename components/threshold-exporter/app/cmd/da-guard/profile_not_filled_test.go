package main

// #3395: a key the tenant's profile writes that the root declares without a
// platform value (`optional_overrides:` only) is not filled in (#1189) —
// `da-guard effective` names it in not_served as profile_not_filled with the
// profile file, and the full check does not turn it into an error
// (value_not_served or otherwise): its severity is ADR-038's shared verdict
// (#3394), not this reason's.
//
// Seams: none — t.TempDir() trees through run().

import (
	"encoding/json"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

func profileNotFilledFiles(profileBody string) map[string]string {
	return map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [kafka_lag]\n",
		"_profiles.yaml": "profiles:\n  gold:\n    " + profileBody + "\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
	}
}

func TestEffective_ProfileNotFilledIsNamed(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  map[string]config.NotServedKey
	}{
		{name: "the profile alone writes the declared key", files: profileNotFilledFiles(`kafka_lag: "7"`),
			want: map[string]config.NotServedKey{"kafka_lag": {Reason: config.NotServedProfileNotFilled, File: "_profiles.yaml"}}},
		{name: "the tenant writes it too", files: func() map[string]string {
			f := profileNotFilledFiles(`kafka_lag: "7"`)
			f["tx.yaml"] = "tenants:\n  tx:\n    _profile: gold\n    kafka_lag: \"5\"\n"
			return f
		}(), want: map[string]config.NotServedKey{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			testutil.WriteTree(t, dir, tc.files)
			code, doc, stderr := runEffectiveOn(t, dir)
			if code != exitOK {
				t.Fatalf("exit = %d, want %d; stderr=%s", code, exitOK, stderr)
			}
			var tx struct {
				EffectiveConfig map[string]any                 `json:"effective_config"`
				NotServed       map[string]config.NotServedKey `json:"not_served"`
			}
			if err := json.Unmarshal(doc.Tenants["tx"], &tx); err != nil {
				t.Fatalf("tenant tx: %v (%s)", err, doc.Tenants["tx"])
			}
			if !reflect.DeepEqual(tx.NotServed, tc.want) {
				t.Errorf("not_served = %v, want %v", tx.NotServed, tc.want)
			}
			if _, named := tc.want["kafka_lag"]; named {
				if v, shown := tx.EffectiveConfig["kafka_lag"]; shown {
					t.Errorf("effective_config shows kafka_lag = %v; no plane applies it", v)
				}
			}
			assertMatchesResolveEffective(t, dir, doc)
		})
	}
}

// TestGuard_ProfileNotFilledAddsNoError: the full check over the tree finds
// the same errors as over the same tree whose profile writes no declared
// key, and no value_not_served.
func TestGuard_ProfileNotFilledAddsNoError(t *testing.T) {
	t.Parallel()
	errorsOf := func(files map[string]string) []reportedFinding {
		_, got := guardFindingsOf(t, files)
		var out []reportedFinding
		for _, f := range got {
			if f.Kind == findingkind.FindingValueNotServed.String() {
				t.Errorf("value_not_served: %+v", f)
			}
			if f.Severity == guard.SeverityError {
				out = append(out, f)
			}
		}
		return out
	}
	withKey := errorsOf(profileNotFilledFiles(`kafka_lag: "7"`))
	without := errorsOf(profileNotFilledFiles(`mysql_connections: "60"`))
	if !reflect.DeepEqual(withKey, without) {
		t.Errorf("errors with the profile's declared key %+v, without it %+v", withKey, without)
	}
	// The run's own exit code: no error, so no exit 1.
	tmp := t.TempDir()
	tree := map[string]string{}
	for k, v := range profileNotFilledFiles(`kafka_lag: "7"`) {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	if code, _, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d")); code == exitFindings && len(without) == 0 {
		t.Errorf("exit = %d with no error over the tree without the key; stderr=%s", code, stderr)
	}
}
