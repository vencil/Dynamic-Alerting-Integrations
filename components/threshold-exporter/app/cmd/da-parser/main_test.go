package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/parser"
)

// fixtureRoot is the absolute path to the parser package's testdata
// directory. We reuse fixtures from the library tests rather than
// duplicating YAML — that's also why the CLI's `import` subcommand
// is happy to take any path including ones outside cmd/.
func fixtureRoot(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	// cmd/da-parser → ../../internal/parser/testdata
	return filepath.Clean(filepath.Join(wd, "..", "..", "internal", "parser", "testdata"))
}

// runWith captures stdout / stderr from run() with the given args.
func runWith(args []string) (int, string, string) {
	var out, errOut bytes.Buffer
	code := run(args, &out, &errOut)
	return code, out.String(), errOut.String()
}

func TestRun_NoArgsPrintsRootUsage(t *testing.T) {
	code, _, errOut := runWith(nil)
	if code != exitOK {
		t.Errorf("code = %d, want %d", code, exitOK)
	}
	if !strings.Contains(errOut, "Subcommands:") {
		t.Errorf("usage missing 'Subcommands:': %q", errOut)
	}
	if !strings.Contains(errOut, "import") || !strings.Contains(errOut, "allowlist") {
		t.Errorf("usage missing subcommand names: %q", errOut)
	}
}

func TestRun_HelpFlagsExitClean(t *testing.T) {
	for _, arg := range []string{"-h", "--help", "help"} {
		t.Run(arg, func(t *testing.T) {
			code, _, _ := runWith([]string{arg})
			if code != exitOK {
				t.Errorf("code = %d, want %d", code, exitOK)
			}
		})
	}
}

func TestRun_VersionFlagsPrintBinaryName(t *testing.T) {
	for _, arg := range []string{"-V", "--version", "version"} {
		t.Run(arg, func(t *testing.T) {
			code, out, _ := runWith([]string{arg})
			if code != exitOK {
				t.Errorf("code = %d, want %d", code, exitOK)
			}
			if !strings.Contains(out, programName) {
				t.Errorf("output missing %q: %q", programName, out)
			}
		})
	}
}

