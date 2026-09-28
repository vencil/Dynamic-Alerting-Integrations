package config

// merge_tenant_profile_test.go — the tenant-api merge core
// (MergeTenantWithRootDefaults / MergeParsedTenantWithRootDefaults: GET
// /tenants/{id}, POST …/validate and the write gate) expands the profile a
// tenant elects the way /metrics does (#1385).
//
// The oracle is /metrics itself: LoadDir (ScanDirTree → BuildFlatConfig →
// ApplyProfiles) resolved at one instant, row for row. The validation half
// pins that a profile problem is advice attributed to the profile's file,
// never a reason to refuse the tenant's write, and that the tenant's own
// Errors are what they always were.
//
// Seams: none — t.TempDir() trees.

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

const profileMergeProfiles = "profiles:\n  strict:\n    mysql_connections: \"55\"\n    pg_connections_critical: \"190\"\n"

func profileMergeTrees() []struct {
	name  string
	files map[string]string
	// wantExpands marks trees where the profile changes what /metrics
	// serves — so the table is not a set of controls only.
	wantExpands bool
} {
	with := func(extra map[string]string) map[string]string {
		m := map[string]string{"_defaults.yaml": platformMergeDefaults}
		for k, v := range extra {
			m[k] = v
		}
		return m
	}
	elects := "tenants:\n  tx:\n    _profile: strict\n"
	return []struct {
		name        string
		files       map[string]string
		wantExpands bool
	}{
		{"profile in _profiles.yaml, elected by the tenant", with(map[string]string{
			"_profiles.yaml": profileMergeProfiles, "tx.yaml": elects}), true},
		{"profile in the root carrier", map[string]string{
			"_defaults.yaml": platformMergeDefaults + profileMergeProfiles, "tx.yaml": elects}, true},
		{"profile in some other root platform file", with(map[string]string{
			"_team.yaml": profileMergeProfiles, "tx.yaml": elects}), true},
		{"elected by a platform tenants: entry", with(map[string]string{
			"_profiles.yaml": profileMergeProfiles,
			"_platform.yaml": "tenants:\n  tx:\n    _profile: strict\n",
			"tx.yaml":        "tenants:\n  tx:\n    redis_memory: \"71\"\n"}), true},
		{"the tenant's _profile over the platform entry's", with(map[string]string{
			"_profiles.yaml": profileMergeProfiles + "  loose:\n    mysql_connections: \"99\"\n",
			"_platform.yaml": "tenants:\n  tx:\n    _profile: loose\n",
			"tx.yaml":        elects}), true},
		{"a key the tenant writes beats the profile", with(map[string]string{
			"_profiles.yaml": profileMergeProfiles,
			"tx.yaml":        "tenants:\n  tx:\n    _profile: strict\n    mysql_connections: \"44\"\n"}), true},
		{"a platform entry's key beats the profile", with(map[string]string{
			"_profiles.yaml": profileMergeProfiles,
			"_platform.yaml": "tenants:\n  tx:\n    mysql_connections: \"66\"\n",
			"tx.yaml":        elects}), true},
		{"defined in two files: the later one wins key by key", with(map[string]string{
			"_a.yaml":        "profiles:\n  strict:\n    mysql_connections: \"11\"\n    redis_memory: \"12\"\n",
			"_profiles.yaml": profileMergeProfiles,
			"tx.yaml":        elects}), true},
		{"unknown profile expands nothing", with(map[string]string{
			"_profiles.yaml": profileMergeProfiles,
			"tx.yaml":        "tenants:\n  tx:\n    _profile: nosuch\n"}), false},
		{"an empty _profile expands nothing", with(map[string]string{
			"_profiles.yaml": profileMergeProfiles,
			"tx.yaml":        "tenants:\n  tx:\n    _profile: \"\"\n"}), false},
		{"a nested _profiles.yaml is read by no plane", with(map[string]string{
			"sub/_profiles.yaml": profileMergeProfiles, "tx.yaml": elects}), false},
		{"an unselected root carrier's profiles are not read", with(map[string]string{
			"_defaults.yml": profileMergeProfiles, "tx.yaml": elects}), false},
		{"a profile file the flat decode rejects contributes nothing", with(map[string]string{
			"_profiles.yaml": profileMergeProfiles + "defaults:\n  x: abc\n", "tx.yaml": elects}), false},
		{"a profile may not fill a declared key", with(map[string]string{
			"_profiles.yaml": "profiles:\n  strict:\n    oracle_wait: \"5\"\n    mysql_connections: \"55\"\n",
			"tx.yaml":        elects}), true},
		{"a profile's legacy spelling loses to the tenant's canonical one", map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_threads_running: 70\n",
			"_profiles.yaml": "profiles:\n  strict:\n    mysql_cpu: \"33\"\n",
			"tx.yaml":        "tenants:\n  tx:\n    _profile: strict\n    mysql_threads_running: \"34\"\n"}, false},
		{"a profile's legacy spelling fills the canonical key", map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_threads_running: 70\n",
			"_profiles.yaml": "profiles:\n  strict:\n    mysql_cpu: \"33\"\n",
			"tx.yaml":        elects}, true},
		{"scheduled and disabled profile values", with(map[string]string{
			"_profiles.yaml": "profiles:\n  strict:\n    redis_memory:\n      default: \"52\"\n      overrides:\n        - window: \"00:00-23:59\"\n          value: \"51\"\n    pg_connections: disable\n",
			"tx.yaml":        elects}), true},
		{"a non-numeric profile value falls back to the default on both", with(map[string]string{
			"_profiles.yaml": "profiles:\n  strict:\n    mysql_connections: abc\n",
			"tx.yaml":        elects}), false},
	}
}

