package gitops

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
)

func kvFinding(key, file string, tenants ...string) KeyFinding {
	return KeyFinding{Severity: "error", Kind: "unread", Key: key, File: file, Tenants: tenants, Reason: "unregistered"}
}

// kvWarning is kvFinding at warning severity: reported, never refusing.
func kvWarning(key, file string, tenants ...string) KeyFinding {
	f := kvFinding(key, file, tenants...)
	f.Severity = "warning"
	return f
}

// ADR-038 decision 5, stage 2: who is told what of a key verdict.
func TestAdviseKeyVerdict_AttributionAndDisclosure(t *testing.T) {
	t.Parallel()
	files := []ProposedFile{{Path: "team/shared.yaml", Tenants: []string{"db-a"}}}
	kv := &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{
		kvFinding("_own", "team/shared.yaml", "db-a"),        // the written tenant's
		kvFinding("_neighbour", "team/shared.yaml", "db-b"),  // another tenant of the written file
		kvFinding("_platform", "_defaults.yaml", "db-a"),     // a file the write does not change
		kvFinding("_nofile", "", "db-a"),                     // no single file
		kvFinding("_elsewhere", "db-c.yaml", "db-c", "db-a"), // a file the write does not change
	}}
	own := "key verdict: error: key _own in team/shared.yaml is read by no reader (unregistered) — tenant(s) db-a"
	neighbour := "key verdict: error: key _neighbour in team/shared.yaml is read by no reader (unregistered) — tenant(s) db-b"
	for name, tc := range map[string]struct {
		names       TenantNames
		treeVisible bool
		whole       []string
	}{
		// No names set (a path that forgets, or an async task's stored
		// results): the written tenant's own lines only.
		"nothing set": {nil, false, []string{msgVerdictOtherWithheld}},
		"may name db-b": {func(id string) bool { return id == "db-b" }, false,
			[]string{neighbour, msgVerdictOtherWithheld}},
		"whole tree": {func(string) bool { return true }, true, []string{
			neighbour,
			"key verdict: error: key _platform in _defaults.yaml is read by no reader (unregistered) — tenant(s) db-a",
			"key verdict: error: key _nofile is read by no reader (unregistered) — tenant(s) db-a",
			"key verdict: error: key _elsewhere in db-c.yaml is read by no reader (unregistered) — tenant(s) db-c, db-a",
		}},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			ctx := WithTreeVisible(WithTenantNames(context.Background(), tc.names), tc.treeVisible)
			adv := adviseKeyVerdict(ctx, files, kv)
			if !reflect.DeepEqual(adv.byTenant, map[string][]string{"db-a": {own}}) {
				t.Errorf("byTenant = %q", adv.byTenant)
			}
			if !reflect.DeepEqual(adv.whole, tc.whole) {
				t.Errorf("whole = %q\nwant    %q", adv.whole, tc.whole)
			}
		})
	}
}

// "Not judged" is always said, with the reason's text and never the
// generator's message; "nothing found" says nothing; no verdict (a
// validator that has none) says nothing.
func TestAdviseKeyVerdict_NotCompletedTruncatedAndEmpty(t *testing.T) {
	t.Parallel()
	files := []ProposedFile{{Path: "db-a.yaml", Tenants: []string{"db-a"}}}
	ctx := context.Background()
	for name, tc := range map[string]struct {
		kv   *KeyVerdict
		want []string
	}{
		"none":          {nil, nil},
		"nothing found": {&KeyVerdict{Status: KeyVerdictComplete}, nil},
		"memory": {&KeyVerdict{Status: KeyVerdictNotCompleted, Reason: "memory"},
			[]string{"key verdict: not completed (it ran out of memory); the write was not refused — the keys of this write were not judged"}},
		"not reported": {&KeyVerdict{Status: KeyVerdictNotCompleted, Reason: KeyVerdictNotReported},
			[]string{"key verdict: not completed (the generator container did not report one; it predates ADR-038 decision 5); the write was not refused — the keys of this write were not judged"}},
		"a reason this tenant-api does not know": {&KeyVerdict{Status: KeyVerdictNotCompleted, Reason: "newer"},
			[]string{"key verdict: not completed (newer); the write was not refused — the keys of this write were not judged"}},
		// The count covers findings about anyone: a number only for a
		// caller who sees the whole tree (below).
		"truncated": {&KeyVerdict{Status: KeyVerdictComplete, Truncated: 7}, []string{msgVerdictOtherWithheld}},
		"no reason": {&KeyVerdict{Status: KeyVerdictNotCompleted},
			[]string{"key verdict: not completed (no reason given); the write was not refused — the keys of this write were not judged"}},
	} {
		if got := adviseKeyVerdict(ctx, files, tc.kv).lines(); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: lines = %q, want %q", name, got, tc.want)
		}
	}
	whole := WithTreeVisible(ctx, true)
	if got := adviseKeyVerdict(whole, files, &KeyVerdict{Status: KeyVerdictComplete, Truncated: 7}).lines(); !reflect.DeepEqual(got,
		[]string{"key verdict: 7 more finding(s) the generator did not send"}) {
		t.Errorf("truncated, whole tree: lines = %q", got)
	}
	// Every reason the generator sends has its own text.
	for _, r := range []string{"no_time", "deadline", "preempted", "memory", "error", "undecidable", "platform_file"} {
		if verdictReasons[r] == "" {
			t.Errorf("reason %s has no text", r)
		}
	}
}

