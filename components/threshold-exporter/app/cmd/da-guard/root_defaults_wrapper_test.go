package main

import (
	"encoding/json"
	"path/filepath"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// servedTx is what /metrics serves tenant tx for mysql_connections and
// _severity_dedup (da-guard served-values) over files written to a conf.d.
func servedTx(t *testing.T, files map[string]string) (any, any) {
	t.Helper()
	tmp := t.TempDir()
	tree := make(map[string]string, len(files))
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", filepath.Join(tmp, "conf.d"))
	if code != exitOK {
		t.Fatalf("served-values exit %d: %s", code, stderr)
	}
	var doc struct {
		Tenants map[string]struct {
			Values map[string]any `json:"values"`
		} `json:"tenants"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatal(err)
	}
	v := doc.Tenants["tx"].Values
	return v["mysql_connections"], v["_severity_dedup"]
}

// #2386: each shape's finding, and — so the finding's premise stays measured
// — what /metrics serves for it. The subtree shapes carry the same two
// top-level keys; only the `defaults:` line differs.
func TestRun_DefaultsWrapper(t *testing.T) {
	t.Parallel()
	const root = "defaults:\n  mysql_connections: 80\n"
	const tenant = "tenants:\n  tx:\n    container_cpu: \"70\"\n"
	const top = "_severity_dedup: disable\nmysql_connections: 70\n"
	cases := []struct {
		name      string
		files     map[string]string
		wantCode  int
		want      []string
		wantConn  any
		wantDedup any
	}{
		{
			name:     "root-unwrapped-threshold-is-not-served",
			files:    map[string]string{"_defaults.yaml": "mysql_connections: 80\n", "team/tx.yaml": tenant},
			wantCode: exitFindings, want: []string{"error root_defaults_unwrapped  _defaults.yaml"},
			wantConn: nil, wantDedup: "enable",
		},
		{
			// /effective shows `disable` (the merge reads the whole document);
			// /metrics serves `enable`.
			name:     "root-unwrapped-reserved-key-is-not-served",
			files:    map[string]string{"_defaults.yaml": "_severity_dedup: disable\n", "team/tx.yaml": tenant},
			wantCode: exitFindings, want: []string{"error root_defaults_unwrapped  _defaults.yaml"},
			wantConn: nil, wantDedup: "enable",
		},
		{
			name:     "root-wrapped-is-clean",
			files:    map[string]string{"_defaults.yaml": root, "team/tx.yaml": tenant},
			wantCode: exitOK, want: []string{},
			wantConn: 80.0, wantDedup: "enable",
		},
		{
			name:     "subtree-unwrapped-is-merged-whole",
			files:    map[string]string{"_defaults.yaml": root, "team/_defaults.yaml": top, "team/tx.yaml": tenant},
			wantCode: exitOK, want: []string{},
			wantConn: 70.0, wantDedup: "disable",
		},
		{
			name:     "subtree-empty-mapping-leaves-the-top-level-out",
			files:    map[string]string{"_defaults.yaml": root, "team/_defaults.yaml": "defaults: {}\n" + top, "team/tx.yaml": tenant},
			wantCode: exitFindings, want: []string{"error defaults_toplevel_ignored  team/_defaults.yaml"},
			wantConn: 80.0, wantDedup: "enable",
		},
		{
			// ⛔ The control the finding must not fire on: `defaults:` with no
			// value is not a mapping, and the merge still reads the whole file.
			name:     "subtree-null-defaults-is-merged-whole",
			files:    map[string]string{"_defaults.yaml": root, "team/_defaults.yaml": "defaults:\n" + top, "team/tx.yaml": tenant},
			wantCode: exitOK, want: []string{},
			wantConn: 70.0, wantDedup: "disable",
		},
		{
			name: "subtree-keys-under-defaults",
			files: map[string]string{"_defaults.yaml": root,
				"team/_defaults.yaml": "defaults:\n  _severity_dedup: disable\n  mysql_connections: 70\n", "team/tx.yaml": tenant},
			wantCode: exitOK, want: []string{},
			wantConn: 70.0, wantDedup: "disable",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			code, got := runRoutingTree(t, tc.files, "")
			if code != tc.wantCode {
				t.Errorf("exit = %d, want %d; findings %v", code, tc.wantCode, got)
			}
			if !equalStrings(got, tc.want) {
				t.Errorf("findings:\n got %q\nwant %q", got, tc.want)
			}
			conn, dedup := servedTx(t, tc.files)
			if conn != tc.wantConn || dedup != tc.wantDedup {
				t.Errorf("served mysql_connections=%v _severity_dedup=%v, want %v %v", conn, dedup, tc.wantConn, tc.wantDedup)
			}
		})
	}
}