func TestMergeTenantProfilesMatchMetrics(t *testing.T) {
	t.Parallel()
	for _, tc := range profileMergeTrees() {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeMergeTree(t, dir, tc.files)
			oracle, _, err := LoadDir(dir, nil)
			if err != nil {
				t.Fatalf("LoadDir: %v", err)
			}
			want := resolvedRows(oracle, "tx")
			body, err := os.ReadFile(filepath.Join(dir, "tx.yaml"))
			if err != nil {
				t.Fatal(err)
			}
			byteMerge := MergeTenantWithRootDefaults(dir, "tx", body)
			if got := resolvedRows(&byteMerge, "tx"); strings.Join(got, "\n") != strings.Join(want, "\n") {
				t.Errorf("GET merge rows differ from /metrics\n got: %v\nwant: %v", got, want)
			}
			parsed, err := ParseConfigFile(body)
			if err != nil {
				t.Fatal(err)
			}
			parsedMerge := MergeParsedTenantWithRootDefaults(dir, parsed)
			if got := resolvedRows(&parsedMerge, "tx"); strings.Join(got, "\n") != strings.Join(want, "\n") {
				t.Errorf("write-gate merge rows differ from /metrics\n got: %v\nwant: %v", got, want)
			}
			// The table's control column, measured: /metrics with every
			// profile removed from the tree.
			noProfiles := t.TempDir()
			for name, content := range tc.files {
				c, perr := ParseConfigFile([]byte(content))
				if perr == nil && len(c.Profiles) > 0 {
					content = stripProfiles(t, content)
				}
				writeMergeTree(t, noProfiles, map[string]string{name: content})
			}
			bare, _, err := LoadDir(noProfiles, nil)
			if err != nil {
				t.Fatal(err)
			}
			if expands := strings.Join(resolvedRows(bare, "tx"), "\n") != strings.Join(want, "\n"); expands != tc.wantExpands {
				t.Errorf("profile changes /metrics = %v, the table says %v", expands, tc.wantExpands)
			}
		})
	}
}

