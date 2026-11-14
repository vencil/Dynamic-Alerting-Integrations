package main

// served_schedules_only_test.go — served-values --schedules-only (#3877):
// the whole UTC day and nothing read at --at alone, so the time of day of
// --at (default: the run's clock) cannot change the outcome. With
// --schedules, an --at inside a part of the day /metrics cannot be gathered
// in exits 2 although that part is already a segment with its error; a
// reader of the day alone (validate_config's tier inversion) then turned to
// "not checked" whenever it ran in that part of the day.

import (
	"encoding/json"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// windowCollisionTree is #3877's tree: a 01:00–03:00 `N:critical` window
// whose row and the `_critical` key's give one series, so /metrics cannot be
// gathered in that part of the day and can in every other.
var windowCollisionTree = map[string]string{
	"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
	"tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"95\"\n    mysql_connections:\n" +
		"      default: \"70\"\n      overrides:\n        - window: \"01:00-03:00\"\n          value: \"50:critical\"\n",
}

// writeConfD writes files under a fresh conf.d/ and returns its path.
func writeConfD(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "conf.d")
	tree := make(map[string]string, len(files))
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, filepath.Dir(dir), tree)
	return dir
}

// TestServedSchedulesOnly_TimeOfDayDoesNotChangeTheDocument: --at inside
// the part of the day that cannot be gathered and outside it give one exit
// code (0) and, but for `at`, one document, whose tenants carry their
// schedules only — the same schedules --schedules writes at a gatherable
// --at — with the colliding part a segment carrying the Gather error.
// --schedules keeps its contract: exit 2 at the colliding --at.
func TestServedSchedulesOnly_TimeOfDayDoesNotChangeTheDocument(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		files              map[string]string
		inside, outside    string
		errFrom, errTo     string
		tenant, keyChecked string
	}{
		"issue-3877":  {windowCollisionTree, "2026-11-14T01:30:00Z", "2026-11-14T05:00:00Z", "01:00", "03:00", "tx", "mysql_connections"},
		"two-tenants": {ungatherableTree, "2026-07-01T15:30:00Z", scheduleAt, "15:00", "16:00", "tenant-b", "mysql_connections"},
		// Time-boxed keys before and past their `expires:` (their expires /
		// expired come from the expiry reading, not the reading at --at), and
		// three colliding parts with errors of their own: segments unchecked.
		"time-boxed": {collidingTree, "2026-07-01T16:20:00Z", scheduleAt, "", "", "", ""},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := writeConfD(t, tc.files)
			docs := map[string]map[string]json.RawMessage{}
			for _, at := range []string{tc.inside, tc.outside} {
				code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", at, "--schedules-only")
				mustOK(t, code, stderr)
				var doc map[string]json.RawMessage
				if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
					t.Fatal(err)
				}
				delete(doc, "at")
				docs[at] = doc
			}
			if !reflect.DeepEqual(docs[tc.inside], docs[tc.outside]) {
				t.Fatalf("--at %s and --at %s give different documents:\n%s\n%s", tc.inside, tc.outside, docs[tc.inside], docs[tc.outside])
			}

			var only struct {
				Tenants map[string]map[string]json.RawMessage `json:"tenants"`
			}
			// --schedules keeps its contract: exit 2 at the colliding --at.
			code, _, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", tc.inside, "--schedules")
			if code != exitCallerErr || !strings.Contains(stderr, "HTTP 500") {
				t.Errorf("--schedules at %s: exit %d, stderr %q, want exit %d", tc.inside, code, stderr, exitCallerErr)
			}

			raw, _ := json.Marshal(docs[tc.inside])
			if err := json.Unmarshal(raw, &only); err != nil {
				t.Fatal(err)
			}
			for tenant, fields := range only.Tenants {
				if len(fields) != 1 || fields["schedules"] == nil {
					t.Errorf("tenant %s carries %v, want schedules only", tenant, keysOf(fields))
				}
			}

			// The same schedules --schedules writes where it can write at all.
			code, full, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", tc.outside, "--schedules")
			mustOK(t, code, stderr)
			var withValues struct {
				Tenants map[string]struct {
					Schedules json.RawMessage `json:"schedules"`
				} `json:"tenants"`
			}
			if err := json.Unmarshal([]byte(full), &withValues); err != nil {
				t.Fatal(err)
			}
			if len(withValues.Tenants) != len(only.Tenants) {
				t.Fatalf("--schedules has tenants %v, --schedules-only %v", keysOf(withValues.Tenants), keysOf(only.Tenants))
			}
			for tenant, tv := range withValues.Tenants {
				var a, b any
				_ = json.Unmarshal(tv.Schedules, &a)
				_ = json.Unmarshal(only.Tenants[tenant]["schedules"], &b)
				if !reflect.DeepEqual(a, b) {
					t.Errorf("tenant %s: --schedules-only schedules differ from --schedules':\n%s\n%s", tenant, only.Tenants[tenant]["schedules"], tv.Schedules)
				}
			}

			if tc.tenant == "" {
				return
			}
			var day struct {
				Segments []scheduleSegment `json:"segments"`
			}
			var sch map[string]json.RawMessage
			_ = json.Unmarshal(only.Tenants[tc.tenant]["schedules"], &sch)
			if err := json.Unmarshal(sch[tc.keyChecked], &day); err != nil {
				t.Fatalf("tenant %s: schedules[%q]: %v", tc.tenant, tc.keyChecked, err)
			}
			errSegs := 0
			for _, s := range day.Segments {
				if s.Error == "" {
					continue
				}
				errSegs++
				if s.From != tc.errFrom || s.To != tc.errTo || !strings.Contains(s.Error, "HTTP 500") {
					t.Errorf("tenant %s %s: error segment %+v, want %s-%s carrying the Gather failure", tc.tenant, tc.keyChecked, s, tc.errFrom, tc.errTo)
				}
			}
			if errSegs != 1 {
				t.Errorf("tenant %s %s: %d error segments, want 1: %+v", tc.tenant, tc.keyChecked, errSegs, day.Segments)
			}

		})
	}
}

