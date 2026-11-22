package config

// `_expected_exporters` (#4097): the reader, its check, the layers, and the
// refusal of the `_metadata.db_type` it replaces. Tenant and profile names
// are fixture names.

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

func svOf(t *testing.T, key, yamlValue string) ScheduledValue {
	t.Helper()
	var m map[string]ScheduledValue
	if err := yamlUnmarshalForTest(key+": "+yamlValue, &m); err != nil {
		t.Fatalf("%s: %s: %v", key, yamlValue, err)
	}
	return m[key]
}

// IsExpectedExporterKind searches the generated table, so it must be sorted
// and free of repeats; and it carries the two kinds that are no database —
// the hole `_metadata.db_type` left (a tenant running only nginx or a JVM
// could declare nothing).
func TestExpectedExporterKinds_TableIsSortedAndCoversApplicationPacks(t *testing.T) {
	t.Parallel()
	if !sort.StringsAreSorted(expectedExporterKinds) {
		t.Errorf("expectedExporterKinds is not sorted: %v", expectedExporterKinds)
	}
	seen := map[string]bool{}
	for _, k := range expectedExporterKinds {
		if seen[k] {
			t.Errorf("kind %q is listed twice", k)
		}
		seen[k] = true
		if !IsExpectedExporterKind(k) {
			t.Errorf("IsExpectedExporterKind(%q) = false for a listed kind", k)
		}
	}
	for _, k := range []string{"nginx", "jvm", "mariadb"} {
		if !IsExpectedExporterKind(k) {
			t.Errorf("%q is not an exporter kind", k)
		}
	}
	// kubernetes is a rule pack with no presence metric: no kind.
	for _, k := range []string{"kubernetes", "MySQL", "", "mariadb "} {
		if IsExpectedExporterKind(k) {
			t.Errorf("%q is admitted as an exporter kind", k)
		}
	}
}

func TestExpectedExportersOf(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name    string
		value   string
		kinds   []string
		problem string // a substring; "" = read
	}{
		{"one kind", "[mariadb]", []string{"mariadb"}, ""},
		{"several, in the order written", "[redis, mariadb, nginx]", []string{"redis", "mariadb", "nginx"}, ""},
		{"block form", "\n  - jvm\n  - nginx", []string{"jvm", "nginx"}, ""},
		{"empty list declares nothing", "[]", nil, ""},
		{"null declares nothing", "null", nil, ""},
		{"empty string declares nothing", `""`, nil, ""},
		{"a scalar is not a list", "mariadb", nil, "is not a list"},
		{"a string that reads like a list is not a list", `"[mariadb]"`, nil, "is not a list"},
		{"a mapping is not a list", "{mariadb: true}", nil, "is not a list"},
		{"a number entry", "[mariadb, 5]", nil, "entry [1] is not a string"},
		{"a boolean entry", "[true]", nil, "entry [0] is not a string"},
		{"a nested list entry", "[[mariadb]]", nil, "entry [0] is not a string"},
		{"a null entry", "[mariadb, null]", nil, "entry [1] is not a string"},
		{"an unknown kind", "[mariadb, MySQL]", nil, "entry [1] is not an exporter kind"},
		{"a rule pack without a presence metric", "[kubernetes]", nil, "entry [0] is not an exporter kind"},
		{"an empty entry", `[""]`, nil, "entry [0] is not an exporter kind"},
		{"a repeat", "[redis, mariadb, redis]", nil, "entry [2] repeats"},
		{"per-cluster form is not read yet", "[mariadb@edge-1]", nil, "per-cluster expectations (`kind@cluster`) are not read yet"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			sv := svOf(t, "_expected_exporters", tc.value)
			kinds, problem := expectedExportersOf(sv)
			if !reflect.DeepEqual(kinds, tc.kinds) {
				t.Errorf("kinds = %v, want %v", kinds, tc.kinds)
			}
			if (tc.problem == "") != (problem == "") || !strings.Contains(problem, tc.problem) {
				t.Errorf("problem = %q, want one containing %q", problem, tc.problem)
			}
			if problem != "" {
				// Nothing unreadable is served, and the sentence names
				// every allowed kind.
				if kinds != nil {
					t.Errorf("an unread value serves %v", kinds)
				}
				for _, k := range ExpectedExporterKinds() {
					if !strings.Contains(problem, k) {
						t.Errorf("problem does not name the allowed kind %q: %q", k, problem)
					}
				}
			}
			// The registered check and the merged-map reader are this reading.
			if got := CheckReservedValue("_expected_exporters", sv); got != problem {
				t.Errorf("CheckReservedValue = %q, the reader's problem is %q", got, problem)
			}
			if got := ExpectedExportersOf(map[string]ScheduledValue{"_expected_exporters": sv}); !reflect.DeepEqual(got, tc.kinds) {
				t.Errorf("ExpectedExportersOf = %v, want %v", got, tc.kinds)
			}
		})
	}
}

