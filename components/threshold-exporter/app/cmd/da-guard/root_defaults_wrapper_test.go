package main

import "testing"

// #2386: a threshold at the top level of an unwrapped ROOT `_defaults.yaml`
// is not served on /metrics (served-values below), while the effective
// config the other checks read carries it — so da-guard names the file and
// exits 1. The wrapped spelling of the same tree is clean and served.
func TestRun_RootDefaultsWrapper(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name     string
		files    map[string]string
		wantCode int
		want     []string
	}{
		{
			name: "unwrapped-root-threshold-is-an-error",
			files: map[string]string{
				"_defaults.yaml": "mysql_connections: 80\n",
				"tx.yaml":        "tenants:\n  tx:\n    container_cpu: \"70\"\n",
			},
			wantCode: exitFindings,
			want:     []string{"error root_defaults_unwrapped  _defaults.yaml"},
		},
		{
			name: "wrapped-root-is-clean",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
				"tx.yaml":        "tenants:\n  tx:\n    container_cpu: \"70\"\n",
			},
			wantCode: exitOK,
			want:     []string{},
		},
		{
			name: "unwrapped-subtree-threshold-is-served-and-clean",
			files: map[string]string{
				"_defaults.yaml":        "defaults:\n  container_cpu: 60\n",
				"team-a/_defaults.yaml": "mysql_connections: 70\n",
				"team-a/tx.yaml":        "tenants:\n  tx:\n    container_cpu: \"70\"\n",
			},
			wantCode: exitOK,
			want:     []string{},
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
		})
	}
}