// stripProfiles removes the top-level `profiles:` block (the trees above
// write it as the last block or before a top-level key).
func stripProfiles(t *testing.T, content string) string {
	t.Helper()
	var out []string
	in := false
	for _, line := range strings.Split(content, "\n") {
		if line == "profiles:" {
			in = true
			continue
		}
		if in && (strings.HasPrefix(line, " ") || line == "") {
			continue
		}
		in = false
		out = append(out, line)
	}
	return strings.Join(out, "\n")
}

// profileKV merges body over tree and returns the tenant-api validation.
func profileKV(t *testing.T, files map[string]string, body string) (KeyValidation, TenantMerge) {
	t.Helper()
	dir := t.TempDir()
	writeMergeTree(t, dir, files)
	m := MergeTenantWithRootDefaults(dir, "tx", []byte(body))
	parsed, err := ParseConfigFile([]byte(body))
	if err == nil && len(parsed.Tenants) > 0 {
		pm := MergeParsedTenantWithRootDefaults(dir, parsed)
		// Sorted: validateOverrideKeys' order follows map iteration.
		a, b := m.ValidateTenantKeys(), pm.ValidateTenantKeys()
		if sortedJoin(a.Errors) != sortedJoin(b.Errors) || sortedJoin(a.Notices) != sortedJoin(b.Notices) {
			t.Fatalf("byte and parsed entry points disagree:\n%+v\n%+v", a, b)
		}
	}
	return m.ValidateTenantKeys(), m
}

func sortedJoin(s []string) string {
	c := append([]string(nil), s...)
	sort.Strings(c)
	return strings.Join(c, "|")
}

func TestMergeTenantDefinedProfileIsNotUnknown(t *testing.T) {
	t.Parallel()
	body := "tenants:\n  tx:\n    _profile: strict\n"
	for name, files := range map[string]map[string]string{
		"_profiles.yaml": {"_defaults.yaml": platformMergeDefaults, "_profiles.yaml": profileMergeProfiles},
		"root carrier":   {"_defaults.yaml": platformMergeDefaults + profileMergeProfiles},
	} {
		kv, _ := profileKV(t, files, body)
		if len(kv.Errors) != 0 || len(kv.Notices) != 0 {
			t.Errorf("%s: errors %q notices %q, want none", name, kv.Errors, kv.Notices)
		}
	}
	// Defined nowhere a root platform file is read: the historical Error,
	// verbatim.
	for name, files := range map[string]map[string]string{
		"no profile at all":     {"_defaults.yaml": platformMergeDefaults},
		"another name":          {"_defaults.yaml": platformMergeDefaults, "_profiles.yaml": profileMergeProfiles},
		"nested _profiles.yaml": {"_defaults.yaml": platformMergeDefaults, "sub/_profiles.yaml": profileMergeProfiles},
	} {
		b := body
		if name == "another name" {
			b = "tenants:\n  tx:\n    _profile: nosuch\n"
		}
		kv, _ := profileKV(t, files, b)
		wantName := "strict"
		if name == "another name" {
			wantName = "nosuch"
		}
		want := `WARN: tenant=tx: _profile references unknown profile "` + wantName + `"`
		if len(kv.Errors) != 1 || kv.Errors[0] != want {
			t.Errorf("%s: errors %q, want [%q]", name, kv.Errors, want)
		}
	}
}

