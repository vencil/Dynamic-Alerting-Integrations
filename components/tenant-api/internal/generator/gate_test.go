package generator

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/testutil"
)

var testBase = strings.Repeat("ab", 20)

func testFiles() []gitops.ProposedFile {
	return []gitops.ProposedFile{
		{Path: "db-a.yaml", Content: []byte("tenants:\n  db-a: {}\n"), Tenants: []string{"db-a"}},
		{Path: "team/shared.yaml", Content: []byte("tenants:\n  db-b: {}\n"), Tenants: []string{"db-b"}},
	}
}

// fastGate is a gate whose minimum judging time is small, so the deadline
// cases run in milliseconds.
func fastGate(t *testing.T, fake *testutil.FakeGenerator, policyPath string) *Gate {
	t.Helper()
	g := NewGate(NewClient(fake.Socket), policyPath)
	g.minJudge = 200 * time.Millisecond
	return g
}

func ctxFor(t *testing.T, d time.Duration) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), d)
	t.Cleanup(cancel)
	return ctx
}

func TestValidateWrites_PassSendsBaseWritesAndRemainingTime(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	g := fastGate(t, fake, "")
	if err := errOf(g.ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles())); err != nil {
		t.Fatalf("pass: %v", err)
	}
	reqs := fake.Requests()
	if len(reqs) != 1 {
		t.Fatalf("requests = %d, want 1", len(reqs))
	}
	r := reqs[0]
	if r.BaseCommit != testBase || len(r.Writes) != 2 || r.Writes[1].Path != "team/shared.yaml" ||
		string(r.Writes[0].Content) != "tenants:\n  db-a: {}\n" {
		t.Errorf("request = %+v", r)
	}
	// deadline_ms is what is left of the ctx: under 20 s, well over 19 s.
	if r.DeadlineMS > 20000 || r.DeadlineMS < 19000 {
		t.Errorf("deadline_ms = %d, want the ~20 s left", r.DeadlineMS)
	}
}

// ADR-036 §4 "驗的和寫的必須是同一份": a pass naming other content is judged
// once more; the second answer decides.
func TestValidateWrites_MismatchJudgedAgainOnce(t *testing.T) {
	t.Parallel()
	for name, wrong := range map[string]func(map[string]any){
		"base": func(v map[string]any) { v["validated"].(map[string]any)["base_commit"] = strings.Repeat("cd", 20) },
		"content": func(v map[string]any) {
			v["validated"].(map[string]any)["files"].([]map[string]any)[0]["sha256"] = "00"
		},
		// The right bytes, judged as a deleted file: a write is never a
		// deletion, so the verdict is on another tree.
		"deleted": func(v map[string]any) {
			v["validated"].(map[string]any)["files"].([]map[string]any)[0]["deleted"] = true
		},
		"file count":   func(v map[string]any) { v["validated"].(map[string]any)["files"] = []map[string]any{} },
		"policy":       func(v map[string]any) { v["validated"].(map[string]any)["policy_sha256"] = "11" },
		"no validated": func(v map[string]any) { v["validated"] = nil },
	} {
		t.Run(name+" then right", func(t *testing.T) {
			t.Parallel()
			fake := testutil.StartFakeGenerator(t)
			fake.SetValidate(func(n int, req testutil.FakeValidateRequest) (int, any) {
				v := testutil.PassVerdict(req, nil)
				if n == 1 {
					wrong(v)
				}
				return 200, v
			})
			g := fastGate(t, fake, "")
			if err := errOf(g.ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles())); err != nil {
				t.Fatalf("second answer right: %v", err)
			}
			if n := len(fake.Requests()); n != 2 {
				t.Errorf("requests = %d, want 2", n)
			}
		})
		t.Run(name+" twice", func(t *testing.T) {
			t.Parallel()
			fake := testutil.StartFakeGenerator(t)
			fake.SetValidate(func(_ int, req testutil.FakeValidateRequest) (int, any) {
				v := testutil.PassVerdict(req, nil)
				wrong(v)
				return 200, v
			})
			g := fastGate(t, fake, "")
			err := errOf(g.ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles()))
			var rej *RejectedError
			if !errors.As(err, &rej) || rej.Reason != ReasonMismatch || !Retryable(rej.Reason) {
				t.Fatalf("err = %v, want a retryable mismatch rejection", err)
			}
			if n := len(fake.Requests()); n != 2 {
				t.Errorf("requests = %d, want 2 (one re-judgement, no more)", n)
			}
			if g.Rejections()[ReasonMismatch] != 1 {
				t.Errorf("rejections = %v", g.Rejections())
			}
		})
	}
}