func TestVerdictAdvice_LinesAreCapped(t *testing.T) {
	t.Parallel()
	// The written tenant's own lines come first: the cut takes the
	// platform's.
	adv := verdictAdvice{byTenant: map[string][]string{"db-a": {"own"}}}
	for i := 0; i < maxVerdictLines+5; i++ {
		adv.whole = append(adv.whole, fmt.Sprint(i))
	}
	got := adv.lines()
	if len(got) != maxVerdictLines+1 || got[0] != "own" || got[maxVerdictLines-1] != fmt.Sprint(maxVerdictLines-2) ||
		got[maxVerdictLines] != "key verdict: 6 more line(s) not shown" {
		t.Fatalf("lines = %d, first %q, last %q", len(got), got[0], got[len(got)-1])
	}
}

// A direct batch: each tenant's lines on its first op that changed
// something, the whole write's once, on the first op that changed
// something.
func TestVerdictAdvice_AdviseBatch(t *testing.T) {
	t.Parallel()
	adv := verdictAdvice{whole: []string{"W"}, byTenant: map[string][]string{"db-b": {"B"}, "db-c": {"C"}}}
	ops := []BatchOp{{TenantID: "db-a"}, {TenantID: "db-b"}, {TenantID: "db-b"}, {TenantID: "db-c"}, {TenantID: "db-b"}}
	outcomes := []BatchOpOutcome{{NoOp: true}, {Notices: []string{"n"}}, {}, {}, {}}
	adv.adviseBatch(ops, outcomes)
	want := [][]string{nil, {"n", "B", "W"}, nil, {"C"}, nil}
	for i, o := range outcomes {
		if !reflect.DeepEqual(o.Notices, want[i]) {
			t.Errorf("op %d notices = %q, want %q", i, o.Notices, want[i])
		}
	}
	// Not judged: said on every op that changed something.
	outcomes = []BatchOpOutcome{{NoOp: true}, {}, {}, {}, {}}
	verdictAdvice{notJudged: "X"}.adviseBatch(ops, outcomes)
	for i, o := range outcomes {
		if want := i > 0; contains(o.Notices, "X") != want {
			t.Errorf("not judged: op %d notices = %q", i, o.Notices)
		}
	}
}

// One RBAC answer per tenant however many findings name it: each Shown is
// an org-read check that counts a deny (DiscloseKeyValidation's rule).
func TestAdviseKeyVerdict_AsksEachTenantOnce(t *testing.T) {
	t.Parallel()
	asked := map[string]int{}
	names := TenantNames(func(id string) bool { asked[id]++; return false })
	kv := &KeyVerdict{Status: KeyVerdictComplete}
	for i := 0; i < 5; i++ {
		kv.Findings = append(kv.Findings, kvFinding(fmt.Sprint("_k", i), "shared.yaml", "db-b"))
	}
	adviseKeyVerdict(WithTenantNames(context.Background(), names),
		[]ProposedFile{{Path: "shared.yaml", Tenants: []string{"db-a"}}}, kv)
	if !reflect.DeepEqual(asked, map[string]int{"db-b": 1}) {
		t.Errorf("asked = %v, want db-b once", asked)
	}
}