// The reading is the declared set: the order does not change the series, a
// different set does, and two unread values are alike only as the same text.
func TestExpectedExportersReadAlike(t *testing.T) {
	t.Parallel()
	sv := func(v string) ScheduledValue { return svOf(t, "_expected_exporters", v) }
	if !readAlike("_expected_exporters", sv("[redis, mariadb]"), sv("[mariadb, redis]")) {
		t.Error("the same kinds in another order are not read alike")
	}
	if readAlike("_expected_exporters", sv("[redis]"), sv("[redis, mariadb]")) {
		t.Error("different kinds are read alike")
	}
	if readAlike("_expected_exporters", sv("[bogus-a]"), sv("[bogus-b]")) {
		t.Error("two different unread values are read alike")
	}
}

// The layers, on /metrics' own load (LoadDir) and through the tenant-api's
// MetadataResolver, which must agree: a root platform file's `tenants:`
// entry, the tenant file over it — the value replaced WHOLE — and a profile
// filling it in when neither writes it.
func TestExpectedExportersLayers(t *testing.T) {
	t.Parallel()
	const profiles = "profiles:\n  web:\n    _expected_exporters: [nginx, jvm]\n"
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  []string
	}{
		{"tenant file", map[string]string{
			"tx.yaml": "tenants:\n  tx:\n    _expected_exporters: [mariadb, redis]\n"}, []string{"mariadb", "redis"}},
		{"platform entry", map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    _expected_exporters: [kafka]\n",
			"tx.yaml":        "tenants:\n  tx: {}\n"}, []string{"kafka"}},
		{"tenant file replaces the platform entry whole, no union", map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    _expected_exporters: [kafka, redis]\n",
			"tx.yaml":        "tenants:\n  tx:\n    _expected_exporters: [mariadb]\n"}, []string{"mariadb"}},
		{"tenant file's empty list over the platform entry declares nothing", map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    _expected_exporters: [kafka]\n",
			"tx.yaml":        "tenants:\n  tx:\n    _expected_exporters: []\n"}, nil},
		{"a later platform file over an earlier one", map[string]string{
			"_a.yaml": "tenants:\n  tx:\n    _expected_exporters: [kafka]\n",
			"_b.yaml": "tenants:\n  tx:\n    _expected_exporters: [oracle]\n",
			"tx.yaml": "tenants:\n  tx: {}\n"}, []string{"oracle"}},
		{"profile fills it in", map[string]string{
			"_profiles.yaml": profiles,
			"tx.yaml":        "tenants:\n  tx:\n    _profile: web\n"}, []string{"nginx", "jvm"}},
		{"tenant file over the profile", map[string]string{
			"_profiles.yaml": profiles,
			"tx.yaml":        "tenants:\n  tx:\n    _profile: web\n    _expected_exporters: [redis]\n"}, []string{"redis"}},
		{"platform entry over the profile", map[string]string{
			"_profiles.yaml": profiles,
			"_platform.yaml": "tenants:\n  tx:\n    _expected_exporters: [db2]\n",
			"tx.yaml":        "tenants:\n  tx:\n    _profile: web\n"}, []string{"db2"}},
		{"an unread value in the winning layer serves nothing, not the layer below", map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    _expected_exporters: [kafka]\n",
			"tx.yaml":        "tenants:\n  tx:\n    _expected_exporters: [mariadb, MySQL]\n"}, nil},
		{"the removed _metadata.db_type declares nothing", map[string]string{
			"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      db_type: [mariadb]\n"}, nil},
		{"not declared", map[string]string{"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: o\n"}, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeMergeTree(t, dir, tc.files)
			cfg, _, err := LoadDir(dir, nil)
			if err != nil {
				t.Fatalf("LoadDir: %v", err)
			}
			var metrics []string
			for _, e := range cfg.ResolveExpectedExporters() {
				if e.Tenant == "tx" {
					metrics = e.Kinds
				}
				if len(e.Kinds) == 0 {
					t.Errorf("ResolveExpectedExporters lists %q with no kind: the opt-in guard", e.Tenant)
				}
			}
			if !reflect.DeepEqual(metrics, tc.want) {
				t.Fatalf("/metrics reads %v, want %v", metrics, tc.want)
			}
			body, err := os.ReadFile(filepath.Join(dir, "tx.yaml"))
			if err != nil {
				t.Fatal(err)
			}
			root, err := LoadRootPlatformChecked(dir)
			if err != nil {
				t.Fatal(err)
			}
			own, err := ParseConfigFile(body)
			if err != nil {
				t.Fatal(err)
			}
			if got := root.MetadataResolver().ExpectedExporters("tx", own.Tenants["tx"]); !reflect.DeepEqual(got, metrics) {
				t.Errorf("MetadataResolver.ExpectedExporters = %v, /metrics = %v", got, metrics)
			}
		})
	}
}

