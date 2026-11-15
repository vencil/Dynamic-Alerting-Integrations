package main

// write_fixture_test.go — the tree a `da-guard write` mechanics test writes
// into.

import "testing"

// declaredKeys are the threshold keys the write-mechanics fixtures write. A
// tree with no root platform file is judged by the key verdict since the
// built-in layer (#3832: no longer undecidable), so a key no layer declares
// is unread and the write that adds it is refused (worsens_tree). These
// tests are about how a file is written, not about the verdict: the root
// declares the keys (optional_overrides), so each one is read.
const declaredKeysDefaults = "optional_overrides: [a, cpu, mem, disk, \"b😀\"]\n"

// writeTreeDeclared is writeTree with a root `_defaults.yaml` declaring
// declaredKeys, unless files brings its own.
func writeTreeDeclared(t *testing.T, files map[string]string) string {
	t.Helper()
	out := make(map[string]string, len(files)+1)
	for k, v := range files {
		out[k] = v
	}
	if _, ok := out["_defaults.yaml"]; !ok {
		out["_defaults.yaml"] = declaredKeysDefaults
	}
	return writeTree(t, out)
}

// snapshotDeclared is snapshot without the root `_defaults.yaml`
// writeTreeDeclared added, while it still holds what was added (a test that
// writes the file itself sees it).
func snapshotDeclared(t *testing.T, root string) map[string]string {
	t.Helper()
	got := snapshot(t, root)
	if got["_defaults.yaml"] == declaredKeysDefaults {
		delete(got, "_defaults.yaml")
	}
	return got
}