// The verdict a validator returns on a pass reaches every direct write's
// notices (a warning: an error refuses, TestKeyVerdict_ErrorRefusesTheWrite);
// a refused write returns none of it.
func TestKeyVerdict_ReachesDirectWritesNotices(t *testing.T) {
	t.Parallel()
	verdict := func(tenant string) *KeyVerdict {
		return &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{kvWarning("_x", tenant+".yaml", tenant)}}
	}
	line := func(tenant string) string {
		return "key verdict: warning: key _x in " + tenant + ".yaml is read by no reader (unregistered) — tenant(s) " + tenant
	}
	body := func(id, v string) string {
		return "tenants:\n  " + id + ":\n    mysql_threads_running: \"" + v + "\"\n"
	}
	merge := func(id, v string) MergeFunc { return func([]byte) (string, error) { return body(id, v), nil } }

	dir := initRepoWithDefaults(t)
	w := newTestWriter(dir, dir)
	v := &recordingValidator{verdict: verdict("db-a")}
	w.SetValidator(v)
	ctx := context.Background()

	notices, err := w.Write(ctx, "db-a", "op@example.com", body("db-a", "70"))
	if err != nil || !contains(notices, line("db-a")) {
		t.Fatalf("Write: notices %q, err %v", notices, err)
	}
	notices, err = w.WriteMerged(ctx, "db-a", "op@example.com", merge("db-a", "71"))
	if err != nil || !contains(notices, line("db-a")) {
		t.Fatalf("WriteMerged: notices %q, err %v", notices, err)
	}
	// A write that changes nothing is not judged: no verdict.
	calls := len(v.calls)
	notices, err = w.WriteMerged(ctx, "db-a", "op@example.com", merge("db-a", "71"))
	if err != nil || contains(notices, line("db-a")) || len(v.calls) != calls {
		t.Fatalf("no-op WriteMerged: notices %q, err %v, asked %d", notices, err, len(v.calls)-calls)
	}
	v.verdict = &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{
		kvWarning("_x", "db-a.yaml", "db-a"), kvWarning("_x", "db-b.yaml", "db-b")}}
	outcomes, err := w.WriteBatch(ctx, []BatchOp{{TenantID: "db-a", Merge: merge("db-a", "72")},
		{TenantID: "db-b", Merge: merge("db-b", "72")}}, "op@example.com")
	if err != nil || !contains(outcomes[0].Notices, line("db-a")) || contains(outcomes[0].Notices, line("db-b")) ||
		!contains(outcomes[1].Notices, line("db-b")) {
		t.Fatalf("WriteBatch: outcomes %+v, err %v", outcomes, err)
	}

	v.err = errFakeRefusal
	if notices, err := w.Write(ctx, "db-a", "op@example.com", body("db-a", "73")); !errors.Is(err, errFakeRefusal) || notices != nil {
		t.Fatalf("refused Write: notices %q, err %v", notices, err)
	}
	if outcomes, err := w.WriteBatch(ctx, []BatchOp{{TenantID: "db-a", Merge: merge("db-a", "73")}}, "op@example.com"); !errors.Is(err, errFakeRefusal) || outcomes != nil {
		t.Fatalf("refused WriteBatch: outcomes %+v, err %v", outcomes, err)
	}
}

// PR mode: the single write's and the batch's one notice list carry it.
func TestKeyVerdict_ReachesPRWritesNotices(t *testing.T) {
	t.Parallel()
	want := "key verdict: warning: key _x in db-a.yaml is read by no reader (unregistered) — tenant(s) db-a"
	dir := newNameDefaultsDir(t)
	initMainBranchRepo(t, dir)
	addBareRemote(t, dir)
	w := newTestWriter(dir, dir)
	w.SetValidator(&recordingValidator{verdict: &KeyVerdict{Status: KeyVerdictComplete,
		Findings: []KeyFinding{kvWarning("_x", "db-a.yaml", "db-a")}}})
	res, err := w.WritePR(context.Background(), "db-a", "op@example.com", "tenants:\n  db-a:\n    mysql_threads_running: \"70\"\n")
	if err != nil || !contains(res.Notices, want) {
		t.Fatalf("WritePR: %+v, %v", res, err)
	}
	res, err = w.WritePRBatch(context.Background(), []PRBatchOp{{TenantID: "db-a", Merge: func([]byte) (string, error) {
		return "tenants:\n  db-a:\n    mysql_threads_running: \"71\"\n", nil
	}}}, "op@example.com")
	if err != nil || !contains(res.Notices, want) {
		t.Fatalf("WritePRBatch: %+v, %v", res, err)
	}
}