// An `_expected_exporters` the exporter does not read, and a `_metadata`
// that still writes the removed `db_type`, are problems marked EveryLayer in
// each of the three layers — where any other unread value is a notice
// outside the tenant's own file (an unknown `_metadata` field stays one).
func TestLayerValueProblems_EveryLayer(t *testing.T) {
	t.Parallel()
	rep := loadForValues(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\nprofiles:\n" +
			"  pbad:\n    _expected_exporters: [nope]\n" +
			"  pold:\n    _metadata: {db_type: mariadb}\n" +
			"  ptypo:\n    _metadata: {bogus: x}\n" +
			"  pgood:\n    _expected_exporters: [nginx]\n",
		"_platform.yaml": "tenants:\n  ta:\n    _expected_exporters: redis\n  tb:\n    _metadata: {db_type: [redis]}\n" +
			"  tc:\n    _metadata: {bogus: x}\n",
		"ta.yaml": "tenants:\n  ta: {}\n",
		"tb.yaml": "tenants:\n  tb: {}\n",
		"tc.yaml": "tenants:\n  tc: {}\n",
		"td.yaml": "tenants:\n  td:\n    _expected_exporters: [redis, redis]\n",
		"te.yaml": "tenants:\n  te:\n    _metadata: {owner: o, db_type: MySQL}\n",
		"tf.yaml": "tenants:\n  tf:\n    _expected_exporters: [redis, jvm]\n    _metadata: {owner: o}\n",
	})
	type row struct {
		layerValueRow
		EveryLayer bool
	}
	var got []row
	for _, p := range rep.ValueProblems {
		got = append(got, row{layerValueRow{p.File, p.Layer, p.Owner, p.Key}, p.EveryLayer})
	}
	want := []row{
		{layerValueRow{"_defaults.yaml", ValueLayerProfile, "pbad", "_expected_exporters"}, true},
		{layerValueRow{"_defaults.yaml", ValueLayerProfile, "pold", "_metadata"}, true},
		{layerValueRow{"_defaults.yaml", ValueLayerProfile, "ptypo", "_metadata"}, false},
		{layerValueRow{"_platform.yaml", ValueLayerPlatform, "ta", "_expected_exporters"}, true},
		{layerValueRow{"_platform.yaml", ValueLayerPlatform, "tb", "_metadata"}, true},
		{layerValueRow{"_platform.yaml", ValueLayerPlatform, "tc", "_metadata"}, false},
		{layerValueRow{"td.yaml", ValueLayerTenant, "td", "_expected_exporters"}, true},
		{layerValueRow{"te.yaml", ValueLayerTenant, "te", "_metadata"}, true},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("ValueProblems =\n%+v\nwant\n%+v", got, want)
	}
}

