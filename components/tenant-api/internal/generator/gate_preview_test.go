package generator

// A preview's judgement (#3016, gitops.WithPreview): not counted among the
// writes' refusals, the base not judged a second time; a key verdict
// without `blocks` cannot be relied on.

import (
	"errors"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/testutil"
)

func TestValidateWrites_APreviewIsNotAWrite(t *testing.T) {
	t.Parallel()
	t.Run("a rejection is not counted", func(t *testing.T) {
		t.Parallel()
		fake := testutil.StartFakeGenerator(t)
		fake.SetValidate(func(int, testutil.FakeValidateRequest) (int, any) {
			return 200, testutil.RejectedVerdict(ReasonBusy)
		})
		g := fastGate(t, fake, "")
		_, err := g.ValidateWrites(gitops.WithPreview(ctxFor(t, 20*time.Second)), testBase, testFiles())
		var rej *RejectedError
		if !errors.As(err, &rej) || rej.Reason != ReasonBusy {
			t.Fatalf("err = %v", err)
		}
		if n := g.Rejections()[ReasonBusy]; n != 0 {
			t.Errorf("a preview's rejection counted: %v", g.Rejections())
		}
		if _, err := g.ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles()); err == nil ||
			g.Rejections()[ReasonBusy] != 1 {
			t.Errorf("a write's rejection: err %v, rejections %v", err, g.Rejections())
		}
	})
	t.Run("findings do not judge the base again", func(t *testing.T) {
		t.Parallel()
		fake := testutil.StartFakeGenerator(t)
		fake.SetValidate(func(_ int, req testutil.FakeValidateRequest) (int, any) {
			return 200, testutil.FindingsVerdict(req, "/w", testutil.Finding("route", "db-a", "db-a.yaml", "wrong"))
		})
		g := fastGate(t, fake, "")
		_, err := g.ValidateWrites(gitops.WithPreview(ctxFor(t, 20*time.Second)), testBase, testFiles())
		var found *FindingsError
		if !errors.As(err, &found) || found.ComparedWithBase {
			t.Fatalf("err = %v", err)
		}
		if n := len(fake.Requests()); n != 1 {
			t.Errorf("requests = %d, want 1 (no base judgement for a preview)", n)
		}
	})
}

func TestValidateWrites_AKeyVerdictWithoutBlocksIsTooOld(t *testing.T) {
	t.Parallel()
	fake := testutil.StartFakeGenerator(t)
	fake.SetValidate(func(_ int, req testutil.FakeValidateRequest) (int, any) {
		v := testutil.PassVerdict(req, nil)
		v["key_verdict"] = map[string]any{"status": "complete", "tenants": 1, "seconds": 0.1, "truncated": 0,
			"findings": []map[string]any{{"severity": "warning", "kind": "disable_noop", "key": "k",
				"file": "db-a.yaml", "tenants": []string{"db-a"}, "reason": nil}}}
		return 200, v
	})
	_, err := fastGate(t, fake, "").ValidateWrites(ctxFor(t, 20*time.Second), testBase, testFiles())
	var unav *UnavailableError
	if !errors.As(err, &unav) || unav.Reason != ReasonGeneratorTooOld {
		t.Fatalf("err = %v, want generator_too_old", err)
	}
}
