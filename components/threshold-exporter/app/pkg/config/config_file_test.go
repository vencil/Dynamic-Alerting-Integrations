package config

// config_file_test.go — the differential pin for ParseConfigFile, the ONE
// decode of a conf.d file (#1957).
//
// Two claims, over one variant corpus:
//
//  1. ParseConfigFile is a plain yaml.Unmarshal into ThresholdConfig — the
//     flat plane's historical decode — for accept/reject AND for the decoded
//     content, with ONE listed exception (#2418): a `defaults:` spelling that
//     the file does not write (null, ±Inf, NaN) beside another spelling of
//     the same threshold that it does write is dropped. The oracle is written
//     out here rather than calling ParseConfigFile twice, so the SSOT cannot
//     drift from it quietly; the exception is the explicit per-row key list
//     nullShadowDrops, not a call into the code under test.
//  2. The walker (ScanDirTree) judges each file by that decode: ParseFailed
//     iff the oracle errors, TenantIDs == the sorted keys of the oracle's
//     Tenants, and TreeScan.Partials holds exactly the oracle's value.
//
// ⛔ Claim 2 is the one with detection power. Putting the walker back on its
// pre-#1957 decode (`tenants:` into map[string]yaml.Node) reddens every row in
// lighterDecodeWouldAccept below — and that set is itself pinned, so the corpus
// cannot quietly lose the rows that tell the two decodes apart.

