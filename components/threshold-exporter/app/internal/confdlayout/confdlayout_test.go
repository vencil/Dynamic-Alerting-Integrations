package confdlayout

import "testing"

func TestLevelOf(t *testing.T) {
	t.Parallel()
	for in, want := range map[string]string{
		"db-a.yaml":            ".",
		"_domain_policy.yaml":  ".",
		"team/db-a.yaml":       "team",
		"team/sub/db-a.yaml":   "team/sub",
		`team\sub\db-a.yaml`:   "team/sub",
		"team/./sub/db-a.yaml": "team/sub",
	} {
		if got := LevelOf(in); got != want {
			t.Errorf("LevelOf(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestIsDomainPolicyFile(t *testing.T) {
	t.Parallel()
	for name, want := range map[string]bool{
		"_domain_policy.yaml":      true,
		"_domain_policy.yml":       true,
		"_Domain_Policy.yaml":      false,
		"_domain_policy.json":      false,
		"team/_domain_policy.yaml": false,
		"_routing_profiles.yaml":   false,
		"":                         false,
	} {
		if got := IsDomainPolicyFile(name); got != want {
			t.Errorf("IsDomainPolicyFile(%q) = %v, want %v", name, got, want)
		}
	}
}
