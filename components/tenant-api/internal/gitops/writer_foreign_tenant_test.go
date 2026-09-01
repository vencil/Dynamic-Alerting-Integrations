package gitops

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/confd"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// smuggled is a body whose URL id is db-a but which also declares `other`.
const smuggled = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n  other:\n    _silent_mode: \"warning\"\n"

// TestWriteRejectsAForeignTenantSection drives the real Write, not validate:
// the gate has to hold at the call site that reaches disk. Both arms matter —
// the exporter's duplicate-tenant guard only fires when the smuggled id also
// owns a file, so the arm WITHOUT one (a "ghost" tenant the exporter would
// serve and GET /tenants could not see) has no downstream backstop at all.
func TestWriteRejectsAForeignTenantSection(t *testing.T) {
	for _, tc := range []struct {
		name        string
		victimOwnsA bool
	}{
		{"smuggled id owns a file", true},
		{"smuggled id owns no file", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := initRepoOnMain(t)
			if tc.victimOwnsA {
				if err := os.WriteFile(filepath.Join(dir, "other.yaml"),
					[]byte("tenants:\n  other:\n    _silent_mode: \"false\"\n"), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			w := NewWriter(dir, dir)

			if _, err := w.Write(context.Background(), "db-a", "a@example.com", smuggled); err == nil {
				t.Fatal("Write accepted a body declaring a tenant section it does not address")
			} else if !errors.Is(err, ErrValidation) {
				t.Errorf("want ErrValidation, got %v", err)
			} else if !strings.Contains(err.Error(), "other") {
				t.Errorf("error must name the foreign section so the caller can fix it, got %v", err)
			}

			if _, err := os.Stat(filepath.Join(dir, "db-a.yaml")); !os.IsNotExist(err) {
				t.Errorf("rejected write still touched disk (stat err=%v)", err)
			}
		})
	}
}

// WritePR is a second, independent caller of validate (writer_pr.go), so the
// gate is asserted there too rather than assumed to be shared.
func TestWritePRRejectsAForeignTenantSection(t *testing.T) {
	dir := initRepoOnMain(t)
	w := NewWriter(dir, dir)
	if _, err := w.WritePR(context.Background(), "db-a", "a@example.com", smuggled); err == nil {
		t.Fatal("WritePR accepted a smuggled tenant section")
	} else if !errors.Is(err, ErrValidation) {
		t.Errorf("want ErrValidation, got %v", err)
	}
}

func TestWriteStillAcceptsABodyThatOnlyDeclaresItsOwnID(t *testing.T) {
	dir := initRepoOnMain(t)
	w := NewWriter(dir, dir)
	if _, err := w.Write(context.Background(), "db-a", "a@example.com", validTenantYAML); err != nil {
		t.Fatalf("gate rejected a legitimate single-tenant write: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "db-a.yaml")); err != nil {
		t.Errorf("accepted write did not reach disk: %v", err)
	}
}

func TestForeignTenantKeys(t *testing.T) {
	for _, tc := range []struct {
		name string
		body string
		id   string
		want []string
	}{
		{"own id only", validTenantYAML, "db-a", nil},
		{"one foreign", smuggled, "db-a", []string{"other"}},
		{"sorted, and the id itself is never listed",
			"tenants:\n  zz: {}\n  db-a: {}\n  aa: {}\n", "db-a", []string{"aa", "zz"}},
		{"empty tenants map", "tenants: {}\n", "db-a", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var tcfg cfg.ThresholdConfig
			if err := yaml.Unmarshal([]byte(tc.body), &tcfg); err != nil {
				t.Fatal(err)
			}
			got := foreignTenantKeys(tcfg, tc.id)
			if len(got) != len(tc.want) {
				t.Fatalf("got %v want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("got %v want %v", got, tc.want)
				}
			}
		})
	}
}

// TestTheTwoPlanesAgreeOnWhatABodyDeclares pins WHY the gate exists rather than
// what it does: tenant-api derives tenant ids from the FILENAME, the exporter
// from the body's `tenants:` KEYS. Before the gate those two sets could differ
// for a body the write plane accepted — this asserts they cannot any more.
func TestTheTwoPlanesAgreeOnWhatABodyDeclares(t *testing.T) {
	dir := initRepoOnMain(t)
	w := NewWriter(dir, dir)

	for _, body := range []string{validTenantYAML, smuggled} {
		if _, err := w.Write(context.Background(), "db-a", "a@example.com", body); err != nil {
			continue // rejected: it never becomes a file, so it cannot diverge
		}
		raw, err := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
		if err != nil {
			t.Fatal(err)
		}
		var tcfg cfg.ThresholdConfig
		if err := yaml.Unmarshal(raw, &tcfg); err != nil {
			t.Fatal(err)
		}
		contentPlane := make([]string, 0, len(tcfg.Tenants))
		for id := range tcfg.Tenants {
			contentPlane = append(contentPlane, id)
		}
		sort.Strings(contentPlane)

		id, ok := confd.TenantIDFromFile("db-a.yaml")
		if !ok {
			t.Fatal("filename plane rejected a file it just wrote")
		}
		filenamePlane := []string{id}

		if len(contentPlane) != len(filenamePlane) || contentPlane[0] != filenamePlane[0] {
			t.Errorf("planes disagree on a body the write gate accepted: filename=%v content=%v",
				filenamePlane, contentPlane)
		}
	}
}