import (
	"errors"
	"io"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// configFileCorpus is one file's bytes per row. Tenant "tx" is the subject
// wherever a row declares one.
var configFileCorpus = map[string]string{
	"plain tenant":                        "tenants:\n  tx:\n    cpu: \"80\"\n",
	"tenant body is a scalar":             "tenants:\n  tx: \"80\"\n",
	"tenant body is a list":               "tenants:\n  tx:\n    - a\n    - b\n",
	"tenant body is null":                 "tenants:\n  tx:\n",
	"tenants is a scalar":                 "tenants: nope\n",
	"tenants is a list":                   "tenants:\n  - tx\n",
	"tenants is null":                     "tenants:\n",
	"defaults value is a string":          "defaults:\n  cpu: \"high\"\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"defaults value is a quoted number":   "defaults:\n  cpu: \"80\"\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"defaults value is a mapping (M5)":    "defaults:\n  cpu:\n    nested: map\ntenants:\n  tx:\n    cpu: \"66\"\n",
	"defaults value in schedule form":     "defaults:\n  cpu:\n    default: \"90\"\ntenants:\n  tx: {}\n",
	"max_metrics_per_tenant non-int":      "max_metrics_per_tenant: lots\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"max_metrics_per_tenant float":        "max_metrics_per_tenant: 1.5\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"state_filters is a scalar":           "state_filters: nope\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"state_filters is a list":             "state_filters:\n  - a\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"profiles is a scalar":                "profiles: nope\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"profiles is a list":                  "profiles:\n  - gold\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"optional_overrides is a mapping":     "optional_overrides:\n  a: b\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"optional_overrides is a scalar":      "optional_overrides: a\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"scheduled value":                     "tenants:\n  tx:\n    cpu:\n      default: \"80\"\n      overrides:\n        - window: \"22:00-06:00\"\n          value: \"95\"\n",
	"scheduled value, overrides a scalar": "tenants:\n  tx:\n    cpu:\n      default: \"80\"\n      overrides: nope\n",
	"scheduled value, default a list":     "tenants:\n  tx:\n    cpu:\n      default: [1, 2]\n",
	"arbitrary mapping value":             "tenants:\n  tx:\n    _routing:\n      receiver: x\n      group_wait: 30s\n",
	"list value":                          "tenants:\n  tx:\n    _custom_alerts:\n      - recipe: r\n",
	"empty file":                          "",
	"comments only":                       "# nothing here\n# tenants:\n",
	"multi-document":                      "tenants:\n  tx:\n    cpu: \"80\"\n---\ntenants:\n  ty:\n    cpu: \"90\"\n",
	"unknown top-level keys":              "whatever: 1\ntenants:\n  tx:\n    cpu: \"80\"\n",
	"syntax error":                        "tenants:\n  tx:\n    cpu: [80\n",
	"top level is a list":                 "- a\n- b\n",
	"alias":                               "base: &b\n  cpu: \"80\"\ntenants:\n  tx: *b\n",
	"merge key":                           "base: &b\n  cpu: \"80\"\ntenants:\n  tx:\n    <<: *b\n    mem: \"70\"\n",
	"duplicate tenant key":                "tenants:\n  tx:\n    cpu: \"80\"\n  tx:\n    cpu: \"90\"\n",
	"duplicate metric key":                "tenants:\n  tx:\n    cpu: \"80\"\n    cpu: \"90\"\n",
	"integer tenant key":                  "tenants:\n  123:\n    cpu: \"80\"\n",
	// #2418: the one exception to "plain decode" (nullShadowDrops).
	"defaults canonical null beside legacy value": "defaults:\n  mysql_threads_running: null\n  " + legacyThreadsRunning + ": 30\ntenants:\n  tx: {}\n",
	"defaults canonical .inf beside legacy value": "defaults:\n  mysql_threads_running: .inf\n  " + legacyThreadsRunning + ": 30\ntenants:\n  tx: {}\n",
	// Not the exception: neither spelling is written, so both decode as-is.
	"defaults both spellings null": "defaults:\n  mysql_threads_running: null\n  " + legacyThreadsRunning + ": null\ntenants:\n  tx: {}\n",
}

// legacyThreadsRunning is the retired spelling of mysql_threads_running.
const legacyThreadsRunning = "mysql_cpu"

// nullShadowDrops is the ONE place the oracle departs from yaml.Unmarshal
// (#2418): per corpus row, the `defaults:` keys ParseConfigFile drops.
var nullShadowDrops = map[string][]string{
	"defaults canonical null beside legacy value": {"mysql_threads_running"},
	"defaults canonical .inf beside legacy value": {"mysql_threads_running"},
}

// lighterDecodeWouldAccept is the set of rows the walker's PRE-#1957 decode
// (`tenants:` into map[string]yaml.Node) accepted while the full decode
// rejects them — i.e. every row where #1957 changed which tenants /effective,
// da-guard and tenant-api see. Pinned so a corpus edit cannot drop the rows
// that give claim 2 its detection power.
var lighterDecodeWouldAccept = []string{
	"defaults value in schedule form",
	"defaults value is a mapping (M5)",
	"defaults value is a quoted number",
	"defaults value is a string",
	"duplicate metric key",
	"max_metrics_per_tenant non-int",
	"optional_overrides is a mapping",
	"optional_overrides is a scalar",
	"profiles is a list",
	"profiles is a scalar",
	"scheduled value, default a list",
	"scheduled value, overrides a scalar",
	"state_filters is a list",
	"state_filters is a scalar",
	"tenant body is a list",
	"tenant body is a scalar",
}

// oracleDecode is the flat plane's historical decode, spelled out, plus the
// listed #2418 drops for corpus row name.
func oracleDecode(name string, data []byte) (ThresholdConfig, error) {
	var cfg ThresholdConfig
	err := yaml.Unmarshal(data, &cfg)
	if err == nil {
		for _, k := range nullShadowDrops[name] {
			delete(cfg.Defaults, k)
		}
	}
	return cfg, err
}

// TestConfigFileCorpus_ExceptionRowsDifferFromThePlainDecode keeps the
// exception honest: every nullShadowDrops row names a key the plain decode
// DOES produce, so the list cannot hide a row where nothing is dropped.
func TestConfigFileCorpus_ExceptionRowsDifferFromThePlainDecode(t *testing.T) {
	t.Parallel()
	for name, keys := range nullShadowDrops {
		body, ok := configFileCorpus[name]
		if !ok {
			t.Fatalf("nullShadowDrops row %q is not in the corpus", name)
		}
		var plain ThresholdConfig
		if err := yaml.Unmarshal([]byte(body), &plain); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		for _, k := range keys {
			if _, in := plain.Defaults[k]; !in {
				t.Errorf("%s: the plain decode has no %q to drop", name, k)
			}
		}
	}
}

func TestParseConfigFile_IsThePlainDecode(t *testing.T) {
	t.Parallel()
	for name, body := range configFileCorpus {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			want, wantErr := oracleDecode(name, []byte(body))
			got, gotErr := ParseConfigFile([]byte(body))
			if (gotErr == nil) != (wantErr == nil) {
				t.Fatalf("accept/reject differs: ParseConfigFile err=%v, yaml.Unmarshal err=%v", gotErr, wantErr)
			}
			if gotErr != nil {
				if gotErr.Error() != wantErr.Error() {
					t.Errorf("error text differs:\n got  %v\n want %v", gotErr, wantErr)
				}
				return
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("decoded content differs:\n got  %#v\n want %#v", got, want)
			}
		})
	}
}