func TestRun_UnknownSubcommandReturnsCallerErr(t *testing.T) {
	code, _, errOut := runWith([]string{"banana"})
	if code != exitCallerErr {
		t.Errorf("code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(errOut, "unknown subcommand") {
		t.Errorf("err missing 'unknown subcommand': %q", errOut)
	}
}

// ─── import ────────────────────────────────────────────────────────

func TestRunImport_BasicProm(t *testing.T) {
	src := filepath.Join(fixtureRoot(t), "promrule_basic.yaml")
	code, out, _ := runWith([]string{"import", "--input", src})
	if code != exitOK {
		t.Fatalf("code = %d, want %d", code, exitOK)
	}
	var res parser.ParseResult
	if err := json.Unmarshal([]byte(out), &res); err != nil {
		t.Fatalf("decode JSON: %v\noutput=%q", err, out)
	}
	if len(res.Rules) != 2 {
		t.Errorf("rules = %d, want 2", len(res.Rules))
	}
	for i, r := range res.Rules {
		if !r.PromPortable {
			t.Errorf("rule[%d] PromPortable = false, want true", i)
		}
		if !r.PromCompatible {
			t.Errorf("rule[%d] PromCompatible = false; default --validate-strict-prom should be on", i)
		}
	}
}

func TestRunImport_StrictPromOffLeavesPromCompatibleZero(t *testing.T) {
	src := filepath.Join(fixtureRoot(t), "promrule_basic.yaml")
	code, out, _ := runWith([]string{"import", "--input", src, "--validate-strict-prom=false"})
	if code != exitOK {
		t.Fatalf("code = %d, want %d", code, exitOK)
	}
	var res parser.ParseResult
	if err := json.Unmarshal([]byte(out), &res); err != nil {
		t.Fatalf("decode JSON: %v", err)
	}
	for i, r := range res.Rules {
		if r.PromCompatible {
			t.Errorf("rule[%d] PromCompatible = true with strict off; want zero value", i)
		}
	}
}

func TestRunImport_VMOnlyPassesWithoutGate(t *testing.T) {
	src := filepath.Join(fixtureRoot(t), "promrule_metricsql.yaml")
	code, _, _ := runWith([]string{"import", "--input", src})
	if code != exitOK {
		t.Errorf("code = %d, want %d (no gate flag, exit should be clean even on VM-only)", code, exitOK)
	}
}

func TestRunImport_FailOnNonPortableTripsOnVMOnly(t *testing.T) {
	src := filepath.Join(fixtureRoot(t), "promrule_metricsql.yaml")
	code, _, errOut := runWith([]string{"import", "--input", src, "--fail-on-non-portable"})
	if code != exitGateFail {
		t.Errorf("code = %d, want %d (gate)", code, exitGateFail)
	}
	if !strings.Contains(errOut, "prom_compatible=false") {
		t.Errorf("err missing gate diagnostic: %q", errOut)
	}
}

func TestRunImport_FailOnNonPortableAutoElevatesStrictProm(t *testing.T) {
	src := filepath.Join(fixtureRoot(t), "promrule_basic.yaml")
	code, _, errOut := runWith([]string{
		"import", "--input", src,
		"--validate-strict-prom=false", "--fail-on-non-portable",
	})
	// basic fixture is pure PromQL; auto-elevation re-enables strict
	// validation, which passes cleanly → exit 0.
	if code != exitOK {
		t.Errorf("code = %d, want %d (auto-elevate then pass)", code, exitOK)
	}
	if !strings.Contains(errOut, "auto-enabling") {
		t.Errorf("err missing auto-enable diagnostic: %q", errOut)
	}
}

func TestRunImport_FailOnAmbiguousTripsOnSyntaxError(t *testing.T) {
	src := filepath.Join(fixtureRoot(t), "promrule_ambiguous.yaml")
	code, _, errOut := runWith([]string{"import", "--input", src, "--fail-on-ambiguous"})
	if code != exitGateFail {
		t.Errorf("code = %d, want %d (gate)", code, exitGateFail)
	}
	if !strings.Contains(errOut, "dialect=ambiguous") {
		t.Errorf("err missing ambiguous gate diagnostic: %q", errOut)
	}
}

func TestRunImport_OutputFileWritesJSONAndDiagnostics(t *testing.T) {
	tmp := t.TempDir()
	outPath := filepath.Join(tmp, "result.json")
	src := filepath.Join(fixtureRoot(t), "promrule_basic.yaml")
	code, stdout, errOut := runWith([]string{"import", "--input", src, "--output", outPath})
	if code != exitOK {
		t.Fatalf("code = %d, want %d (errOut=%q)", code, exitOK, errOut)
	}
	if stdout != "" {
		t.Errorf("stdout = %q; want empty when --output != '-'", stdout)
	}
	if !strings.Contains(errOut, "wrote") {
		t.Errorf("errOut missing 'wrote': %q", errOut)
	}
	body, err := os.ReadFile(outPath)
	if err != nil {
		t.Fatalf("read output file: %v", err)
	}
	var res parser.ParseResult
	if err := json.Unmarshal(body, &res); err != nil {
		t.Fatalf("decode output JSON: %v", err)
	}
	if len(res.Rules) != 2 {
		t.Errorf("rules = %d, want 2", len(res.Rules))
	}
}

func TestRunImport_MissingInputFlagReturnsCallerErr(t *testing.T) {
	code, _, errOut := runWith([]string{"import"})
	if code != exitCallerErr {
		t.Errorf("code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(errOut, "--input is required") {
		t.Errorf("err missing required-flag message: %q", errOut)
	}
}

func TestRunImport_MissingFilePathReturnsCallerErr(t *testing.T) {
	code, _, _ := runWith([]string{"import", "--input", "/this/does/not/exist.yaml"})
	if code != exitCallerErr {
		t.Errorf("code = %d, want %d", code, exitCallerErr)
	}
}

func TestRunImport_GeneratedByOverridesBinaryStamp(t *testing.T) {
	src := filepath.Join(fixtureRoot(t), "promrule_basic.yaml")
	const stamp = "ci-job-99 da-parser@v2.8.0"
	code, out, _ := runWith([]string{"import", "--input", src, "--generated-by", stamp})
	if code != exitOK {
		t.Fatalf("code = %d, want %d", code, exitOK)
	}
	var res parser.ParseResult
	if err := json.Unmarshal([]byte(out), &res); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if res.Provenance.GeneratedBy != stamp {
		t.Errorf("GeneratedBy = %q, want %q", res.Provenance.GeneratedBy, stamp)
	}
}

func TestRunImport_DefaultGeneratedByContainsBinaryName(t *testing.T) {
	src := filepath.Join(fixtureRoot(t), "promrule_basic.yaml")
	code, out, _ := runWith([]string{"import", "--input", src})
	if code != exitOK {
		t.Fatalf("code = %d", code)
	}
	var res parser.ParseResult
	if err := json.Unmarshal([]byte(out), &res); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !strings.Contains(res.Provenance.GeneratedBy, programName) {
		t.Errorf("default GeneratedBy = %q; want to include %q", res.Provenance.GeneratedBy, programName)
	}
}

// ─── allowlist ─────────────────────────────────────────────────────

func TestRunAllowlist_DefaultTextFormat(t *testing.T) {
	code, out, _ := runWith([]string{"allowlist"})
	if code != exitOK {
		t.Errorf("code = %d", code)
	}
	// Spot-check a few core entries — text format is one name per line.
	for _, want := range []string{"rollup_rate", "interpolate", "label_set"} {
		if !strings.Contains(out, want+"\n") {
			t.Errorf("expected %q line in allowlist text output", want)
		}
	}
}

func TestRunAllowlist_JSONFormatIncludesVersion(t *testing.T) {
	code, out, _ := runWith([]string{"allowlist", "--format", "json"})
	if code != exitOK {
		t.Errorf("code = %d", code)
	}
	var body struct {
		MetricsqlVersion string   `json:"metricsql_version"`
		Functions        []string `json:"functions"`
	}
	if err := json.Unmarshal([]byte(out), &body); err != nil {
		t.Fatalf("decode JSON: %v\nout=%q", err, out)
	}
	if body.MetricsqlVersion == "" {
		t.Error("metricsql_version is empty")
	}
	if len(body.Functions) < 50 {
		t.Errorf("functions = %d entries; expected ≥ 50", len(body.Functions))
	}
	// Determinism: sort order is documented (parser.VMOnlyFunctionNames sorts).
	for i := 1; i < len(body.Functions); i++ {
		if body.Functions[i-1] > body.Functions[i] {
			t.Errorf("functions not sorted at %d: %q > %q", i, body.Functions[i-1], body.Functions[i])
			break
		}
	}
}

func TestRunAllowlist_BadFormatReturnsCallerErr(t *testing.T) {
	code, _, errOut := runWith([]string{"allowlist", "--format", "xml"})
	if code != exitCallerErr {
		t.Errorf("code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(errOut, "must be 'text' or 'json'") {
		t.Errorf("err missing format diagnostic: %q", errOut)
	}
}

// ─── stdin path (separate because it touches os.Stdin) ─────────────

func TestReadInputBytes_StdinPath(t *testing.T) {
	// We can't drive os.Stdin from runWith easily, so test
	// readInputBytes directly with a temp pipe.
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	const sample = "groups: []\n"
	if _, err := w.WriteString(sample); err != nil {
		t.Fatalf("write: %v", err)
	}
	w.Close()
	saved := os.Stdin
	os.Stdin = r
	defer func() { os.Stdin = saved }()

	bytes, label, err := readInputBytes("-")
	if err != nil {
		t.Fatalf("readInputBytes(-) = err %v", err)
	}
	if string(bytes) != sample {
		t.Errorf("bytes = %q, want %q", string(bytes), sample)
	}
	if label != "<stdin>" {
		t.Errorf("label = %q, want '<stdin>'", label)
	}
}
