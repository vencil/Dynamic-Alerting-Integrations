package config

// docs_yaml_inventory_parity_test.go — the Go half of the docs' YAML inventory
// (#3416).
//
// Which ```yaml unit in docs/**/*.md is which file, which consumer reads it and
// whether it is a `_defaults.yaml` is decided in ONE place:
// scripts/tools/_lib_doc_yaml_inventory.py. The drift lint, the unit-sanity
// lint and this package's loader test (docs_defaults_sample_test.go) all read
// that inventory; before, the three had four answers between them, and a block
// one of them judged "not conf.d" was read by nobody.
//
// This file runs the inventory's CLI (python3, with a da-guard: the default
// reader of every undeclared unit is `da-guard parse-tree`) and holds Go's own
// extractor to it: the units Go cuts out of docs/ — path, line, text and fence
// attributes — are exactly the inventory's. A fence the inventory stops seeing
// (an attribute its extractor does not take, a changed fence rule) fails here,
// as does one Go stops seeing.
//
// The inventory runs once per test binary. python3 or da-guard missing fails
// under CI and skips elsewhere (the tenant-api generator tests' rule): CI must
// run this, a laptop without python3 need not.

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
)

// inventoryUnit is one unit of the inventory's JSON (`--json`).
type inventoryUnit struct {
	Path          string   `json:"path"`
	Line          int      `json:"line"`
	Text          string   `json:"text"`
	Declaration   *string  `json:"declaration"`
	Consumer      *string  `json:"consumer"`
	How           *string  `json:"how"`
	Exemption     *string  `json:"exemption"`
	ExpectRefused *string  `json:"expect_refused"`
	ExpectInvalid bool     `json:"expect_invalid"`
	Read          bool     `json:"read"`
	RefusalKind   *string  `json:"refusal_kind"`
	ConfdKind     *string  `json:"confd_kind"`
	Errors        []string `json:"errors"`
}

type inventoryDoc struct {
	Units []inventoryUnit `json:"units"`
}

var (
	inventoryOnce sync.Once
	inventoryRes  *inventoryDoc
	inventoryErr  string
)

// requireOrSkip fails under CI (`$CI` set) and skips elsewhere.
func requireOrSkip(t *testing.T, why string) {
	t.Helper()
	if os.Getenv("CI") != "" {
		t.Fatalf("%s; CI must run this test", why)
	}
	t.Skip(why)
}

// inventoryDaGuard is $DA_GUARD_BINARY when set (relative to the repository
// root: `go test` runs in the package directory), else one CGO_ENABLED=0
// `go build` of ./cmd/da-guard into a temporary directory (#3174: the shipped
// shape).
func inventoryDaGuard(root string) (string, error) {
	if p := strings.TrimSpace(os.Getenv("DA_GUARD_BINARY")); p != "" {
		if !filepath.IsAbs(p) {
			p = filepath.Join(root, p)
		}
		return p, nil
	}
	dir, err := os.MkdirTemp("", "da-guard-inventory")
	if err != nil {
		return "", err
	}
	out := filepath.Join(dir, "da-guard")
	cmd := exec.Command("go", "build", "-buildvcs=false", "-o", out, "./cmd/da-guard")
	cmd.Dir = filepath.Join(root, "components", "threshold-exporter", "app")
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
	if b, err := cmd.CombinedOutput(); err != nil {
		return "", fmt.Errorf("go build da-guard: %v: %s", err, strings.TrimSpace(string(b)))
	}
	return out, nil
}

// docYAMLInventory is the inventory of docs/, from its CLI. Only parse-tree
// is run (`--read confd`): no unit this package reads needs another consumer.
func docYAMLInventory(t *testing.T) *inventoryDoc {
	t.Helper()
	root := findRepoRootForRegistry(t)
	inventoryOnce.Do(func() {
		python, err := exec.LookPath("python3")
		if err != nil {
			inventoryErr = "python3 is not on PATH: the docs' YAML inventory cannot be had"
			return
		}
		daGuard, err := inventoryDaGuard(root)
		if err != nil {
			inventoryErr = err.Error()
			return
		}
		cmd := exec.Command(python, "-X", "utf8",
			filepath.Join(root, "scripts", "tools", "_lib_doc_yaml_inventory.py"),
			"--json", "--read", "confd", "--repo-root", root)
		cmd.Env = append(os.Environ(), "DA_GUARD_BINARY="+daGuard)
		var stderr strings.Builder
		cmd.Stderr = &stderr
		out, err := cmd.Output()
		if err != nil {
			inventoryErr = fmt.Sprintf("the inventory CLI failed (%v): %s", err, strings.TrimSpace(stderr.String()))
			return
		}
		var doc inventoryDoc
		if err := json.Unmarshal(out, &doc); err != nil {
			inventoryErr = fmt.Sprintf("the inventory CLI printed no inventory JSON: %v", err)
			return
		}
		inventoryRes = &doc
	})
	if inventoryRes == nil {
		if strings.HasPrefix(inventoryErr, "python3 is not on PATH") {
			requireOrSkip(t, inventoryErr)
		}
		t.Fatal(inventoryErr)
	}
	return inventoryRes
}

