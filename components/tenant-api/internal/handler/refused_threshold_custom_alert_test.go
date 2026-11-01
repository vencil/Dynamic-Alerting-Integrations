package handler

// #3267: a custom alert's `threshold:` is judged by the same reading as a
// threshold key (resolveOneCustomAlert: splitThresholdSeverity, then
// parseThresholdNumber — the classes config.RefusedValueAt gives), so the
// values the threshold keys now refuse with 400 are refused on both custom
// alert write paths too, and the values the exporter reads are taken.

import (
	"net/http"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

func TestCustomAlertThreshold_RefusedAsAThresholdKey(t *testing.T) {
	t.Parallel()
	type tc struct {
		value string
		want  int
	}
	var cases []tc
	for _, v := range refusedThresholdValues {
		cases = append(cases, tc{v, http.StatusBadRequest})
	}
	for _, v := range acceptedThresholdValues {
		cases = append(cases, tc{v, http.StatusOK})
	}
	for _, c := range cases {
		t.Run("custom-alerts/"+c.value, func(t *testing.T) {
			t.Parallel()
			dir := setupConfigDir(t, map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults})
			initGitRepo(t, dir)
			deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}
			h := cfg.ComputeSourceHash([]byte(caTenantYAML))
			body := `{"base_hash":"` + h + `","custom_alerts":[{"recipe":"threshold","name":"q","metric":"queue_depth",` +
				`"threshold":"` + c.value + `","window":"5m"}]}`
			resp := putCustomAlerts(t, deps, "db-a", body, "alice@example.com", []string{"dba"})
			if resp.StatusCode != c.want {
				b, _ := readBody(resp)
				t.Fatalf("threshold %q: status = %d, want %d; body: %s", c.value, resp.StatusCode, c.want, b)
			}
		})
		t.Run("tenant-put/"+c.value, func(t *testing.T) {
			t.Parallel()
			body := "tenants:\n  " + parityIn + ":\n    _custom_alerts:\n" +
				"      - recipe: threshold\n        name: q\n        metric: queue_depth\n        op: \">\"\n" +
				"        window: 5m\n        threshold: \"" + c.value + "\"\n"
			w := parityServe(t, parityCase{file: "tenants:\n" + paritySec(parityIn, "warning"), groups: "in-team",
				body: body}, false, endpointPut)
			if w.Code != c.want {
				t.Fatalf("threshold %q: PUT = %d %s, want %d", c.value, w.Code, w.Body.String(), c.want)
			}
		})
	}
}
