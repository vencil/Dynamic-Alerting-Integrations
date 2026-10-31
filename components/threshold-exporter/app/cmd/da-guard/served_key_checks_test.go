package main

// ADR-038 §11: served-values hands the CI readers, per tenant, the two key
// checks they print and no longer judge themselves — ValidateTenantKeys'
// messages on a `version` label (ADR-024 OQ-6) and on a retired spelling
// (#1231) — and nothing else of that judgement (whether a key is read is the
// shared key verdict's). Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestServedValues_KeyChecks(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  container_cpu: 80\n  mysql_threads_running: 30\n",
		"tx.yaml": "tenants:\n  tx:\n" +
			"    mysql_cpu: \"40\"\n" + // retired spelling
			"    container_cpu{version=\"V2!\"}: \"5\"\n" + // a label the exporter refuses
			"    container_cpu{version=\"v2\"}: \"6\"\n" + // a good label: no message
			"    mysql_conections: \"5\"\n", // unread: the verdict's, not a key check
		"ty.yaml": "tenants:\n  ty:\n    container_cpu: \"70\"\n",
	}
	var out, errOut bytes.Buffer
	if c := runServedValues([]string{"--config-dir", writeTree(t, files)}, &out, &errOut); c != exitOK {
		t.Fatalf("served-values exit %d: %s", c, errOut.String())
	}
	var doc struct {
		Tenants map[string]struct {
			KeyChecks *servedKeyChecks `json:"key_checks"`
		} `json:"tenants"`
	}
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	tx := doc.Tenants["tx"].KeyChecks
	if tx == nil {
		t.Fatalf("tx has no key_checks:\n%s", out.String())
	}
	if len(tx.VersionLabels) != 1 || !strings.Contains(tx.VersionLabels[0], `version "V2!"`) {
		t.Errorf("version_labels = %q, want the one bad label", tx.VersionLabels)
	}
	if len(tx.Renames) != 1 || !strings.Contains(tx.Renames[0], `"mysql_cpu" was renamed to "mysql_threads_running"`) {
		t.Errorf("renames = %q, want mysql_cpu's", tx.Renames)
	}
	for _, m := range append(append([]string{}, tx.VersionLabels...), tx.Renames...) {
		if strings.Contains(m, "mysql_conections") {
			t.Errorf("an unread key in key_checks: %q", m)
		}
	}
	// Always present, [] when none: a reader tells "none" from "not given".
	ty := doc.Tenants["ty"].KeyChecks
	if ty == nil || ty.VersionLabels == nil || ty.Renames == nil || len(ty.VersionLabels)+len(ty.Renames) != 0 {
		t.Errorf("ty key_checks = %+v, want both lists present and empty", ty)
	}
}
