package main

// schedule_null_test.go — #2708: a null inside a schedule-shaped threshold.
//
// Owner's ruling: `{default: null}` (and `{default: null, overrides: []}`)
// is exactly plain null — no write at that layer, the layer below shows
// through — on /metrics (`served-values`) and on the walker (`effective`,
// the guard). A null inside a schedule that HAS windows (a window's
// `value: null`, or a null `default:` beside windows) is refused: one
// schedule_null_value error naming file, tenant and key. Runtime behaviour
// for those shapes is not defined here and not tested.
//
// Measured on main 990f112f before the fix (tenant in sub/, root 30, sub
// 40): plain null served 40 / effective 40 (defaults layer); `{default:
// null}` served 30 / effective `{default: null}` from the tenant layer; the
// window-null schedule exited 0 with no finding.
//
// Seams: none — t.TempDir() trees through run().

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

const scheduleNullAt = "2026-10-01T12:00:00Z"

// scheduleNullLayer is one layer that can carry the value: the files of a
// tree where that layer writes v (a YAML flow value) for mysql_connections,
// the value the layer below gives, and where it comes from on the walker.
type scheduleNullLayer struct {
	name     string
	files    func(v string) map[string]string
	below    float64
	layer    string // config.KeyLayer* of the value below
	file     string // its file
	finding  string // the schedule_null_value Field when v is refused
	tenantID string // the finding's TenantID
}

func scheduleNullLayers() []scheduleNullLayer {
	const root = "defaults:\n  mysql_connections: 30\n"
	return []scheduleNullLayer{
		{
			name: "tenant",
			files: func(v string) map[string]string {
				return map[string]string{
					"_defaults.yaml":     root,
					"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
					"sub/tx.yaml":        "tenants:\n  tx:\n    mysql_connections: " + v + "\n",
				}
			},
			below: 40, layer: config.KeyLayerDefaults, file: "sub/_defaults.yaml",
			finding: "sub/tx.yaml:tenants.tx.mysql_connections", tenantID: "tx",
		},
		{
			name: "platform",
			files: func(v string) map[string]string {
				return map[string]string{
					"_defaults.yaml":     root,
					"_profiles.yaml":     "profiles:\n  std:\n    mysql_connections: 50\n",
					"_platform.yaml":     "tenants:\n  tx:\n    mysql_connections: " + v + "\n",
					"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
					"sub/tx.yaml":        "tenants:\n  tx:\n    _profile: std\n",
				}
			},
			below: 50, layer: config.KeyLayerProfile, file: "_profiles.yaml",
			finding: "_platform.yaml:tenants.tx.mysql_connections", tenantID: "tx",
		},
		{
			name: "profile",
			files: func(v string) map[string]string {
				return map[string]string{
					"_defaults.yaml":     root,
					"_profiles.yaml":     "profiles:\n  std:\n    mysql_connections: " + v + "\n",
					"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
					"sub/tx.yaml":        "tenants:\n  tx:\n    _profile: std\n",
				}
			},
			below: 40, layer: config.KeyLayerDefaults, file: "sub/_defaults.yaml",
			finding: "_profiles.yaml:profiles.std.mysql_connections",
		},
		{
			name: "subtree",
			files: func(v string) map[string]string {
				return map[string]string{
					"_defaults.yaml":     root,
					"sub/_defaults.yaml": "defaults:\n  mysql_connections: " + v + "\n",
					"sub/tx.yaml":        "tenants:\n  tx: {}\n",
				}
			},
			below: 30, layer: config.KeyLayerDefaults, file: "_defaults.yaml",
			finding: "sub/_defaults.yaml:defaults.mysql_connections",
		},
	}
}

// walkerValue is tx's mysql_connections on the walker (`da-guard
// effective`): the value and the layer/file key_sources names for it.
func walkerValue(t *testing.T, files map[string]string) (any, config.KeySource) {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "conf.d")
	tree := make(map[string]string, len(files))
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, filepath.Dir(dir), tree)
	code, doc, stderr := runEffectiveOn(t, dir)
	if code != exitOK {
		t.Fatalf("effective: exit %d; stderr=%q", code, stderr)
	}
	var tx struct {
		EffectiveConfig map[string]any              `json:"effective_config"`
		KeySources      map[string]config.KeySource `json:"key_sources"`
	}
	if err := json.Unmarshal(doc.Tenants["tx"], &tx); err != nil {
		t.Fatalf("effective tx: %v", err)
	}
	return tx.EffectiveConfig["mysql_connections"], tx.KeySources["mysql_connections"]
}

func scheduleNullFindings(fs []guard.Finding) []guard.Finding {
	var out []guard.Finding
	for _, f := range fs {
		if f.Kind == guard.FindingScheduleNullValue {
			out = append(out, f)
		}
	}
	return out
}

