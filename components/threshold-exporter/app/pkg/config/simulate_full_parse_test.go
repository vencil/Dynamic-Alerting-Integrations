package config

// #1981: /simulate refuses what the exporter would drop on load. Each case
// builds the same tree on disk and asks the exporter's own load (LoadDir) for
// its verdict, so "simulate rejects" is checked against "the real path skips
// the file" rather than against a hand-kept list.

import (
	"errors"
	"fmt"
	"io"
	"log"
	"strings"
	"testing"
)

func TestSimulate_RejectsWhatTheExporterSkips(t *testing.T) {
	t.Parallel()
	const (
		validL0     = "defaults:\n  mysql_connections: 80\n"
		validTenant = "tenants:\n  tx:\n    mysql_connections: \"90\"\n"
		emptyTenant = "tenants:\n  tx: {}\n"
	)
	cases := []struct {
		name   string
		l0, l1 string // "" = level absent
		tenant string
		// wantErr: substrings the error must carry ("" = must succeed).
		wantErr []string
		// wantFailed: the scan key LoadDir reports in parseFailed for the
		// same tree ("" = LoadDir must report none).
		wantFailed string
		// wantVal: simulated effective mysql_connections on success.
		wantVal any
	}{
		{
			// Already an error before #1981, but only in the merge
			// ("simulate merge: …"); now refused up front by the same
			// decode the walker uses, and the message names tenant_yaml.
			name:       "tenant body is a scalar",
			l0:         validL0,
			tenant:     "tenants:\n  tx: 5\n",
			wantErr:    []string{"tenant_yaml", "exporter would skip", "cannot unmarshal"},
			wantFailed: "tx.yaml",
		},
		{
			name:       "tenant file defaults value is a string",
			l0:         validL0,
			tenant:     "defaults:\n  mysql_connections: abc\n" + validTenant,
			wantErr:    []string{"tenant_yaml", "exporter would skip", "abc"},
			wantFailed: "tx.yaml",
		},
		{
			name:       "tenant file max_metrics_per_tenant is not an int",
			l0:         validL0,
			tenant:     "max_metrics_per_tenant: abc\n" + validTenant,
			wantErr:    []string{"tenant_yaml", "exporter would skip", "abc"},
			wantFailed: "tx.yaml",
		},
		{
			// Pins ParseTenantFile, not bare ParseConfigFile: the latter
			// admits a non-UTF-8 tenant id, the walker (#2266) does not.
			name:       "tenant file declares a non-UTF-8 tenant id",
			l0:         validL0,
			tenant:     "tenants:\n  !!binary /w==: {}\n  tx: {}\n",
			wantErr:    []string{"tenant_yaml", "exporter would skip", "not valid UTF-8"},
			wantFailed: "tx.yaml",
		},
		{
			// The exporter drops the WHOLE root file, cpu included.
			name:       "L0 defaults value is a string",
			l0:         "defaults:\n  mysql_connections: abc\n  cpu: 80\n",
			tenant:     validTenant,
			wantErr:    []string{"defaults_chain_yaml[0]", "L0, root _defaults.yaml", "abc"},
			wantFailed: "_defaults.yaml",
		},
		{
			// The subtree plane decodes nested chain files into `any`, so a
			// quoted number takes effect on the real path too.
			name:    "L1 value quoted number",
			l0:      validL0,
			l1:      "defaults:\n  mysql_connections: \"70\"\n",
			tenant:  emptyTenant,
			wantVal: "70",
		},
		{
			// Unchanged by #1981 on purpose: the exporter does not reject a
			// nested _defaults.yaml for a non-numeric value either (it only
			// syntax-probes it), so refusing it here would make simulate
			// stricter than the real path. What VALUE each plane then shows
			// for it is a separate divergence, tracked in #2296.
			name:    "L1 value non-numeric string",
			l0:      validL0,
			l1:      "defaults:\n  mysql_connections: abc\n",
			tenant:  emptyTenant,
			wantVal: "abc",
		},
		{
			name:    "control: valid tree",
			l0:      validL0,
			tenant:  validTenant,
			wantVal: "90",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			// Disk tree for the exporter's verdict: L0 at the root, L1 in
			// a/, the tenant beside the deepest level.
			files := map[string]string{}
			var chain [][]byte
			tenantKey := "tx.yaml"
			if tc.l0 != "" {
				files["_defaults.yaml"] = tc.l0
				chain = append(chain, []byte(tc.l0))
			}
			if tc.l1 != "" {
				files["a/_defaults.yaml"] = tc.l1
				chain = append(chain, []byte(tc.l1))
				tenantKey = "a/tx.yaml"
			}
			files[tenantKey] = tc.tenant
			dir := writeSimProfileTree(t, files)
			_, parseFailed, lerr := LoadDir(dir, log.New(io.Discard, "", 0))
			if lerr != nil {
				t.Fatalf("LoadDir: %v", lerr)
			}
			wantFailed := tc.wantFailed
			if wantFailed == "tx.yaml" {
				wantFailed = tenantKey
			}
			switch {
			case wantFailed == "" && len(parseFailed) != 0:
				t.Fatalf("exporter oracle: parseFailed = %v, want none", parseFailed)
			case wantFailed != "" && (len(parseFailed) != 1 || parseFailed[0] != wantFailed):
				t.Fatalf("exporter oracle: parseFailed = %v, want [%s]", parseFailed, wantFailed)
			}

			sim, err := SimulateEffective(SimulateRequest{TenantID: "tx", TenantYAML: []byte(tc.tenant), DefaultsChainYAML: chain})
			if len(tc.wantErr) > 0 {
				if err == nil {
					t.Fatalf("simulate err = nil (config %v), want an error: the exporter skips %s", sim.Config, wantFailed)
				}
				for _, sub := range tc.wantErr {
					if !strings.Contains(err.Error(), sub) {
						t.Errorf("simulate err = %q, want it to contain %q", err, sub)
					}
				}
				return
			}
			if err != nil {
				t.Fatalf("simulate err = %v, want success", err)
			}
			if got := sim.Config["mysql_connections"]; got != tc.wantVal {
				t.Errorf("mysql_connections = %v (%T), want %v (%T)", got, got, tc.wantVal, tc.wantVal)
			}
		})
	}
}