// TestParseTenantFile_OnlyAddsTheUTF8Rejection pins the #2266 split: over the
// corpus (all UTF-8 ids) the tenant-file decode IS the one decode, and a
// non-UTF-8 tenant id is rejected by it — with nothing decoded, so a caller
// that drops the error still sees no tenant — while ParseConfigFile, which a
// `_` platform file is decoded with, still accepts those bytes.
func TestParseTenantFile_OnlyAddsTheUTF8Rejection(t *testing.T) {
	t.Parallel()
	for name, body := range configFileCorpus {
		want, wantErr := ParseConfigFile([]byte(body))
		got, gotErr := ParseTenantFile([]byte(body))
		if (gotErr == nil) != (wantErr == nil) || !reflect.DeepEqual(got, want) {
			t.Errorf("%s: ParseTenantFile = (%#v, %v), ParseConfigFile = (%#v, %v)", name, got, gotErr, want, wantErr)
		}
	}
	data := []byte("tenants:\n  !!binary dP8=:\n    cpu: \"80\"\n  tx:\n    cpu: \"80\"\n")
	if cfg, err := ParseConfigFile(data); err != nil || len(cfg.Tenants) != 2 {
		t.Errorf("ParseConfigFile = (%d tenants, %v), want both entries accepted", len(cfg.Tenants), err)
	}
	cfg, err := ParseTenantFile(data)
	if err == nil || err.Error() != `tenant id "t\xff" is not valid UTF-8` {
		t.Errorf("ParseTenantFile error = %v, want the non-UTF-8 tenant id rejection", err)
	}
	if !reflect.DeepEqual(cfg, ThresholdConfig{}) {
		t.Errorf("a rejected tenant file decoded to %#v, want nothing", cfg)
	}
}

func TestScanDirTree_JudgesEachFileByTheOneDecode(t *testing.T) {
	t.Parallel()
	for name, body := range configFileCorpus {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			if err := os.WriteFile(filepath.Join(root, "x.yaml"), []byte(body), 0o600); err != nil {
				t.Fatal(err)
			}
			obs := &recordingObserver{}
			scan, err := ScanDirTree(root, nil, obs, discardLogger)
			if err != nil {
				t.Fatalf("ScanDirTree: %v", err)
			}
			f := scan.Files["x.yaml"]
			if f == nil {
				t.Fatalf("x.yaml not kept by the walk")
			}
			want, wantErr := oracleDecode(name, []byte(body))

			if f.ParseFailed != (wantErr != nil) {
				t.Fatalf("walker ParseFailed=%v, but the one decode says err=%v", f.ParseFailed, wantErr)
			}
			if wantErr != nil {
				if f.TenantIDs != nil {
					t.Errorf("a rejected file declared tenants %v", f.TenantIDs)
				}
				if _, ok := scan.Partials["x.yaml"]; ok {
					t.Errorf("a rejected file has a decoded partial")
				}
				if _, err := scan.Locate("tx"); !errors.Is(err, ErrTenantNotFound) {
					t.Errorf("Locate(tx) on a rejected file = %v, want ErrTenantNotFound", err)
				}
				if len(obs.parseFailures) != 1 {
					t.Errorf("parse failures counted = %d, want 1", len(obs.parseFailures))
				}
				return
			}
			var wantIDs []string
			for tid := range want.Tenants {
				wantIDs = append(wantIDs, tid)
			}
			sort.Strings(wantIDs)
			if !reflect.DeepEqual(f.TenantIDs, wantIDs) {
				t.Errorf("walker TenantIDs = %v, want the decoded Tenants' keys %v", f.TenantIDs, wantIDs)
			}
			got, ok := scan.Partials["x.yaml"]
			if !ok {
				t.Fatalf("an accepted file has no decoded partial in TreeScan.Partials")
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("TreeScan.Partials content differs from the one decode:\n got  %#v\n want %#v", got, want)
			}
			if len(obs.parseFailures) != 0 {
				t.Errorf("an accepted file was counted as a parse failure: %v", obs.parseFailures)
			}
		})
	}
}

// TestConfigFileCorpus_KeepsTheRowsThatTellTheDecodesApart pins the corpus'
// own discriminating power (see lighterDecodeWouldAccept).
func TestConfigFileCorpus_KeepsTheRowsThatTellTheDecodesApart(t *testing.T) {
	t.Parallel()
	var got []string
	for name, body := range configFileCorpus {
		var light struct {
			Tenants map[string]yaml.Node `yaml:"tenants"`
		}
		lightOK := yaml.Unmarshal([]byte(body), &light) == nil
		_, fullErr := oracleDecode(name, []byte(body))
		if lightOK && fullErr != nil {
			got = append(got, name)
		}
	}
	sort.Strings(got)
	if !reflect.DeepEqual(got, lighterDecodeWouldAccept) {
		t.Errorf("rows the pre-#1957 decode accepted and the full decode rejects:\n got  %q\n want %q", got, lighterDecodeWouldAccept)
	}
}

