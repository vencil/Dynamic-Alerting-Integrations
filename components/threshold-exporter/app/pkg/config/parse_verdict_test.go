package config

// parse_verdict_test.go — ADR-036 step 6 (#3206): every decode entry asks
// parse-tree first (parse_verdict.go), so a conf.d file parsetree.Parse
// refuses is refused by the exporter's load too, with the same kind.
//
// The matrix is the nine shapes ADR-036 measured on main (2026-10-20): seven
// the exporter read before this step and two both sides already refused, each
// in a tenant file and in the root `_defaults.yaml`, beside a control row.
// The metric half (da_config_parse_refused_total through the configMetrics
// seam) is package main's TestParseRefusedMatrix_MetricAndLog; the da-guard
// exit codes are cmd/da-guard's TestParseVerdictMatrix_ExitThree.

import (
	"errors"
	"fmt"
	"io"
	"log"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// parseVerdictShape is one row: extra is appended to an otherwise valid file.
type parseVerdictShape struct {
	name  string
	extra string
	kind  parsetree.Kind // "" = the control: parse-tree reads it
	// before: the exporter read the file before step 6 (the seven shapes);
	// false for the two it already refused, and for the control.
	before bool
}

// parseVerdictShapes is the matrix. Package main and cmd/da-guard keep their
// own copy of the strings: a test package cannot import another's test file.
var parseVerdictShapes = []parseVerdictShape{
	{"multiple documents", "---\nx: 1\n", parsetree.KindMultipleDocuments, true},
	{"!!binary", "x: !!binary aGVsbG8=\n", parsetree.KindTagNotAllowed, true},
	{"!!set", "x: !!set {a: null}\n", parsetree.KindTagNotAllowed, true},
	{"BOM mid-file", "# note \uFEFF here\n", parsetree.KindBOMNotAtStart, true},
	{"null key", "~: 1\n", parsetree.KindNullKey, true},
	{"non-specific tag", "x: ! \"y\"\n", parsetree.KindNonSpecificTag, true},
	{"trailing --- only", "---\n", parsetree.KindMultipleDocuments, true},
	{"duplicate key", "x: 1\nx: 2\n", parsetree.KindDuplicateKey, false},
	{"non-scalar key", "? [a]\n: 1\n", parsetree.KindNonScalarKey, false},
	{"control", "", "", false},
}

const (
	pvTenant1  = "tenants:\n  t1:\n    mysql_connections: \"80\"\n"
	pvTenant2  = "tenants:\n  t2:\n    mysql_connections: \"90\"\n"
	pvDefaults = "defaults:\n  mysql_connections: 70\n  mysql_slow_queries: 50\n"
)

// parseVerdictTree is the row's tree with the shape in the tenant file t1.yaml
// (inDefaults false) or in the root `_defaults.yaml`.
func parseVerdictTree(t *testing.T, s parseVerdictShape, inDefaults bool) string {
	t.Helper()
	files := map[string]string{"_defaults.yaml": pvDefaults, "t1.yaml": pvTenant1, "t2.yaml": pvTenant2}
	if inDefaults {
		files["_defaults.yaml"] += s.extra
	} else {
		files["t1.yaml"] += s.extra
	}
	return writePlatformTree(t, files)
}

func TestParseVerdictMatrix_ParseTreeRowsAreWhatTheMatrixSays(t *testing.T) {
	t.Parallel()
	// The matrix is parse-tree's own answer, asserted rather than trusted.
	for _, s := range parseVerdictShapes {
		for _, base := range []string{pvTenant1, pvDefaults} {
			_, bad := parsetree.Parse([]byte(base + s.extra))
			got := parsetree.Kind("")
			if bad != nil {
				got = bad.Kind
			}
			if got != s.kind {
				t.Errorf("%s: parse-tree kind %q, the matrix says %q", s.name, got, s.kind)
			}
			// before: the decode alone (no verdict) read it.
			_, derr := parseConfigFile([]byte(base+s.extra), true)
			if s.kind != "" && (derr == nil) != s.before {
				t.Errorf("%s: the decode alone err = %v; the matrix says the exporter read it before step 6: %v", s.name, derr, s.before)
			}
		}
	}
}

// The exporter's load (LoadDir: the flat plane /metrics serves, its
// parseFailed is da-guard's exit 3) and the walker plane (ResolveEffective:
// tenant-api /effective, da-guard effective) over each row.
func TestParseVerdictMatrix_TheExporterServesWhatParseTreeReads(t *testing.T) {
	t.Parallel()
	for _, s := range parseVerdictShapes {
		for _, inDefaults := range []bool{false, true} {
			s, inDefaults := s, inDefaults
			where := map[bool]string{false: "tenant file", true: "_defaults.yaml"}[inDefaults]
			t.Run(s.name+"/"+where, func(t *testing.T) {
				t.Parallel()
				dir := parseVerdictTree(t, s, inDefaults)
				cfg, parseFailed, err := LoadDir(dir, log.New(io.Discard, "", 0))
				if err != nil {
					t.Fatalf("LoadDir: %v", err)
				}
				tenants := make([]string, 0, len(cfg.Tenants))
				for id := range cfg.Tenants {
					tenants = append(tenants, id)
				}
				sort.Strings(tenants)
				_, cpu := cfg.Defaults["mysql_slow_queries"]

				wantTenants, wantFailed, wantCPU := []string{"t1", "t2"}, []string(nil), true
				switch {
				case s.kind == "":
				case inDefaults:
					// The carrier dropped whole: every tenant stays, without
					// the defaults only it supplied.
					wantFailed, wantCPU = []string{"_defaults.yaml"}, false
				default:
					// The tenant file's tenant is not served; the other is.
					wantTenants, wantFailed = []string{"t2"}, []string{"t1.yaml"}
				}
				if !reflect.DeepEqual(tenants, wantTenants) || !reflect.DeepEqual(parseFailed, wantFailed) || cpu != wantCPU {
					t.Errorf("LoadDir: tenants %v, parseFailed %v, mysql_slow_queries default %v; want %v, %v, %v",
						tenants, parseFailed, cpu, wantTenants, wantFailed, wantCPU)
				}

				ec, rerr := ResolveEffective(dir, "t1")
				switch {
				case s.kind != "" && !inDefaults:
					if !errors.Is(rerr, ErrTenantNotFound) {
						t.Errorf("ResolveEffective(t1) err = %v, want ErrTenantNotFound", rerr)
					}
				case rerr != nil:
					t.Errorf("ResolveEffective(t1): %v", rerr)
				case s.kind != "" && !reflect.DeepEqual(ec.ChainParseFailed, []string{"_defaults.yaml"}):
					t.Errorf("ResolveEffective(t1) chain_parse_failed = %v, want [_defaults.yaml]", ec.ChainParseFailed)
				case s.kind == "" && len(ec.ChainParseFailed) != 0:
					t.Errorf("control: chain_parse_failed = %v, want none", ec.ChainParseFailed)
				}
			})
		}
	}
}

// The verdict reaches the walker's observer with its kind (the exporter's
// da_config_parse_refused_total is package main's half), and the WARN line
// names it.
func TestParseVerdictMatrix_TheWalkerCountsTheKind(t *testing.T) {
	t.Parallel()
	for _, s := range parseVerdictShapes {
		s := s
		t.Run(s.name, func(t *testing.T) {
			t.Parallel()
			dir := parseVerdictTree(t, s, false)
			obs := &recordingObserver{}
			var logs strings.Builder
			if _, err := ScanDirTree(dir, nil, obs, log.New(&logs, "", 0)); err != nil {
				t.Fatalf("ScanDirTree: %v", err)
			}
			var wantFailures, wantRefused []string
			if s.kind != "" {
				wantFailures, wantRefused = []string{"t1.yaml"}, []string{"t1.yaml " + string(s.kind)}
				if !strings.Contains(logs.String(), "(kind "+string(s.kind)+")") {
					t.Errorf("the skip WARN does not name the kind:\n%s", logs.String())
				}
			}
			if !reflect.DeepEqual(obs.parseFailures, wantFailures) || !reflect.DeepEqual(obs.refused, wantRefused) {
				t.Errorf("observer: parse failures %v, refused %v; want %v, %v", obs.parseFailures, obs.refused, wantFailures, wantRefused)
			}
		})
	}
}

// Every decode entry gives the same verdict on the same bytes, with the kind
// reachable through the error chain the callers wrap it in.
func TestParseVerdict_EveryDecodeEntryRefusesTheSameFiles(t *testing.T) {
	t.Parallel()
	for _, s := range parseVerdictShapes {
		data := []byte(pvTenant1 + s.extra)
		_, e1 := ParseConfigFile(data)
		_, e2 := ParseTenantFile(data)
		e3 := ParseChainDefaults(data).err
		_, e4 := ParseTenantDoc(data).TenantRaw("t1")
		_, ok5 := reportUnparseableNestedPlatformFile("/x/team/_notes.yaml", data, nil, log.New(io.Discard, "", 0))
		for i, err := range []error{e1, e2, e3, e4} {
			kind, ok := UnreadableKindOf(err)
			if s.kind == "" {
				if err != nil {
					t.Errorf("%s: entry %d refuses the control: %v", s.name, i+1, err)
				}
				continue
			}
			if !ok || kind != s.kind {
				t.Errorf("%s: entry %d err = %v, want parse-tree's %s", s.name, i+1, err, s.kind)
			}
		}
		if ok5 != (s.kind == "") {
			t.Errorf("%s: the nested probe ok = %v", s.name, ok5)
		}
	}
}

// Since step 6 a resolve error has no file content left to come from: a
// file the merge would refuse is one the build drops first, so the chain
// reads it as empty instead (effective_scan_test.go's coverage list).
func TestResolveEffective_NoFileTheBuildReadsFailsTheMerge(t *testing.T) {
	t.Parallel()
	extras := map[string]string{
		"repeated key under an unknown field (#2179)": "extra:\n  k: 1\n  k: 2\n",
		"explicit tag its text does not satisfy":      "extra: !!int abc\n",
		"self-referencing alias":                      "extra: &a {b: *a}\n",
		"merge of a scalar":                           "extra:\n  <<: 5\n",
	}
	for _, s := range parseVerdictShapes {
		if s.kind != "" {
			extras[s.name] = s.extra
		}
	}
	for name, extra := range extras {
		dir := writePlatformTree(t, map[string]string{"_defaults.yaml": pvDefaults + extra, "t1.yaml": pvTenant1})
		ec, err := ResolveEffective(dir, "t1")
		var de *DecodeError
		if errors.As(err, &de) || err != nil {
			t.Errorf("%s: ResolveEffective err = %v, want the chain to read the file as empty", name, err)
			continue
		}
		if !reflect.DeepEqual(ec.ChainParseFailed, []string{"_defaults.yaml"}) {
			t.Errorf("%s: chain_parse_failed = %v, want [_defaults.yaml]", name, ec.ChainParseFailed)
		}
	}
}

// #2266's rejection names every non-UTF-8 tenant id, sorted, so the message
// is the same on every run (map order is random). Since step 6 no file
// parse-tree reads can spell one (`!!binary` is tag_not_allowed, raw bytes
// invalid_utf8), so it is pinned on the decode alone.
func TestParseTenantFileDecode_NonUTF8IDsNamedInAStableOrder(t *testing.T) {
	t.Parallel()
	// !!binary: "c\xff", "a\xff", "b\xff"
	data := []byte("tenants:\n  !!binary Y/8=:\n    mysql_connections: \"1\"\n  !!binary Yf8=:\n    mysql_connections: \"2\"\n" +
		"  !!binary Yv8=:\n    mysql_connections: \"3\"\n")
	const want = `tenant ids ["a\xff" "b\xff" "c\xff"] are not valid UTF-8`
	for i := 0; i < 20; i++ {
		if _, err := parseTenantFileDecode(data); err == nil || err.Error() != want {
			t.Fatalf("run %d: err = %v, want %s", i, err, want)
		}
	}
	if _, err := ParseTenantFile(data); fmt.Sprint(UnreadableKindOf(err)) != fmt.Sprint(parsetree.KindTagNotAllowed, true) {
		t.Errorf("ParseTenantFile err = %v, want parse-tree's tag_not_allowed", err)
	}
}