// Too little time left to judge again: a retryable refusal, not a second
// request and not a 503.
func TestValidateWrites_NoTimeToJudgeAgain(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	fake.SetValidate(func(_ int, req testutil.FakeValidateRequest) (int, any) {
		time.Sleep(300 * time.Millisecond)
		v := testutil.PassVerdict(req, nil)
		v["validated"].(map[string]any)["base_commit"] = strings.Repeat("cd", 20)
		return 200, v
	})
	g := fastGate(t, fake, "")
	err := errOf(g.ValidateWrites(ctxFor(t, 450*time.Millisecond), testBase, testFiles()))
	var rej *RejectedError
	if !errors.As(err, &rej) || rej.Reason != ReasonNoTime {
		t.Fatalf("err = %v, want no_time", err)
	}
	if n := len(fake.Requests()); n != 1 {
		t.Errorf("requests = %d, want 1", n)
	}
}

func TestValidateWrites_NoTimeAtAll(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	g := fastGate(t, fake, "")
	err := errOf(g.ValidateWrites(ctxFor(t, 100*time.Millisecond), testBase, testFiles()))
	var rej *RejectedError
	if !errors.As(err, &rej) || rej.Reason != ReasonNoTime || !Retryable(ReasonNoTime) {
		t.Fatalf("err = %v, want no_time", err)
	}
	if n := len(fake.Requests()); n != 0 {
		t.Errorf("requests = %d, want none", n)
	}
}

func TestValidateWrites_DeploymentPolicyHash(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	policy := filepath.Join(dir, "policy.yaml")
	if err := os.WriteFile(policy, []byte("allowed_domains: [example.com]\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("allowed_domains: [example.com]\n"))
	right := hex.EncodeToString(sum[:])
	other := strings.Repeat("0", 64)
	for _, tc := range []struct {
		name    string
		path    string
		answer  *string
		wantErr bool
	}{
		{"same file", policy, &right, false},
		{"other file", policy, &other, true},
		{"generator has none", policy, nil, true},
		{"tenant-api has none", "", &right, true},
		{"neither", "", nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			fake := testutil.StartFakeGenerator(t)
			fake.SetValidate(func(_ int, req testutil.FakeValidateRequest) (int, any) {
				return 200, testutil.PassVerdict(req, tc.answer)
			})
			err := errOf(fastGate(t, fake, tc.path).ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles()))
			var rej *RejectedError
			if tc.wantErr != (err != nil) || (err != nil && (!errors.As(err, &rej) || rej.Reason != ReasonMismatch)) {
				t.Errorf("err = %v, want mismatch: %v", err, tc.wantErr)
			}
		})
	}
	t.Run("unreadable copy", func(t *testing.T) {
		t.Parallel()
		fake := testutil.StartFakeGenerator(t)
		err := errOf(fastGate(t, fake, filepath.Join(dir, "missing.yaml")).ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles()))
		var un *UnavailableError
		if !errors.As(err, &un) || un.Code != CodePolicyUnavailable || un.Reason != ReasonPolicyUnreadable {
			t.Fatalf("err = %v, want POLICY_UNAVAILABLE policy_unreadable", err)
		}
		if n := len(fake.Requests()); n != 0 {
			t.Errorf("requests = %d, want none", n)
		}
	})
}