// ADR-038 decision 5: an error finding about a tenant the write is for
// refuses the write (ErrValidation, 400) and nothing is written; a warning,
// or an error about a tenant the write is not for, does not.
func TestKeyVerdict_ErrorRefusesTheWrite(t *testing.T) {
	t.Parallel()
	body := func(id, v string) string {
		return "tenants:\n  " + id + ":\n    mysql_threads_running: \"" + v + "\"\n"
	}
	merge := func(id, v string) MergeFunc { return func([]byte) (string, error) { return body(id, v), nil } }
	line := "key verdict: error: key _x in db-a.yaml is read by no reader (unregistered) — tenant(s) db-a"
	dir := initRepoWithDefaults(t)
	w := newTestWriter(dir, dir)
	v := &recordingValidator{verdict: &KeyVerdict{Status: KeyVerdictComplete,
		Findings: []KeyFinding{kvFinding("_x", "db-a.yaml", "db-a")}}}
	w.SetValidator(v)
	ctx := context.Background()
	head := func() string { return strings.TrimSpace(gitOut(t, dir, "rev-parse", "HEAD")) }

	before := head()
	notices, err := w.Write(ctx, "db-a", "op@example.com", body("db-a", "70"))
	var kve *KeyVerdictError
	if !errors.Is(err, ErrValidation) || !errors.As(err, &kve) || !strings.Contains(err.Error(), line) || notices != nil {
		t.Fatalf("Write: notices %q, err %v", notices, err)
	}
	if head() != before {
		t.Fatal("a refused write was committed")
	}

	// A batch: the op of the refused tenant carries it; nothing is written.
	v.verdict = &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{kvFinding("_x", "db-a.yaml", "db-a")}}
	outcomes, err := w.WriteBatch(ctx, []BatchOp{{TenantID: "db-b", Merge: merge("db-b", "72")},
		{TenantID: "db-a", Merge: merge("db-a", "72")}}, "op@example.com")
	var rej *BatchRejectedError
	if !errors.As(err, &rej) || outcomes != nil {
		t.Fatalf("WriteBatch: outcomes %+v, err %v", outcomes, err)
	}
	if rej.Outcomes[0].Err != nil || !errors.As(rej.Outcomes[1].Err, &kve) || !strings.Contains(rej.Outcomes[1].Err.Error(), line) {
		t.Fatalf("WriteBatch outcomes: %+v", rej.Outcomes)
	}
	if head() != before {
		t.Fatal("a refused batch was committed")
	}

	// Not refused: a warning, and an error about a tenant this write is not
	// for (its file is not written: the platform's to report).
	n := 80
	for name, f := range map[string]KeyFinding{
		"warning":            kvWarning("_x", "db-a.yaml", "db-a"),
		"another tenant's":   kvFinding("_x", "db-b.yaml", "db-b"),
		"another in my file": kvFinding("_x", "db-a.yaml", "db-z"),
	} {
		v.verdict = &KeyVerdict{Status: KeyVerdictComplete, Findings: []KeyFinding{f}}
		n++
		if _, err := w.Write(ctx, "db-a", "op@example.com", body("db-a", fmt.Sprint(n))); err != nil {
			t.Errorf("%s: Write refused: %v", name, err)
		}
	}
}

// PR mode refuses alike, the single write and the batch.
func TestKeyVerdict_ErrorRefusesPRWrites(t *testing.T) {
	t.Parallel()
	dir := newNameDefaultsDir(t)
	initMainBranchRepo(t, dir)
	addBareRemote(t, dir)
	w := newTestWriter(dir, dir)
	w.SetValidator(&recordingValidator{verdict: &KeyVerdict{Status: KeyVerdictComplete,
		Findings: []KeyFinding{kvFinding("_x", "db-a.yaml", "db-a")}}})
	if _, err := w.WritePR(context.Background(), "db-a", "op@example.com", "tenants:\n  db-a:\n    mysql_threads_running: \"70\"\n"); !errors.Is(err, ErrValidation) {
		t.Fatalf("WritePR: %v", err)
	}
	_, err := w.WritePRBatch(context.Background(), []PRBatchOp{{TenantID: "db-a", Merge: func([]byte) (string, error) {
		return "tenants:\n  db-a:\n    mysql_threads_running: \"71\"\n", nil
	}}}, "op@example.com")
	var rej *BatchRejectedError
	if !errors.As(err, &rej) || !errors.Is(err, ErrValidation) {
		t.Fatalf("WritePRBatch: %v", err)
	}
}
