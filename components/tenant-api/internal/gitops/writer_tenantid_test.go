package gitops

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestWriterRejectsInvalidTenantID (ADR-035): guardTenantID is the one place
// every tenant write method enforces the tenant-id rule (a DNS-1123 label),
// so a caller that skips the handler's ValidateWritableTenantID still cannot
// write such an id. Each method returns ErrInvalidTenantID and writes nothing.
// Before ADR-035 the writer accepted `Team_A` / `UPPER` (#2341 R8 rule).
func TestWriterRejectsInvalidTenantID(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	body := func(id string) string { return "tenants:\n  " + id + ":\n    cpu: \"1\"\n" }

	cases := []struct {
		name string
		call func(w *Writer, id string) error
	}{
		{"Write", func(w *Writer, id string) error {
			_, err := w.Write(ctx, id, "op@example.com", body(id))
			return err
		}},
		{"WriteIfUnchanged", func(w *Writer, id string) error {
			// A non-empty base: an empty one is refused before the guard.
			_, err := w.WriteIfUnchanged(ctx, id, "op@example.com", body(id), "any-base-hash")
			return err
		}},
		{"WriteMerged", func(w *Writer, id string) error {
			_, err := w.WriteMerged(ctx, id, "op@example.com", func([]byte) (string, error) {
				return body(id), nil
			})
			return err
		}},
		{"WritePR", func(w *Writer, id string) error {
			_, err := w.WritePR(ctx, id, "op@example.com", body(id))
			return err
		}},
		{"WritePRBatch", func(w *Writer, id string) error {
			_, err := w.WritePRBatch(ctx, []PRBatchOp{{TenantID: id, Merge: func([]byte) (string, error) {
				return body(id), nil
			}}}, "op@example.com")
			return err
		}},
		{"WriteFederationSubsetFile", func(w *Writer, id string) error {
			return w.WriteFederationSubsetFile(ctx, id, "op@example.com", "metrics: []\n")
		}},
		{"DryRunValidate", func(w *Writer, id string) error {
			_, _, err := w.DryRunValidate(ctx, id, body(id))
			return err
		}},
		{"DryRunValidateBodyOnly", func(_ *Writer, id string) error {
			_, err := DryRunValidateBodyOnly(id, body(id))
			return err
		}},
	}

	for _, id := range []string{"Team_A", "UPPER", "tenant_1", "-x", strings.Repeat("a", 64)} {
		for _, tc := range cases {
			t.Run(tc.name+"/"+id, func(t *testing.T) {
				t.Parallel()
				dir := t.TempDir()
				w := NewWriter(dir, dir)
				err := tc.call(w, id)
				if !errors.Is(err, ErrInvalidTenantID) {
					t.Fatalf("%s(%q) error = %v, want ErrInvalidTenantID", tc.name, id, err)
				}
				for _, p := range []string{id + ".yaml", filepath.Join("_federation", id+".yaml")} {
					if _, serr := os.Stat(filepath.Join(dir, p)); serr == nil {
						t.Errorf("%s(%q) wrote %s", tc.name, id, p)
					}
				}
			})
		}
	}
}

// TestGuardTenantID_ReservedFirstAndValidPasses: a reserved id keeps its own
// refusal (ErrReservedTenantID, not the rule's), and a DNS-1123 id passes.
func TestGuardTenantID_ReservedFirstAndValidPasses(t *testing.T) {
	t.Parallel()
	if err := guardTenantID("_rbac"); !errors.Is(err, ErrReservedTenantID) {
		t.Errorf("guardTenantID(_rbac) = %v, want ErrReservedTenantID", err)
	}
	for _, id := range []string{"db-a", "a", "010", strings.Repeat("a", 63)} {
		if err := guardTenantID(id); err != nil {
			t.Errorf("guardTenantID(%q) = %v, want nil", id, err)
		}
	}
}