func TestMergeTenantProfileProblemsAreNoticesNamingFileAndProfile(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": platformMergeDefaults,
		"_a.yaml":        "profiles:\n  strict:\n    bogus_key: \"1\"\n",
		"_profiles.yaml": "profiles:\n  strict:\n    _silense_mode: warning\n    redis_memory:\n      default: \"50\"\n      expires: not-a-time\n    oracle_wait: \"5\"\n    mysql_connections: abc\n",
	}
	body := "tenants:\n  tx:\n    _profile: strict\n"
	kv, _ := profileKV(t, files, body)
	if len(kv.Errors) != 0 {
		t.Fatalf("profile problems block the write: %q", kv.Errors)
	}
	want := []struct{ file, fragment string }{
		{"_a.yaml", `unknown key "bogus_key" not in defaults`},
		{"_profiles.yaml", `unknown reserved key "_silense_mode"`},
		{"_profiles.yaml", "invalid `expires:`"},
		{"_profiles.yaml", `key "oracle_wait" is declared without a platform value`},
	}
	for _, w := range want {
		n := 0
		for _, got := range kv.Notices {
			if strings.HasPrefix(got, "NOTICE: platform file "+w.file+", profile \"strict\" (elected by tenant tx via _profile): ") &&
				strings.Contains(got, w.fragment) && strings.Contains(got, "does not block writing it") {
				n++
			}
		}
		if n != 1 {
			t.Errorf("want one notice for %s naming %s; notices %q", w.fragment, w.file, kv.Notices)
		}
	}
	if len(kv.Notices) != len(want) {
		t.Errorf("notices %d, want %d (a non-numeric value is not judged, for a profile as for the tenant): %q", len(kv.Notices), len(want), kv.Notices)
	}

	// Must-fire control: the same keys in the tenant's own body are Errors.
	own := "tenants:\n  tx:\n    _profile: strict\n    bogus_key: \"1\"\n    _silense_mode: warning\n    redis_memory:\n      default: \"50\"\n      expires: not-a-time\n"
	kv, _ = profileKV(t, files, own)
	if len(kv.Errors) != 3 {
		t.Errorf("control: the tenant's own bad keys give errors %q, want 3", kv.Errors)
	}
	for _, n := range kv.Notices {
		if strings.Contains(n, "bogus_key") || strings.Contains(n, "_silense_mode") || strings.Contains(n, "expires") {
			t.Errorf("control: a key the tenant writes is still reported against the profile: %q", n)
		}
	}
}

// A profile elected by a platform entry is reported for the tenant too; a
// profile nobody elects is not reported at all.
func TestMergeTenantProfileNoticesOnlyForTheElectedProfile(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": platformMergeDefaults,
		"_profiles.yaml": "profiles:\n  strict:\n    bogus_key: \"1\"\n  other:\n    other_bogus: \"1\"\n",
		"_platform.yaml": "tenants:\n  tx:\n    _profile: strict\n",
	}
	kv, _ := profileKV(t, files, "tenants:\n  tx:\n    redis_memory: \"60\"\n")
	if len(kv.Errors) != 0 || len(kv.Notices) != 1 || !strings.Contains(kv.Notices[0], `profile "strict"`) ||
		!strings.Contains(kv.Notices[0], "bogus_key") {
		t.Errorf("errors %q notices %q, want one notice for strict's bogus_key", kv.Errors, kv.Notices)
	}
}

// The flat-KV fallback: the display map is not the tenant layer, so the
// profile's keys filled into it are not judged as the tenant's.
func TestMergeTenantFlatFallbackProfileIsNotTheTenants(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": platformMergeDefaults,
		"_profiles.yaml": "profiles:\n  strict:\n    bogus_key: \"1\"\n    mysql_connections: \"55\"\n",
	}
	kv, m := profileKV(t, files, "_profile: strict\n")
	if len(kv.Errors) != 0 {
		t.Errorf("errors %q: the profile's key judged as the tenant's", kv.Errors)
	}
	if _, ok := m.own["tx"]["bogus_key"]; ok {
		t.Errorf("the tenant layer holds the profile's key: %v", m.own["tx"])
	}
	if !containsRow(resolvedRows(&m, "tx"), "mysql_connections{}=55/warning") {
		t.Errorf("rows %v: the profile is not expanded for the flat document", resolvedRows(&m, "tx"))
	}
}