// TestTreeScan_PartialsAreLazyAndReleased pins the side map's lifetime: nil
// on a scan that parsed nothing (the quiet tick), and dropped by ReleaseData
// so a retained prior holds no decoded config.
func TestTreeScan_PartialsAreLazyAndReleased(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	p := filepath.Join(root, "a.yaml")
	if err := os.WriteFile(p, []byte("tenants:\n  ta: {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-time.Hour)
	if err := os.Chtimes(p, old, old); err != nil {
		t.Fatal(err)
	}
	cold, err := ScanDirTree(root, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := cold.Partials["a.yaml"]; !ok {
		t.Fatalf("cold scan: no partial for a.yaml")
	}
	cold.ReleaseData()
	if cold.Partials != nil {
		t.Errorf("ReleaseData left Partials allocated (%d entries)", len(cold.Partials))
	}
	warm, err := ScanDirTree(root, cold, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	if warm.Partials != nil {
		t.Errorf("a scan that parsed nothing allocated Partials (%d entries)", len(warm.Partials))
	}
	if !reflect.DeepEqual(warm.Files["a.yaml"].TenantIDs, []string{"ta"}) {
		t.Errorf("warm scan lost the carried declarations: %v", warm.Files["a.yaml"].TenantIDs)
	}
}

// TestParseConfigFile_NullDoesNotShadowTheOtherSpelling pins #2418 at the
// decode: inside one `defaults:` block the canonical-wins dedup is among the
// spellings the file WRITES (levelWritesSpelling). A null beside the other
// spelling's value is dropped so that value is served; every other shape
// decodes exactly as a plain yaml.Unmarshal does.
func TestParseConfigFile_NullDoesNotShadowTheOtherSpelling(t *testing.T) {
	t.Parallel()
	C, L := "mysql_threads_running", "mysql_cpu"
	cases := []struct {
		name, body string
		want       map[string]float64
	}{
		{"canonical null, legacy value", C + ": null\n  " + L + ": 30", map[string]float64{L: 30}},
		{"legacy null, canonical value", L + ": ~\n  " + C + ": 30", map[string]float64{C: 30}},
		{"critical: canonical null, legacy value", C + "_critical: null\n  " + L + "_critical: 30", map[string]float64{L + "_critical": 30}},
		// A non-finite number is no threshold either (levelWritesSpelling).
		{"canonical .inf, legacy value", C + ": .inf\n  " + L + ": 30", map[string]float64{L: 30}},
		{"canonical -.inf, legacy value", C + ": -.inf\n  " + L + ": 30", map[string]float64{L: 30}},
		{"canonical .nan, legacy value", C + ": .nan\n  " + L + ": 30", map[string]float64{L: 30}},
		// Unchanged shapes: plain yaml.Unmarshal.
		{"both null", C + ": null\n  " + L + ": null", map[string]float64{C: 0, L: 0}},
		{"canonical null alone", C + ": null", map[string]float64{C: 0}},
		{"both values", C + ": 40\n  " + L + ": 30", map[string]float64{C: 40, L: 30}},
		{"non-aliased null", "pg_connections: null", map[string]float64{"pg_connections": 0}},
	}
	for _, tc := range cases {
		got, err := ParseConfigFile([]byte("defaults:\n  " + tc.body + "\n"))
		if err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if !reflect.DeepEqual(got.Defaults, tc.want) {
			t.Errorf("%s: Defaults = %v, want %v", tc.name, got.Defaults, tc.want)
		}
	}
}

// TestLoadDir_RootNonThresholdSpellingBesideAValueServesTheValue pins #2418
// on /metrics (LoadDir + Resolve) for every root spelling that writes
// nothing: null, ±Inf and NaN in the canonical spelling beside the retired
// spelling at 30 serve the 30.
func TestLoadDir_RootNonThresholdSpellingBesideAValueServesTheValue(t *testing.T) {
	t.Parallel()
	for _, canonVal := range []string{"null", ".inf", "-.inf", ".nan"} {
		root := t.TempDir()
		files := map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_threads_running: " + canonVal + "\n  " + legacyThreadsRunning + ": 30\n",
			"tx.yaml":        "tenants:\n  tx:\n    redis_x: \"1\"\n",
		}
		for n, body := range files {
			if err := os.WriteFile(filepath.Join(root, n), []byte(body), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		cfg, _, err := LoadDir(root, log.New(io.Discard, "", 0))
		if err != nil {
			t.Fatalf("%s: LoadDir: %v", canonVal, err)
		}
		var got []float64
		for _, r := range cfg.Resolve() {
			if r.Tenant == "tx" && r.Component == "mysql" && r.Metric == "threads_running" && r.Severity == "warning" {
				got = append(got, r.Value)
			}
		}
		if len(got) != 1 || got[0] != 30 {
			t.Errorf("canonical %s beside the retired spelling at 30: served %v, want [30]", canonVal, got)
		}
	}
}
