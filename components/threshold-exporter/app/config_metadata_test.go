package main

// Tenant metadata tests — _metadata block resolution + ValidateTenantKeys
// reserved-key handling. Split out of config_test.go in PR-2; shared
// helpers live in config_test.go.

import (
	"testing"
)

// region MetadataAndValidation — metadata resolution and tenant key validation

// ResolveMetadata (v1.11.0)
// ============================================================

func TestResolveMetadata_WithMetadata(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("70"),
				"_metadata":         SV("runbook_url: https://wiki.example.com/db-a\nowner: team-dba\ntier: gold\n"),
			},
		},
	}
	result := cfg.ResolveMetadata()
	if len(result) != 1 {
		t.Fatalf("expected 1 metadata, got %d", len(result))
	}
	if result[0].RunbookURL != "https://wiki.example.com/db-a" {
		t.Errorf("runbook_url = %q, want https://wiki.example.com/db-a", result[0].RunbookURL)
	}
	if result[0].Owner != "team-dba" {
		t.Errorf("owner = %q, want team-dba", result[0].Owner)
	}
	if result[0].Tier != "gold" {
		t.Errorf("tier = %q, want gold", result[0].Tier)
	}
}

func TestResolveMetadata_WithoutMetadata(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"mysql_connections": SV("70")},
			"db-b": {"redis_memory": SV("1024")},
		},
	}
	result := cfg.ResolveMetadata()
	if len(result) != 2 {
		t.Fatalf("expected 2 metadata entries (all tenants), got %d", len(result))
	}
	// All fields should be empty string
	for _, m := range result {
		if m.RunbookURL != "" || m.Owner != "" || m.Tier != "" {
			t.Errorf("tenant=%s: expected empty metadata, got runbook=%q owner=%q tier=%q",
				m.Tenant, m.RunbookURL, m.Owner, m.Tier)
		}
	}
}

func TestResolveMetadata_PartialMetadata(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_metadata": SV("owner: team-dba\n"),
			},
		},
	}
	result := cfg.ResolveMetadata()
	if len(result) != 1 {
		t.Fatalf("expected 1, got %d", len(result))
	}
	if result[0].Owner != "team-dba" {
		t.Errorf("owner = %q, want team-dba", result[0].Owner)
	}
	if result[0].RunbookURL != "" {
		t.Errorf("runbook_url = %q, want empty", result[0].RunbookURL)
	}
	if result[0].Tier != "" {
		t.Errorf("tier = %q, want empty", result[0].Tier)
	}
}

func TestResolveMetadata_Sorted(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-c": {"mysql_connections": SV("70")},
			"db-a": {"mysql_connections": SV("70")},
			"db-b": {"mysql_connections": SV("70")},
		},
	}
	result := cfg.ResolveMetadata()
	if len(result) != 3 {
		t.Fatalf("expected 3, got %d", len(result))
	}
	if result[0].Tenant != "db-a" || result[1].Tenant != "db-b" || result[2].Tenant != "db-c" {
		t.Errorf("not sorted: %v, %v, %v", result[0].Tenant, result[1].Tenant, result[2].Tenant)
	}
}

func TestResolveMetadata_UnconditionalOutput(t *testing.T) {
	// All tenants must appear regardless of _metadata presence
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("70"),
				"_metadata":         SV("runbook_url: https://wiki.example.com/db-a\n"),
			},
			"db-b": {"redis_memory": SV("1024")}, // no _metadata
		},
	}
	result := cfg.ResolveMetadata()
	if len(result) != 2 {
		t.Fatalf("expected 2 (all tenants), got %d", len(result))
	}
	tenants := map[string]ResolvedMetadata{}
	for _, m := range result {
		tenants[m.Tenant] = m
	}
	if tenants["db-a"].RunbookURL != "https://wiki.example.com/db-a" {
		t.Errorf("db-a runbook_url = %q", tenants["db-a"].RunbookURL)
	}
	if tenants["db-b"].RunbookURL != "" {
		t.Errorf("db-b should have empty runbook_url, got %q", tenants["db-b"].RunbookURL)
	}
}

// endregion