// ADR-036 §4: related findings first, and the ones the base also reports
// marked pre-existing (judged on the base without the write).
func TestValidateWrites_FindingsOrderedAndCompared(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	fake.SetValidate(func(n int, req testutil.FakeValidateRequest) (int, any) {
		if n == 1 {
			return 200, testutil.FindingsVerdict(req, "/work/job-1/conf.d",
				testutil.Finding("policy_violation", "db-z", "db-z.yaml", "/work/job-1/conf.d/db-z.yaml: old problem"),
				testutil.Finding("receiver_invalid", "", "team/shared.yaml", "shared file problem"),
				testutil.Finding("receiver_invalid", "db-a", "", "db-a problem"),
			)
		}
		if len(req.Writes) != 0 || req.BaseCommit != testBase {
			return 400, map[string]any{"error": "BAD_REQUEST", "message": "base judgement carries writes"}
		}
		return 200, testutil.FindingsVerdict(req, "/work/job-2/conf.d",
			testutil.Finding("policy_violation", "db-z", "db-z.yaml", "/work/job-2/conf.d/db-z.yaml: old problem"))
	})
	err := errOf(fastGate(t, fake, "").ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles()))
	var fe *FindingsError
	if !errors.As(err, &fe) {
		t.Fatalf("err = %v, want *FindingsError", err)
	}
	if !fe.ComparedWithBase || fe.RC != 1 || len(fe.Findings) != 3 {
		t.Fatalf("findings error = %+v", fe)
	}
	want := []struct {
		msg      string
		related  bool
		existing bool
	}{
		{"shared file problem", true, false},
		{"db-a problem", true, false},
		{"/work/job-1/conf.d/db-z.yaml: old problem", false, true},
	}
	for i, w := range want {
		f := fe.Findings[i]
		if f.Message != w.msg || f.Related != w.related || f.PreExisting == nil || *f.PreExisting != w.existing {
			t.Errorf("finding %d = %+v (pre_existing %v), want %+v", i, f, f.PreExisting, w)
		}
	}
}

// No time left for the base judgement: no pre_existing marks, and the
// error says it was not compared.
func TestValidateWrites_FindingsNotCompared(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	fake.SetValidate(func(n int, req testutil.FakeValidateRequest) (int, any) {
		time.Sleep(300 * time.Millisecond)
		return 200, testutil.FindingsVerdict(req, "/w", testutil.Finding("x", "db-a", "", "m"))
	})
	err := errOf(fastGate(t, fake, "").ValidateWrites(ctxFor(t, 450*time.Millisecond), testBase, testFiles()))
	var fe *FindingsError
	if !errors.As(err, &fe) || fe.ComparedWithBase || fe.Findings[0].PreExisting != nil || !fe.Findings[0].Related {
		t.Fatalf("err = %v (%+v)", err, fe)
	}
	if n := len(fake.Requests()); n != 1 {
		t.Errorf("requests = %d, want 1", n)
	}
}

// A base judgement that passes: compared, nothing pre-existing.
func TestValidateWrites_FindingsBasePasses(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	fake.SetValidate(func(n int, req testutil.FakeValidateRequest) (int, any) {
		if n == 1 {
			return 200, testutil.FindingsVerdict(req, "/w", testutil.Finding("x", "db-q", "", "m"))
		}
		return 200, testutil.PassVerdict(req, nil)
	})
	err := errOf(fastGate(t, fake, "").ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles()))
	var fe *FindingsError
	if !errors.As(err, &fe) || !fe.ComparedWithBase || fe.Findings[0].PreExisting == nil || *fe.Findings[0].PreExisting || fe.Findings[0].Related {
		t.Fatalf("err = %v (%+v)", err, fe)
	}
}

func TestValidateWrites_RejectedReasons(t *testing.T) {
	t.Parallel()
	for reason, retry := range map[string]bool{
		ReasonBusy: true, ReasonDeadline: true, ReasonGitRead: true,
		ReasonMemory: false, ReasonIncomplete: false,
	} {
		t.Run(reason, func(t *testing.T) {
			t.Parallel()
			fake := testutil.StartFakeGenerator(t)
			fake.SetValidate(func(int, testutil.FakeValidateRequest) (int, any) { return 200, testutil.RejectedVerdict(reason) })
			g := fastGate(t, fake, "")
			err := errOf(g.ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles()))
			var rej *RejectedError
			if !errors.As(err, &rej) || rej.Reason != reason || Retryable(reason) != retry {
				t.Fatalf("err = %v", err)
			}
			if g.Rejections()[reason] != 1 {
				t.Errorf("rejections = %v", g.Rejections())
			}
		})
	}
}