// TestServedSchedulesOnly_Subset: --tenant applies as with --schedules —
// tenants_requested / tenants_absent, schedules only — at an --at that
// cannot be gathered.
func TestServedSchedulesOnly_Subset(t *testing.T) {
	t.Parallel()
	dir := writeConfD(t, ungatherableTree)
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", "2026-07-01T15:30:00Z",
		"--schedules-only", "--tenant", "tenant-a", "--tenant", "nope")
	mustOK(t, code, stderr)
	var doc struct {
		Tenants   map[string]map[string]json.RawMessage `json:"tenants"`
		Requested []string                              `json:"tenants_requested"`
		Absent    []string                              `json:"tenants_absent"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(keysOf(doc.Tenants), []string{"tenant-a"}) || !reflect.DeepEqual(doc.Requested, []string{"nope", "tenant-a"}) ||
		!reflect.DeepEqual(doc.Absent, []string{"nope"}) {
		t.Errorf("tenants %v, requested %v, absent %v", keysOf(doc.Tenants), doc.Requested, doc.Absent)
	}
	if fields := doc.Tenants["tenant-a"]; len(fields) != 1 || fields["schedules"] == nil {
		t.Errorf("tenant-a carries %v, want schedules only", keysOf(fields))
	}
}

// TestServedSchedulesOnly_RefusesDisableEffects: keys[].disable has no keys
// to go in.
func TestServedSchedulesOnly_RefusesDisableEffects(t *testing.T) {
	t.Parallel()
	dir := writeConfD(t, windowCollisionTree)
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--schedules-only", "--disable-effects")
	if code != exitCallerErr || stdout != "" || !strings.Contains(stderr, "--disable-effects") {
		t.Errorf("exit %d, stdout %q, stderr %q; want exit %d, nothing written", code, stdout, stderr, exitCallerErr)
	}
}
