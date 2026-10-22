package gitops

// #3227: the write gate judges `_silent_mode` with the exporter's reader;
// a value the file already holds, read alike, is a notice, never a refusal
// (ratchetValues). Unknown base ⇒ strict.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

const purpleNote = `_silent_mode "purple" is not a value the exporter reads`

func countSub(msgs []string, sub string) int {
	n := 0
	for _, m := range msgs {
		if strings.Contains(m, sub) {
			n++
		}
	}
	return n
}

// validateOn runs validate() for tenant vr-t with the tenant file holding
// base ("" = no file).
func validateOn(t *testing.T, base, body string) (errs, notices []string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "vr-t.yaml")
	if base != "" {
		if err := os.WriteFile(path, []byte(base), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return validate(dir, "vr-t", path, body)
}

func vrBody(lines string) string { return "tenants:\n  vr-t:\n" + lines }

func TestValidate_ValueRatchet(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name       string
		base, body string
		wantErr    bool
	}{
		{"new tenant (ENOENT) is strict", "", vrBody("    _silent_mode: purple\n"), true},
		{"unchanged, another key edited", vrBody("    _silent_mode: purple\n"),
			vrBody("    _silent_mode: purple\n    _severity_dedup: disable\n"), false},
		{"changed to another bad value", vrBody("    _silent_mode: purple\n"), vrBody("    _silent_mode: orange\n"), true},
		{"added to a tenant that had none", vrBody("    _severity_dedup: enable\n"), vrBody("    _silent_mode: purple\n"), true},
		{"Purple to purple", vrBody("    _silent_mode: Purple\n"), vrBody("    _silent_mode: \"purple\"\n"), false},
		{"base refused by parse-tree is strict", vrBody("    _silent_mode: purple\n") + "---\ntenants: {}\n",
			vrBody("    _silent_mode: purple\n"), true},
		{"base with a binary tag is strict", vrBody("    _silent_mode: purple\n    x: !!binary d2FybmluZw==\n"),
			vrBody("    _silent_mode: purple\n"), true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			errs, notices := validateOn(t, c.base, c.body)
			if c.wantErr {
				if countSub(errs, purpleNote)+countSub(errs, `"orange" is not a value`) != 1 || countSub(notices, "_silent_mode") != 0 {
					t.Errorf("want an error: errs=%q notices=%q", errs, notices)
				}
				return
			}
			if len(errs) != 0 || countSub(notices, purpleNote) != 1 {
				t.Errorf("want a notice: errs=%q notices=%q", errs, notices)
			}
		})
	}
}

// Deleting the key: nothing to say.
func TestValidate_ValueRatchet_KeyDeleted(t *testing.T) {
	t.Parallel()
	errs, notices := validateOn(t, vrBody("    _silent_mode: purple\n"), vrBody("    _severity_dedup: enable\n"))
	if len(errs) != 0 || countSub(notices, "_silent_mode") != 0 {
		t.Errorf("errs=%q notices=%q", errs, notices)
	}
}

// configDir == "" has no base: strict.
func TestValidate_ValueRatchet_NoConfigDirIsStrict(t *testing.T) {
	t.Parallel()
	errs, _ := validate("", "vr-t", "vr-t.yaml", vrBody("    _silent_mode: purple\n"))
	if countSub(errs, purpleNote) != 1 {
		t.Errorf("errs = %q", errs)
	}
}

// Ratchet moves the value findings only: an unknown key the base also had
// still blocks.
func TestValidate_ValueRatchet_OtherErrorsStay(t *testing.T) {
	t.Parallel()
	doc := vrBody("    _silent_mode: purple\n    _silent_mod: warning\n")
	errs, notices := validateOn(t, doc, doc)
	if countSub(errs, `unknown reserved key "_silent_mod"`) != 1 || countSub(errs, purpleNote) != 0 ||
		countSub(notices, purpleNote) != 1 {
		t.Errorf("errs=%q notices=%q", errs, notices)
	}
}

// Every tenant kv declares is ratcheted against its own section of the base,
// and a read error or a base that does not decode leaves kv as it is.
func TestRatchetValues_Direct(t *testing.T) {
	t.Parallel()
	cur := cfg.ThresholdConfig{Tenants: map[string]map[string]cfg.ScheduledValue{
		"a": {"_silent_mode": {Default: "purple"}},
		"b": {"_silent_mode": {Default: "orange"}},
	}}
	kv := cur.ValidateTenantKeysByTenant()
	base := []byte("tenants:\n  a:\n    _silent_mode: purple\n  b:\n    _silent_mode: purple\n")

	got := ratchetValues(kv, base, nil)
	if a := got.Tenants["a"]; len(a.Errors) != 0 || len(a.Notices) != 1 {
		t.Errorf("a: %+v", a)
	}
	if b := got.Tenants["b"]; len(b.Errors) != 1 || len(b.Notices) != 0 {
		t.Errorf("b: %+v", b)
	}
	for name, g := range map[string]cfg.KeyValidationByTenant{
		"ENOENT":      ratchetValues(kv, nil, os.ErrNotExist),
		"read error":  ratchetValues(kv, base, errors.New("EIO")),
		"empty file":  ratchetValues(kv, []byte{}, nil),
		"undecodable": ratchetValues(kv, []byte("tenants: [1, 2]\n"), nil),
		"parse-tree":  ratchetValues(kv, append(append([]byte{}, base...), []byte("---\n{}\n")...), nil),
	} {
		if len(g.Tenants["a"].Errors) != 1 || len(g.Tenants["b"].Errors) != 1 {
			t.Errorf("%s: not strict: %+v", name, g.Tenants)
		}
	}
}

