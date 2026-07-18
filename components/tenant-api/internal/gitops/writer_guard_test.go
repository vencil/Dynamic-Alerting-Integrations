package gitops

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

// TestWriterRejectsReservedTenantID is the writer-side defense-in-depth backstop:
// even if a caller bypasses the handler's ValidateTenantID, no tenant write
// method may overwrite a reserved conf.d control file. Each method must return
// ErrReservedTenantID and leave the on-disk control file byte-for-byte intact.
func TestWriterRejectsReservedTenantID(t *testing.T) {
	const original = "domain_policies:\n  finance:\n    allowed_receiver_types: [slack]\n"
	ctx := context.Background()

	// A tenant id whose {id}.yaml is a real control file.
	const reserved = "_domain_policy"

	cases := []struct {
		name string
		call func(w *Writer) error
	}{
		{"Write", func(w *Writer) error {
			return w.Write(ctx, reserved, "op@example.com", "tenants:\n  x:\n    cpu: \"1\"\n")
		}},
		{"WriteMerged", func(w *Writer) error {
			return w.WriteMerged(ctx, reserved, "op@example.com", func([]byte) (string, error) {
				return "tenants:\n  x:\n    cpu: \"1\"\n", nil
			})
		}},
		{"WritePR", func(w *Writer) error {
			_, err := w.WritePR(ctx, reserved, "op@example.com", "tenants:\n  x:\n    cpu: \"1\"\n")
			return err
		}},
		{"WritePRBatch", func(w *Writer) error {
			_, err := w.WritePRBatch(ctx, []PRBatchOp{{TenantID: reserved, Merge: func([]byte) (string, error) {
				return "tenants:\n  x:\n    cpu: \"1\"\n", nil
			}}}, "op@example.com")
			return err
		}},
		{"WriteFederationSubsetFile", func(w *Writer) error {
			return w.WriteFederationSubsetFile(ctx, reserved, "op@example.com", "metrics: []\n")
		}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			ctrl := filepath.Join(dir, reserved+".yaml")
			if err := os.WriteFile(ctrl, []byte(original), 0o644); err != nil {
				t.Fatal(err)
			}
			w := NewWriter(dir, dir)

			err := tc.call(w)
			if !errors.Is(err, ErrReservedTenantID) {
				t.Fatalf("%s(%q) error = %v, want ErrReservedTenantID", tc.name, reserved, err)
			}
			got, rerr := os.ReadFile(ctrl)
			if rerr != nil {
				t.Fatalf("read control file: %v", rerr)
			}
			if string(got) != original {
				t.Errorf("%s clobbered %s: got %q, want unchanged %q", tc.name, ctrl, got, original)
			}
		})
	}
}