// Expanding writes into this merge's maps only: the shared decode cache
// (and so the next merge) never sees another merge's profile fill.
func TestMergeTenantProfileExpansionDoesNotReachTheCache(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeMergeTree(t, dir, map[string]string{
		"_defaults.yaml": platformMergeDefaults,
		"_profiles.yaml": profileMergeProfiles,
		"_platform.yaml": "tenants:\n  tx:\n    redis_memory: \"61\"\n",
	})
	a := MergeTenantWithRootDefaults(dir, "tx", []byte("tenants:\n  tx:\n    _profile: strict\n"))
	a.Profiles["strict"]["redis_memory"] = ScheduledValue{Default: "1"}
	a.Tenants["tx"]["pg_connections"] = ScheduledValue{Default: "2"}
	b := MergeTenantWithRootDefaults(dir, "tx", []byte("tenants:\n  tx: {}\n"))
	if _, ok := b.Profiles["strict"]["redis_memory"]; ok {
		t.Errorf("a write into one merge's profile reached the next: %v", b.Profiles["strict"])
	}
	if _, ok := b.Tenants["tx"]["pg_connections"]; ok || b.Tenants["tx"]["redis_memory"].Default != "61" {
		t.Errorf("tenant map shared across merges: %v", b.Tenants["tx"])
	}
}

// Witness trees for four mutants the blind review found alive; each notice
// is compared as the whole string.
func TestMergeTenantProfileNoticeWitnesses(t *testing.T) {
	t.Parallel()
	const tail = " — fix it in that file; it is not in this tenant's file and does not block writing it"
	for _, tc := range []struct {
		name  string
		files map[string]string
		body  string
		want  []string
	}{
		{
			// M2: the platform entry sets redis_memory, so /metrics serves
			// the platform's 61 and the profile's broken redis_memory never
			// reaches the tenant — nothing to report.
			name: "M2 a key the platform entry sets is not the profile's",
			files: map[string]string{
				"_defaults.yaml": platformMergeDefaults,
				"_platform.yaml": "tenants:\n  tx:\n    redis_memory: \"61\"\n",
				"_profiles.yaml": "profiles:\n  strict:\n    redis_memory:\n      default: \"50\"\n      expires: not-a-time\n",
			},
			body: "tenants:\n  tx:\n    _profile: strict\n",
			want: nil,
		},
		{
			// M3: both spellings in the profile, in different files; the
			// canonical one wins (canonicalView), so its file is named.
			name: "M3 the canonical spelling's file is named",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_threads_running: 70\n",
				"_a.yaml":        "profiles:\n  strict:\n    mysql_threads_running:\n      default: \"1\"\n      expires: bad-a\n",
				"_b.yaml":        "profiles:\n  strict:\n    mysql_cpu: \"2\"\n",
			},
			body: "tenants:\n  tx:\n    _profile: strict\n",
			want: []string{`NOTICE: platform file _a.yaml, profile "strict" (elected by tenant tx via _profile): invalid ` + "`expires:`" + ` "bad-a" on "mysql_threads_running" (need RFC3339 e.g. 2026-07-01T00:00:00Z) — override will NOT auto-revert` + tail},
		},
		{
			// M4 (the stripped prefix) and M6 (a padded name elects the
			// trimmed one, as ApplyProfiles trims it).
			name: "M4/M6 exact text, padded _profile",
			files: map[string]string{
				"_defaults.yaml": platformMergeDefaults,
				"_profiles.yaml": "profiles:\n  strict:\n    bogus_key: \"1\"\n",
			},
			body: "tenants:\n  tx:\n    _profile: \" strict \"\n",
			want: []string{`NOTICE: platform file _profiles.yaml, profile "strict" (elected by tenant tx via _profile): unknown key "bogus_key" not in defaults` + tail},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			kv, _ := profileKV(t, tc.files, tc.body)
			if len(kv.Errors) != 0 {
				t.Errorf("errors %q, want none", kv.Errors)
			}
			if strings.Join(kv.Notices, "\n") != strings.Join(tc.want, "\n") {
				t.Errorf("notices\n got: %q\nwant: %q", kv.Notices, tc.want)
			}
		})
	}
}
