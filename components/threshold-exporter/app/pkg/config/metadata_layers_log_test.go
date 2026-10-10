package config

// #2830: MetadataResolver writes nothing to the process log — not the root
// carrier's parse ERROR, not ApplyProfiles' unknown-profile WARN, not
// ResolveMetadata's decode WARN — while the tenant-api merge core keeps the
// carrier ERROR on the same tree (the control below).
//
// ⛔ NOT t.Parallel(): swaps the process-global log output (captureGlobalLog
// — an idempotent reset to os.Stderr, never a save-then-restore).

import (
	"strings"
	"testing"
)

func TestMetadataResolverWritesNoLog(t *testing.T) {
	dir := t.TempDir()
	writeMergeTree(t, dir, map[string]string{
		"_defaults.yaml": "defaults: [\n",
		"_profiles.yaml": metadataProfiles,
		"_platform.yaml": "tenants:\n  tz:\n    _profile: nope\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: pfin\n",
		"ty.yaml":        "tenants:\n  ty:\n    _metadata:\n      environment: {a: b}\n",
		"tz.yaml":        "tenants:\n  tz: {}\n",
	})
	buf := captureGlobalLog(t)

	root, err := LoadRootPlatformChecked(dir)
	if err != nil {
		t.Fatalf("LoadRootPlatformChecked: %v", err)
	}
	r := root.MetadataResolver()
	for _, id := range []string{"tx", "ty", "tz"} {
		body := "tenants:\n  " + id + ": {}\n"
		if id == "tx" {
			body = "tenants:\n  tx:\n    _profile: pfin\n"
		}
		if _, ok, _ := r.ResolveFile(id, []byte(body)); !ok {
			t.Fatalf("%s not read", id)
		}
	}
	if got, _, _ := r.ResolveFile("tx", []byte("tenants:\n  tx:\n    _profile: pfin\n")); got.Owner != "team-p" {
		t.Errorf("tx owner = %q, want team-p (the profile's)", got.Owner)
	}
	if _, ok, _ := r.ResolveFile("ty", []byte("tenants:\n  ty:\n    _metadata:\n      environment: {a: b}\n")); !ok {
		t.Fatal("ty not read")
	}
	if buf.Len() != 0 {
		t.Errorf("MetadataResolver wrote to the log:\n%s", buf.String())
	}

	// Control: the tree does make the merge core log the carrier ERROR.
	MergeTenantWithRootDefaults(dir, "tx", []byte("tenants:\n  tx: {}\n"))
	if !strings.Contains(buf.String(), "failed to parse") {
		t.Errorf("control: merge core logged %q, want the carrier parse ERROR", buf.String())
	}
}
