package config

// docs_yaml_inventory_test.go — this package's reader of the docs' YAML
// inventory (#3416).
//
// Which ```yaml unit in docs/**/*.md is which file, which consumer reads it and
// whether it is a `_defaults.yaml` is decided in ONE place:
// scripts/tools/_lib_doc_yaml_inventory.py, which asks the site's renderer
// (Python-Markdown with mkdocs.yml's extensions) which code blocks exist
// (#3814). The drift lint, the unit-sanity lint and this package's loader test
// (docs_defaults_sample_test.go) all read that inventory. Go keeps no
// extractor of its own: a second hand-written reader of Markdown is the
// disagreement #3814 removed, not a check on it.
//
// This file runs the inventory's CLI (python3 with the renderer's packages,
// and a da-guard: the default reader of every undeclared unit is
// `da-guard parse-tree`) once per test binary. python3 missing fails under CI
// and skips elsewhere (the tenant-api generator tests' rule): CI must run
// this, a laptop without python3 need not.

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
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
// root: `go test` runs in the package directory), else one build of
// ./cmd/da-guard into a temporary directory through scripts/ops/go_build.sh,
// the one place the build flags are written (#3425: the shipped shape).
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
	cmd := exec.Command("sh", filepath.Join(root, "scripts", "ops", "go_build.sh"),
		filepath.Join(root, "components", "threshold-exporter", "app"), "./cmd/da-guard", out)
	if b, err := cmd.CombinedOutput(); err != nil {
		return "", fmt.Errorf("build da-guard: %v: %s", err, strings.TrimSpace(string(b)))
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