// `{default: null}` and `{default: null, overrides: []}` are plain null on
// every layer #2518 reads a null on: /metrics serves the layer below, the
// walker shows the layer below's value attributed to that layer, and the
// guard reports nothing (the same as for plain null — measured too).
func TestGuard_ScheduleDefaultNullIsPlainNull(t *testing.T) {
	t.Parallel()
	for _, layer := range scheduleNullLayers() {
		for _, v := range []string{"null", "{default: null}", "{default: null, overrides: []}", "{default: ~, overrides: ~}"} {
			t.Run(layer.name+"/"+v, func(t *testing.T) {
				t.Parallel()
				files := layer.files(v)
				code, doc, _, stderr := served(t, files, scheduleNullAt)
				mustOK(t, code, stderr)
				wantValue(t, doc, "tx", "mysql_connections", layer.below)

				got, src := walkerValue(t, files)
				if fmt.Sprint(got) != fmt.Sprint(layer.below) || src.Layer != layer.layer || src.File != layer.file {
					t.Errorf("walker: mysql_connections = %v from %s %s, want %v from %s %s",
						got, src.Layer, src.File, layer.below, layer.layer, layer.file)
				}

				code, fs := guardFindingsOf(t, files)
				if code != exitOK || len(fs) != 0 {
					t.Errorf("guard: exit %d, findings %+v; want 0 and none", code, fs)
				}
			})
		}
	}
}

// A null inside a schedule WITH windows is refused on every layer: exit 1
// and exactly one schedule_null_value error naming the file, the place in
// it, the tenant (for a `tenants:` entry) and where the null is.
func TestGuard_ScheduleNullWithWindowsIsRefused(t *testing.T) {
	t.Parallel()
	shapes := map[string]struct{ v, problem string }{
		"window value null": {
			`{default: 50, overrides: [{window: "00:00-23:59", value: null}]}`,
			"`overrides[0]` (window \"00:00-23:59\") has `value: null`",
		},
		"default null beside a window": {
			`{default: null, overrides: [{window: "00:00-01:00", value: 60}]}`,
			"`default:` is null beside 1 override window(s)",
		},
	}
	for _, layer := range scheduleNullLayers() {
		for name, shape := range shapes {
			t.Run(layer.name+"/"+name, func(t *testing.T) {
				t.Parallel()
				code, fs := guardFindingsOf(t, layer.files(shape.v))
				if code != exitFindings {
					t.Errorf("exit = %d, want %d", code, exitFindings)
				}
				got := scheduleNullFindings(fs)
				if len(got) != 1 {
					t.Fatalf("schedule_null_value findings = %+v (all: %+v), want exactly one", got, fs)
				}
				f := got[0]
				if f.Severity != guard.SeverityError || f.Field != layer.finding || f.TenantID != layer.tenantID {
					t.Errorf("finding = %+v, want error / Field %q / tenant %q", f, layer.finding, layer.tenantID)
				}
				for _, want := range []string{shape.problem, strings.SplitN(layer.finding, ":", 2)[0] + ":", "#2708"} {
					if !strings.Contains(f.Message, want) {
						t.Errorf("message lacks %q: %s", want, f.Message)
					}
				}
			})
		}
	}
}

// Controls: a numeric schedule and a schedule with a window but no null are
// not refused and serve as before; a tenant entry of a platform file for a
// tenant outside --scope is not reported.
func TestGuard_ScheduleNullControls(t *testing.T) {
	t.Parallel()
	layer := scheduleNullLayers()[0] // tenant in sub/
	numeric := layer.files(`{default: 50, overrides: [{window: "00:00-23:59", value: 70}]}`)
	if code, fs := guardFindingsOf(t, numeric); code != exitOK || len(fs) != 0 {
		t.Errorf("numeric schedule: exit %d, findings %+v; want 0 and none", code, fs)
	}
	code, doc, _, stderr := served(t, numeric, scheduleNullAt)
	mustOK(t, code, stderr)
	wantValue(t, doc, "tx", "mysql_connections", 70)

	outside := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 30\n",
		"_platform.yaml": "tenants:\n  ty:\n    mysql_connections: {default: null, overrides: [{window: \"00:00-01:00\", value: 60}]}\n",
		"a/tx.yaml":      "tenants:\n  tx: {}\n",
		"b/ty.yaml":      "tenants:\n  ty: {}\n",
	}
	if code, fs := guardFindingsOf(t, outside, "--scope", "a"); code != exitOK || len(scheduleNullFindings(fs)) != 0 {
		t.Errorf("--scope a: exit %d, findings %+v; want 0 and no schedule_null_value", code, fs)
	}
	if code, fs := guardFindingsOf(t, outside); code != exitFindings || len(scheduleNullFindings(fs)) != 1 {
		t.Errorf("whole tree: exit %d, findings %+v; want 1 and one schedule_null_value", code, fs)
	}
}