func TestValidateWrites_HTTPStatuses(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name   string
		status int
		body   any
		check  func(error) bool
	}{
		{"503 validator", 503, map[string]any{"error": "VALIDATOR_UNAVAILABLE", "reason": "amtool_missing", "message": "no amtool"},
			func(err error) bool {
				var u *UnavailableError
				return errors.As(err, &u) && u.Code == CodeValidatorUnavailable && u.Reason == "amtool_missing" && u.Message == "no amtool"
			}},
		{"503 policy", 503, map[string]any{"error": "POLICY_UNAVAILABLE", "reason": "policy_unusable", "message": "bad"},
			func(err error) bool {
				var u *UnavailableError
				return errors.As(err, &u) && u.Code == CodePolicyUnavailable && u.Reason == "policy_unusable"
			}},
		{"503 other code", 503, map[string]any{"error": "WHATEVER"},
			func(err error) bool {
				var u *UnavailableError
				return errors.As(err, &u) && u.Code == CodeValidatorUnavailable && u.Reason == ReasonUnknown
			}},
		{"413", 413, map[string]any{"error": "PAYLOAD_TOO_LARGE", "message": "big"},
			func(err error) bool {
				var r *RejectedError
				return errors.As(err, &r) && r.Reason == ReasonTooLarge && !Retryable(r.Reason)
			}},
		{"400", 400, map[string]any{"error": "BAD_REQUEST", "message": "parent is a file"},
			func(err error) bool {
				var r *RejectedError
				return errors.As(err, &r) && r.Reason == ReasonInvalidRequest && r.Message == "parent is a file"
			}},
		{"500", 500, map[string]any{},
			func(err error) bool {
				var u *UnavailableError
				return errors.As(err, &u) && u.Reason == ReasonBadResponse
			}},
		{"200 not json", 200, "not an object",
			func(err error) bool {
				var u *UnavailableError
				return errors.As(err, &u) && u.Reason == ReasonBadResponse
			}},
		{"200 unknown result", 200, map[string]any{"result": "maybe"},
			func(err error) bool {
				var u *UnavailableError
				return errors.As(err, &u) && u.Reason == ReasonBadResponse
			}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			fake := testutil.StartFakeGenerator(t)
			fake.SetValidate(func(int, testutil.FakeValidateRequest) (int, any) { return tc.status, tc.body })
			if err := errOf(fastGate(t, fake, "").ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles())); !tc.check(err) {
				t.Errorf("err = %v", err)
			}
		})
	}
}

func TestValidateWrites_Unreachable(t *testing.T) {
	t.Parallel()
	g := NewGate(NewClient(filepath.Join(t.TempDir(), "none.sock")), "")
	err := errOf(g.ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles()))
	var u *UnavailableError
	if !errors.As(err, &u) || u.Code != CodeValidatorUnavailable || u.Reason != ReasonUnreachable {
		t.Fatalf("err = %v, want VALIDATOR_UNAVAILABLE unreachable", err)
	}
}

// The service answers within the time it was given, or it is wedged: no
// answer before the request's deadline is VALIDATOR_UNAVAILABLE (ADR-036
// §3), not a rejection of this write.
func TestValidateWrites_NoAnswerBeforeDeadline(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	fake.SetValidate(func(int, testutil.FakeValidateRequest) (int, any) {
		time.Sleep(time.Second)
		return 200, map[string]any{}
	})
	err := errOf(fastGate(t, fake, "").ValidateWrites(ctxFor(t, 400*time.Millisecond), testBase, testFiles()))
	var u *UnavailableError
	if !errors.As(err, &u) || u.Reason != ReasonUnreachable {
		t.Fatalf("err = %v, want VALIDATOR_UNAVAILABLE unreachable", err)
	}
}

// A cancelled request (the client went away) is the ctx's error, as every
// write path returns it.
func TestValidateWrites_Cancelled(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	fake.SetValidate(func(int, testutil.FakeValidateRequest) (int, any) {
		cancel()
		time.Sleep(200 * time.Millisecond)
		return 200, map[string]any{}
	})
	err := errOf(fastGate(t, fake, "").ValidateWrites(ctx, testBase, testFiles()))
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
}

func TestValidateWrites_NoDeadlineUsesFallback(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	if err := errOf(fastGate(t, fake, "").ValidateWrites(context.Background(), testBase, testFiles())); err != nil {
		t.Fatal(err)
	}
	if ms := fake.Requests()[0].DeadlineMS; ms > FallbackDeadline.Milliseconds() || ms < FallbackDeadline.Milliseconds()-1000 {
		t.Errorf("deadline_ms = %d, want ~%v", ms, FallbackDeadline)
	}
}

