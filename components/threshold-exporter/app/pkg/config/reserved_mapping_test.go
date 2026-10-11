package config

import "testing"

// #2859: a reserved key's mapping that happens to have a `default` key used to
// be decoded as a scheduled threshold, and every other key was dropped:
// `_metadata: {default: x, environment: production, owner: team-a}` served
// no environment and no owner, while the same mapping without `default` did.
func TestReservedMappingWithDefaultKeyKeepsEveryKey(t *testing.T) {
	data := []byte("tenants:\n" +
		"  t1:\n    _metadata: {default: x, environment: production, owner: team-a}\n" +
		"  t2:\n    _metadata: {environment: production, owner: team-b}\n" +
		"  t3:\n    cpu:\n      default: \"80\"\n      overrides: []\n")
	cfg, err := ParseConfigFile(data)
	if err != nil {
		t.Fatal(err)
	}
	for tenant, owner := range map[string]string{"t1": "team-a", "t2": "team-b"} {
		meta, ok, decoded := MetadataResolver{}.ResolveFile(tenant, data)
		if !ok || !decoded || meta.Environment != "production" || meta.Owner != owner {
			t.Errorf("%s: metadata = %+v (ok %v, decoded %v); want environment production, owner %s", tenant, meta, ok, decoded, owner)
		}
	}
	// A threshold's {default, overrides} is still a scheduled value.
	if got := cfg.Tenants["t3"]["cpu"].Default; got != "80" {
		t.Errorf("t3 cpu default = %q, want 80", got)
	}
}