// The removed `_metadata.db_type`: the problem IS the migration instruction.
// It names the replacement key; it prints the exact line to write when every
// value is an exporter kind — built from the kind table; otherwise it counts
// the values that are not a kind and lists the allowed ones (never the value
// itself, #3703); and it says that declaring turns the absence alert on.
func TestMetadataCheck_RemovedDBTypeIsTheMigrationInstruction(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name     string
		value    string
		contains []string
		absent   []string
	}{
		{"one kind, the v2.9 scalar form", "{owner: a, db_type: mariadb}",
			[]string{"`_expected_exporters: [mariadb]`", "Delete `db_type`"}, []string{"allowed kinds"}},
		{"a list of kinds, a repeat written once", "{db_type: [redis, mariadb, redis]}",
			[]string{"`_expected_exporters: [redis, mariadb]`"}, []string{"allowed kinds"}},
		{"a free-text v2.9 value is no kind", "{db_type: MySQL}",
			[]string{"1 of its value(s) is not an exporter kind", "allowed kinds: ", "nginx", "`_expected_exporters: [<kind>, …]`"},
			[]string{"MySQL", "[mariadb]"}},
		{"kinds and non-kinds: no line to copy", "{db_type: [mariadb, Postgres, 7]}",
			[]string{"2 of its value(s) is not an exporter kind", "allowed kinds: "}, []string{"Postgres", "[mariadb]"}},
		{"a mapping is no kind", "{db_type: {a: b}}",
			[]string{"1 of its value(s) is not an exporter kind"}, nil},
		{"null declares nothing", "{db_type: null, owner: a}",
			[]string{"It declares nothing here: delete `db_type`"}, []string{"allowed kinds"}},
		{"empty list declares nothing", "{db_type: []}",
			[]string{"It declares nothing here: delete `db_type`"}, nil},
		// The field is read off the mapping as written: a mapping the
		// reader's map does not hold still gets the instruction.
		{"beside a non-string key", "{db_type: [mariadb], 1: x}",
			[]string{"`_expected_exporters: [mariadb]`"}, nil},
		{"beside a field of the wrong shape", "{db_type: [mariadb], tags: {a: b}}",
			[]string{"`_expected_exporters: [mariadb]`"}, []string{"does not decode"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			sv := svOf(t, "_metadata", tc.value)
			got := CheckReservedValue("_metadata", sv)
			for _, s := range append([]string{"`_metadata.db_type` is removed", "`_expected_exporters`", "TenantExporterAbsent"}, tc.contains...) {
				if !strings.Contains(got, s) {
					t.Errorf("problem lacks %q:\n%s", s, got)
				}
			}
			for _, s := range tc.absent {
				if strings.Contains(got, s) {
					t.Errorf("problem carries %q:\n%s", s, got)
				}
			}
			if !valueErrorInEveryLayer("_metadata", sv) {
				t.Error("not an error in every layer")
			}
		})
	}
	// A `_metadata` without the removed field is judged as before, and is no
	// every-layer error.
	for _, v := range []string{"{owner: a}", "{bogus: x}", "null"} {
		sv := svOf(t, "_metadata", v)
		if strings.Contains(CheckReservedValue("_metadata", sv), "db_type` is removed") || valueErrorInEveryLayer("_metadata", sv) {
			t.Errorf("_metadata: %s is judged as the removed field", v)
		}
	}
	// db_type is no field of the reader any more.
	for _, f := range TenantMetadataFields() {
		if f == "db_type" {
			t.Error("TenantMetadata still reads db_type")
		}
	}
}
