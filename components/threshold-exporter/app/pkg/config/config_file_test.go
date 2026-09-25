package config

// config_file_test.go — the differential pin for ParseConfigFile, the ONE
// decode of a conf.d file (#1957).
//
// Two claims, over one variant corpus:
//
//  1. ParseConfigFile is byte-for-byte a plain yaml.Unmarshal into
//     ThresholdConfig — the flat plane's historical decode — for accept/reject
//     AND for the decoded content. The oracle is written out here rather than
//     calling ParseConfigFile twice, so the SSOT cannot drift from it quietly.
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

// oracleDecode is the flat plane's historical decode, spelled out.
func oracleDecode(data []byte) (ThresholdConfig, error) {
	var cfg ThresholdConfig
	err := yaml.Unmarshal(data, &cfg)
	return cfg, err
}

func TestParseConfigFile_IsThePlainDecode(t *testing.T) {
	t.Parallel()
	for name, body := range configFileCorpus {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			want, wantErr := oracleDecode([]byte(body))
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
			want, wantErr := oracleDecode([]byte(body))

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
		_, fullErr := oracleDecode([]byte(body))
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