// goDocUnits is Go's own cut of docs/: every unit of every yaml fence, as
// "path:line" → its fence info and text.
func goDocUnits(t *testing.T) map[string][2]string {
	t.Helper()
	root := findRepoRootForRegistry(t)
	out := map[string][2]string{}
	err := filepath.WalkDir(filepath.Join(root, "docs"), func(path string, d os.DirEntry, werr error) error {
		if werr != nil {
			return werr
		}
		if d.IsDir() || !strings.HasSuffix(d.Name(), ".md") {
			return nil
		}
		raw, rerr := os.ReadFile(path) // #nosec G304 -- walking the repo's own docs/
		if rerr != nil {
			return rerr
		}
		rel, rerr := filepath.Rel(root, path)
		if rerr != nil {
			return rerr
		}
		rel = filepath.ToSlash(rel)
		for _, b := range extractYAMLBlocks(rel, string(raw)) {
			for _, s := range splitConcatenatedFiles(b.body) {
				// b.line is the opening fence; body line 0 sits one line below
				// it, so +1 makes offset=0 point at the segment's first line.
				out[fmt.Sprintf("%s:%d", rel, b.line+s.offset+1)] = [2]string{b.info, s.text}
			}
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walk docs/: %v", err)
	}
	return out
}

// TestDocsYAMLInventoryParity: Go's units of docs/ are the inventory's —
// the same set of path:line, and for each the same fence attributes and text.
func TestDocsYAMLInventoryParity(t *testing.T) {
	inv := docYAMLInventory(t)
	goUnits := goDocUnits(t)
	pyUnits := map[string][2]string{}
	declared := 0
	for _, u := range inv.Units {
		info := ""
		if u.Declaration != nil {
			info = *u.Declaration
			declared++
		}
		pyUnits[fmt.Sprintf("%s:%d", u.Path, u.Line)] = [2]string{info, u.Text}
	}
	var onlyGo, onlyPy, differ []string
	for k, g := range goUnits {
		p, ok := pyUnits[k]
		switch {
		case !ok:
			onlyGo = append(onlyGo, k)
		case p != g:
			differ = append(differ, fmt.Sprintf("%s: Go %q / inventory %q", k, g[0], p[0]))
		}
	}
	for k := range pyUnits {
		if _, ok := goUnits[k]; !ok {
			onlyPy = append(onlyPy, k)
		}
	}
	sort.Strings(onlyGo)
	sort.Strings(onlyPy)
	sort.Strings(differ)
	if len(onlyGo)+len(onlyPy)+len(differ) > 0 {
		t.Errorf("Go's extractor and the docs' YAML inventory disagree on the units of docs/:\n"+
			"  only Go: %v\n  only the inventory: %v\n  attributes or text differ: %v",
			onlyGo, onlyPy, differ)
	}
	// A parity of two empty sets, or of sets with no attributed fence in
	// them, proves nothing about the case this test exists for.
	if len(goUnits) < 100 || declared == 0 {
		t.Fatalf("docs/ yielded %d units, %d of them in an attributed fence — the "+
			"extractors are broken, or the docs moved; a parity of nothing must fail loudly",
			len(goUnits), declared)
	}
}

// TestExtractYAMLBlocksTakesFenceAttributes: a fence with attributes is a
// yaml fence (its info kept), a ```yamlx one is not.
func TestExtractYAMLBlocksTakesFenceAttributes(t *testing.T) {
	t.Parallel()
	md := "```yaml {.confd}\na: 1\n```\n" +
		"  ```yml {.illustrative reason=\"x y\"}\n  b: 2\n  ```\n" +
		"```yaml\nc: 3\n```\n" +
		"```yamlx\nd: 4\n```\n"
	assertBlocks(t, extractYAMLBlocks("x.md", md), []docYAMLBlock{
		{file: "x.md", line: 1, info: "{.confd}", body: "a: 1"},
		{file: "x.md", line: 4, info: "{.illustrative reason=\"x y\"}", body: "  b: 2"},
		{file: "x.md", line: 7, info: "", body: "c: 3"},
	})
}

// TestExtractYAMLBlocksTracksFencesAsCommonMark: a ``` line inside another
// fence is that fence's text; ```` closes ```; only ASCII blanks are trimmed
// (U+3000 / \x1c before a fence make it no fence); a CRLF's CR stays in the
// body line and a lone CR is no line break — the inventory's cases
// (tests/lint/test_doc_yaml_inventory.py), so the two extractors cut alike.
func TestExtractYAMLBlocksTracksFencesAsCommonMark(t *testing.T) {
	t.Parallel()
	md := "````markdown\n```yaml\nhidden: 1\n```\n````\n" +
		"```yaml\na: 1\n````\n" +
		"~~~text\n```yaml\n~~~\n" +
		"```yaml\nb: 2\n```\n"
	assertBlocks(t, extractYAMLBlocks("x.md", md), []docYAMLBlock{
		{file: "x.md", line: 6, body: "a: 1"},
		{file: "x.md", line: 12, body: "b: 2"},
	})
	for _, blank := range []string{"\u3000", "\x1c", "\u00a0"} {
		if got := extractYAMLBlocks("x.md", blank+"```yaml\na: 1\n```\n"); len(got) != 0 {
			t.Errorf("%q before ```yaml: got %+v, want no fence", blank, got)
		}
	}
	crlf := "prose\rmore prose\r\n\r\n```yaml\r\na: 1\r\nb: 2\r\n```\r\n"
	assertBlocks(t, extractYAMLBlocks("x.md", crlf), []docYAMLBlock{
		{file: "x.md", line: 3, body: "a: 1\r\nb: 2\r"},
	})
}

func assertBlocks(t *testing.T, got, want []docYAMLBlock) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("got %d blocks %+v, want %+v", len(got), got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("block %d: got %+v, want %+v", i, got[i], want[i])
		}
	}
}