// ADR-036 §4: git would commit other bytes than the ones judged (the
// Writer's staged-blob check) — a non-retryable rejection naming the files,
// counted under normalized.
func TestRefuseNormalized(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	g := fastGate(t, fake, "")
	err := g.RefuseNormalized([]string{"team/a.yaml", "b.yaml"})
	var rej *RejectedError
	if !errors.As(err, &rej) || rej.Reason != ReasonNormalized || Retryable(rej.Reason) ||
		!strings.Contains(rej.Message, "team/a.yaml, b.yaml") {
		t.Fatalf("err = %v", err)
	}
	if n := g.Rejections()[ReasonNormalized]; n != 1 {
		t.Errorf("rejections[normalized] = %d, want 1", n)
	}
	if len(fake.Requests()) != 0 {
		t.Error("refusing asked the generator")
	}
}

// errOf is a ValidateWrites answer's error.
func errOf(_ *gitops.KeyVerdict, err error) error { return err }

// ADR-038 decision 5: a pass's key verdict reaches the Writer — only the
// matching pass's: a first pass that judged other content judged another
// tree, and its findings are not this write's.
func TestValidateWrites_KeyVerdictOfTheMatchingPassOnly(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	fake.SetValidate(func(n int, req testutil.FakeValidateRequest) (int, any) {
		v := testutil.PassVerdict(req, nil)
		key := "_first_pass_key"
		if n == 1 {
			v["validated"].(map[string]any)["base_commit"] = strings.Repeat("cd", 20)
		} else {
			key = "_second_pass_key"
		}
		v["key_verdict"] = map[string]any{"status": "complete", "tenants": 1, "seconds": 0.1, "truncated": 3,
			"findings": []map[string]any{{"severity": "error", "kind": "unread", "key": key,
				"file": "db-a.yaml", "tenants": []string{"db-a"}, "reason": "unregistered"},
				{"severity": "notice", "kind": "unread", "key": "_p", "file": nil, "tenants": []string{}, "reason": nil}}}
		return 200, v
	})
	kv, err := fastGate(t, fake, "").ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles())
	if err != nil {
		t.Fatalf("pass: %v", err)
	}
	want := &gitops.KeyVerdict{Status: gitops.KeyVerdictComplete, Truncated: 3, Findings: []gitops.KeyFinding{
		{Severity: "error", Kind: "unread", Key: "_second_pass_key", File: "db-a.yaml", Tenants: []string{"db-a"}, Reason: "unregistered"},
		{Severity: "notice", Kind: "unread", Key: "_p", Tenants: []string{}},
	}}
	if !reflect.DeepEqual(kv, want) {
		t.Fatalf("key verdict = %+v, want %+v", kv, want)
	}
}

// A verdict that did not complete keeps its reason, never the service's
// message (it can carry a platform path or stderr); a pass without one
// (an older service) is "not reported", never an empty complete verdict.
func TestValidateWrites_KeyVerdictNotCompletedOrNotReported(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		kv   any
		want *gitops.KeyVerdict
	}{
		"not completed": {map[string]any{"status": "not_completed", "reason": "memory", "tenants": 4,
			"seconds": 1.5, "message": "da-guard: /srv/conf.d/_defaults.yaml killed"},
			&gitops.KeyVerdict{Status: gitops.KeyVerdictNotCompleted, Reason: "memory"}},
		"absent": {nil, &gitops.KeyVerdict{Status: gitops.KeyVerdictNotCompleted, Reason: gitops.KeyVerdictNotReported}},
		// A status this tenant-api does not know is no complete verdict.
		"unknown status": {map[string]any{"status": "partial", "reason": "x"},
			&gitops.KeyVerdict{Status: gitops.KeyVerdictNotCompleted, Reason: "x"}},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			fake := testutil.StartFakeGenerator(t)
			fake.SetValidate(func(_ int, req testutil.FakeValidateRequest) (int, any) {
				v := testutil.PassVerdict(req, nil)
				if tc.kv == nil {
					delete(v, "key_verdict")
				} else {
					v["key_verdict"] = tc.kv
				}
				return 200, v
			})
			kv, err := fastGate(t, fake, "").ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles())
			if err != nil {
				t.Fatalf("pass: %v", err)
			}
			if !reflect.DeepEqual(kv, tc.want) {
				t.Fatalf("key verdict = %+v, want %+v", kv, tc.want)
			}
		})
	}
}