// The write path end to end, direct mode: a tenant whose file already holds
// a bad value can still change another key (#3227's reason for the ratchet),
// and is told about the value; changing the value to another bad one is
// refused. The dry-run (PreviewPut) agrees with the write.
func TestWrite_ValueRatchet_DirectMode(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"vw-t.yaml": "tenants:\n  vw-t:\n    _silent_mode: purple\n"})
	w := newTestWriter(dir, dir)
	ctx := context.Background()

	ok := "tenants:\n  vw-t:\n    _silent_mode: purple\n    _severity_dedup: disable\n"
	perrs, pnotices, err := previewDryRun(ctx, w, "vw-t", ok)
	if err != nil || len(perrs) != 0 || countSub(pnotices, purpleNote) != 1 {
		t.Fatalf("dry-run: errs=%q notices=%q err=%v", perrs, pnotices, err)
	}
	notices, err := w.Write(ctx, "vw-t", "a@example.com", ok)
	if err != nil || countSub(notices, purpleNote) != 1 {
		t.Fatalf("Write: notices=%q err=%v", notices, err)
	}

	bad := "tenants:\n  vw-t:\n    _silent_mode: orange\n    _severity_dedup: disable\n"
	perrs, _, err = previewDryRun(ctx, w, "vw-t", bad)
	if err != nil || countSub(perrs, `"orange" is not a value the exporter reads`) != 1 {
		t.Fatalf("dry-run: errs=%q err=%v", perrs, err)
	}
	if _, err := w.Write(ctx, "vw-t", "a@example.com", bad); !errors.Is(err, ErrValidation) ||
		!strings.Contains(err.Error(), `"orange" is not a value the exporter reads`) {
		t.Fatalf("Write orange: err = %v, want ErrValidation", err)
	}
}

// Batch: each op is judged on the tree the ops before it left (#2900), so the
// ratchet's base is that tree — an op that leaves the value alone passes with
// a notice; op 1 fixing it and op 2 writing it back is a change, refused; and
// a refused op leaves no write behind, so the next op's base is the disk.
func TestWriteBatch_ValueRatchet(t *testing.T) {
	const onDisk = "tenants:\n  vb-t:\n    _silent_mode: purple\n"
	other := func(existing []byte) (string, error) {
		return string(existing) + "    _severity_dedup: disable\n", nil
	}

	t.Run("an op that leaves it alone passes with a notice", func(t *testing.T) {
		dir := seedTreeRepo(t, map[string]string{"vb-t.yaml": onDisk})
		w := newTestWriter(dir, dir)
		outcomes, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: "vb-t", Merge: other}}, "a@example.com")
		if err != nil || countSub(outcomes[0].Notices, purpleNote) != 1 {
			t.Fatalf("outcomes=%+v err=%v", outcomes, err)
		}
	})
	t.Run("same value patched again is a notice", func(t *testing.T) {
		dir := seedTreeRepo(t, map[string]string{"vb-t.yaml": onDisk})
		w := newTestWriter(dir, dir)
		outcomes, err := w.WriteBatch(context.Background(), []BatchOp{setOp("vb-t", onDisk), {TenantID: "vb-t", Merge: other}}, "a@example.com")
		if err != nil || countSub(outcomes[1].Notices, purpleNote) != 1 {
			t.Fatalf("outcomes=%+v err=%v", outcomes, err)
		}
	})
	t.Run("op 1 fixes it, op 2 writes it back: refused", func(t *testing.T) {
		dir := seedTreeRepo(t, map[string]string{"vb-t.yaml": onDisk})
		w := newTestWriter(dir, dir)
		_, err := w.WriteBatch(context.Background(), []BatchOp{
			setOp("vb-t", silentBody("vb-t", "warning")),
			setOp("vb-t", onDisk),
		}, "a@example.com")
		var rej *BatchRejectedError
		if !errors.As(err, &rej) || rej.Outcomes[0].Err != nil || !errors.Is(rej.Outcomes[1].Err, ErrValidation) {
			t.Fatalf("err = %v", err)
		}
	})
	t.Run("a refused op leaves the disk as the next op's base", func(t *testing.T) {
		dir := seedTreeRepo(t, map[string]string{"vb-t.yaml": onDisk})
		w := newTestWriter(dir, dir)
		_, err := w.WriteBatch(context.Background(), []BatchOp{
			setOp("vb-t", "tenants:\n  vb-t:\n    _silent_mode: orange\n"),
			{TenantID: "vb-t", Merge: other},
		}, "a@example.com")
		var rej *BatchRejectedError
		if !errors.As(err, &rej) || !errors.Is(rej.Outcomes[0].Err, ErrValidation) || rej.Outcomes[1].Err != nil ||
			countSub(rej.Outcomes[1].Notices, purpleNote) != 1 {
			t.Fatalf("err = %v outcomes = %+v", err, rej)
		}
	})
}

// A no-op merge stays a no-op: nothing is refused for what the file holds,
// and the value is told as a notice.
func TestWriteMerged_NoOpUnaffected(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"vn-t.yaml": "tenants:\n  vn-t:\n    _silent_mode: purple\n"})
	w := newTestWriter(dir, dir)
	notices, err := w.WriteMerged(context.Background(), "vn-t", "a@example.com",
		func([]byte) (string, error) { return "", ErrMergeNoOp })
	// WriteMerged answers a no-op as success with the file's notices.
	if err != nil || countSub(notices, purpleNote) != 1 {
		t.Fatalf("notices=%q err=%v", notices, err)
	}
}