// The L1 "70" case above takes effect on /metrics as 70 — the reason simulate
// must keep accepting it.
func TestSimulate_L1QuotedNumberIsServed(t *testing.T) {
	t.Parallel()
	dir := writeSimProfileTree(t, map[string]string{
		"_defaults.yaml":   "defaults:\n  mysql_connections: 80\n",
		"a/_defaults.yaml": "defaults:\n  mysql_connections: \"70\"\n",
		"a/tx.yaml":        "tenants:\n  tx: {}\n",
	})
	if got := servedMySQL(t, dir); got != 70 {
		t.Fatalf("served mysql_connections = %v, want 70", got)
	}
}

// The control tree's merged_hash is the one measured before #1981
// (origin/main 9b1f4e47): the full-parse gate adds a refusal, never a change
// to an accepted request's result.
func TestSimulate_FullParseLeavesValidResultUnchanged(t *testing.T) {
	t.Parallel()
	sim, err := SimulateEffective(SimulateRequest{
		TenantID:          "tx",
		TenantYAML:        []byte("tenants:\n  tx:\n    mysql_connections: \"90\"\n"),
		DefaultsChainYAML: [][]byte{[]byte("defaults:\n  mysql_connections: 80\n  cpu: 75\n"), []byte("defaults:\n  cpu: \"70\"\n")},
	})
	if err != nil {
		t.Fatal(err)
	}
	const want = "c0c3733f5958582f"
	if sim.MergedHash != want {
		t.Fatalf("merged_hash = %s, want %s (config %v)", sim.MergedHash, want, sim.Config)
	}
}

// A payload with many wrong-typed values yields a bounded {error} that still
// says how many errors there were — yaml.v3 renders one line per type error,
// so uncapped this grew with the input (#1981 blind review SF2).
func TestSimulate_RejectionMessageIsCapped(t *testing.T) {
	t.Parallel()
	const n = 5000
	var tenant, l0 strings.Builder
	tenant.WriteString("defaults:\n")
	l0.WriteString("defaults:\n")
	for i := 0; i < n; i++ {
		fmt.Fprintf(&tenant, "  k%d: abc\n", i)
		fmt.Fprintf(&l0, "  k%d: abc\n", i)
	}
	tenant.WriteString("tenants:\n  tx: {}\n")
	cases := []struct {
		name  string
		req   SimulateRequest
		field string
	}{
		{"tenant_yaml", SimulateRequest{TenantID: "tx", TenantYAML: []byte(tenant.String())}, "tenant_yaml"},
		{"L0", SimulateRequest{TenantID: "tx", TenantYAML: []byte("tenants:\n  tx: {}\n"), DefaultsChainYAML: [][]byte{[]byte(l0.String())}}, "defaults_chain_yaml[0]"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, err := SimulateEffective(tc.req)
			if err == nil {
				t.Fatal("err = nil, want a rejection")
			}
			msg := err.Error()
			t.Logf("len(error) = %d bytes", len(msg))
			if len(msg) > simulateErrorBytes+512 {
				t.Errorf("len(error) = %d, want ≤ %d", len(msg), simulateErrorBytes+512)
			}
			if got := strings.Count(msg, "cannot unmarshal"); got != simulateErrorLines {
				t.Errorf("error lists %d decode errors, want %d", got, simulateErrorLines)
			}
			want := fmt.Sprintf("… and %d more errors (%d total)", n-simulateErrorLines, n)
			if !strings.Contains(msg, want) || !strings.Contains(msg, tc.field) {
				t.Errorf("error = %.300q…, want it to name %q and end with %q", msg, tc.field, want)
			}
		})
	}
}

// A file the exporter would drop is rejected even when tenant_id is also
// missing from it: the rejection (400) precedes ErrSimulateTenantNotFound
// (404) — #1981 blind review SF3.
func TestSimulate_RejectionPrecedesTenantNotFound(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		req   SimulateRequest
		field string
	}{
		{"broken tenant file", SimulateRequest{TenantID: "missing", TenantYAML: []byte("defaults:\n  x: abc\ntenants:\n  tx: {}\n")}, "tenant_yaml"},
		{"broken L0", SimulateRequest{TenantID: "missing", TenantYAML: []byte("tenants:\n  tx: {}\n"), DefaultsChainYAML: [][]byte{[]byte("defaults:\n  x: abc\n")}}, "defaults_chain_yaml[0]"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, err := SimulateEffective(tc.req)
			if err == nil || errors.Is(err, ErrSimulateTenantNotFound) || !strings.Contains(err.Error(), tc.field) {
				t.Fatalf("err = %v, want the %s rejection, not ErrSimulateTenantNotFound", err, tc.field)
			}
		})
	}
}
