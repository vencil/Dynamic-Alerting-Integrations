package statemigrate

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/statestore"
)

type fakeHistory struct {
	max   uint32
	err   error
	calls int
}

func (h *fakeHistory) HistoricalMaxUint(context.Context, string, string) (uint32, error) {
	h.calls++
	return h.max, h.err
}

func write(t *testing.T, dir, rel, content string) {
	t.Helper()
	p := filepath.Join(dir, rel)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func newStore(t *testing.T) statestore.Store {
	t.Helper()
	s, err := statestore.NewDir(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func get(t *testing.T, s statestore.Store, key string) string {
	t.Helper()
	b, ok, err := s.Get(context.Background(), key)
	if err != nil {
		t.Fatal(err)
	}
	if !ok {
		return "<absent>"
	}
	return string(b)
}

func TestImportMovesEveryRecordOnce(t *testing.T) {
	confd := t.TempDir()
	write(t, confd, "_groups.yaml", "groups: {g: {label: G}}\n")
	write(t, confd, "_views.yaml", "views: {}\n")
	write(t, confd, "_federation_policy.yaml", "whitelist:\n- metric: up\n")
	write(t, confd, "_federation/db-a.yaml", "metrics: [up]\n")
	write(t, confd, "_federation/db-b.yml", "metrics: []\n") // any spelling the old reader took
	write(t, confd, "db-a.yaml", "tenants: {db-a: {}}\n")    // a tenant, not state
	st := newStore(t)

	rep, err := Import(context.Background(), st, confd, &fakeHistory{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"_groups.yaml", "_views.yaml", "_federation_policy.yaml", "_federation.db-a.yaml", "_federation.db-b.yaml"}
	if strings.Join(rep.Imported, ",") != strings.Join(want, ",") || len(rep.Kept) != 0 {
		t.Fatalf("first pass: imported %v kept %v, want imported %v", rep.Imported, rep.Kept, want)
	}
	if got := get(t, st, statestore.SubsetKey("db-a")); got != "metrics: [up]\n" {
		t.Errorf("subset db-a = %q", got)
	}

	// The store wins from now on: a later conf.d change is not imported over it.
	write(t, confd, "_groups.yaml", "groups: {}\n")
	rep, err = Import(context.Background(), st, confd, &fakeHistory{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.Imported) != 0 || len(rep.Kept) != len(want) {
		t.Fatalf("second pass: imported %v kept %v, want nothing imported", rep.Imported, rep.Kept)
	}
	if got := get(t, st, statestore.KeyGroups); got != "groups: {g: {label: G}}\n" {
		t.Errorf("groups overwritten by a later conf.d copy: %q", got)
	}
}

func TestImportNothingToMove(t *testing.T) {
	st := newStore(t)
	rep, err := Import(context.Background(), st, t.TempDir(), &fakeHistory{}, nil)
	if err != nil || len(rep.Imported)+len(rep.Kept) != 0 {
		t.Fatalf("empty conf.d: %+v, %v", rep, err)
	}
	if got := get(t, st, statestore.KeyAccountRegistry); got != "<absent>" {
		t.Errorf("a registry was created from nothing: %q", got)
	}
}

// The ledger's next_account_id is raised to the git-history high-water mark:
// a counter reverted to 1002 after reaching 1005 must not hand out 1002 again.
func TestImportRegistryFloorsToHistory(t *testing.T) {
	confd := t.TempDir()
	write(t, confd, account.RegistryFileName, "schema_version: v1\nnext_account_id: 1002\nallocations:\n  a: 1000\n  b: 1001\n")
	st := newStore(t)
	h := &fakeHistory{max: 1005}
	if _, err := Import(context.Background(), st, confd, h, nil); err != nil {
		t.Fatal(err)
	}
	reg, err := account.Parse([]byte(get(t, st, account.RegistryFileName)))
	if err != nil {
		t.Fatal(err)
	}
	if reg.NextAccountID != 1005 {
		t.Fatalf("imported next_account_id = %d, want 1005", reg.NextAccountID)
	}
	if id, ok := reg.Lookup("b"); !ok || id != 1001 {
		t.Fatalf("allocation b = %d, %v; want 1001 kept", id, ok)
	}

	// Already in the store: git history is not read again.
	h2 := &fakeHistory{max: 9999}
	rep, err := Import(context.Background(), st, confd, h2, nil)
	if err != nil {
		t.Fatal(err)
	}
	if h2.calls != 0 {
		t.Errorf("git history read %d time(s) though the registry is already in the store", h2.calls)
	}
	if len(rep.Kept) != 1 || rep.Kept[0] != account.RegistryFileName {
		t.Errorf("second pass kept %v", rep.Kept)
	}
}

// The file is gone (an emptyDir restart) but git history remembers ids: the
// imported ledger starts above them rather than at the floor.
func TestImportRegistryFromHistoryAlone(t *testing.T) {
	st := newStore(t)
	if _, err := Import(context.Background(), st, t.TempDir(), &fakeHistory{max: 1010}, nil); err != nil {
		t.Fatal(err)
	}
	reg, err := account.Parse([]byte(get(t, st, account.RegistryFileName)))
	if err != nil {
		t.Fatal(err)
	}
	if reg.NextAccountID != 1010 {
		t.Fatalf("next_account_id = %d, want 1010", reg.NextAccountID)
	}
}

func TestImportRegistryFailsClosed(t *testing.T) {
	t.Run("unparseable", func(t *testing.T) {
		confd := t.TempDir()
		write(t, confd, account.RegistryFileName, "schema_version: v1\nnext_account_id: 1000\nallocations:\n  a: 1005\n")
		st := newStore(t)
		if _, err := Import(context.Background(), st, confd, &fakeHistory{}, nil); err == nil {
			t.Fatal("a registry that does not parse was imported")
		}
		if got := get(t, st, account.RegistryFileName); got != "<absent>" {
			t.Errorf("store holds %q after a refused import", got)
		}
	})
	t.Run("history unreadable", func(t *testing.T) {
		if _, err := Import(context.Background(), newStore(t), t.TempDir(), &fakeHistory{err: errors.New("git")}, nil); err == nil {
			t.Fatal("an unreadable history was taken as no history")
		}
	})
}

func fleetOf(n int) FleetSize { return func() (int, error) { return n, nil } }

// A blank or missing ledger while tenants exist is withheld, not imported as
// an empty one: the startup check must still see the slot empty and refuse to
// boot, or the next allocation re-issues an id a live tenant already holds.
func TestImportWithholdsALostRegistryWhileTenantsExist(t *testing.T) {
	for name, tc := range map[string]struct {
		file    *string
		histMax uint32
	}{
		"blank file, no history":     {file: ptr(""), histMax: 0},
		"blank file, history":        {file: ptr("  \n"), histMax: 1003},
		"missing file, history only": {file: nil, histMax: 1003},
	} {
		t.Run(name, func(t *testing.T) {
			confd := t.TempDir()
			write(t, confd, "db-a.yaml", "tenants: {db-a: {}}\n")
			if tc.file != nil {
				write(t, confd, account.RegistryFileName, *tc.file)
			}
			st := newStore(t)
			rep, err := Import(context.Background(), st, confd, &fakeHistory{max: tc.histMax}, fleetOf(1))
			if err != nil {
				t.Fatal(err)
			}
			if got := get(t, st, account.RegistryFileName); got != "<absent>" {
				t.Fatalf("a lost ledger was imported as %q", got)
			}
			if len(rep.Withheld) != 1 || rep.Withheld[0] != account.RegistryFileName {
				t.Errorf("withheld = %v, want the registry", rep.Withheld)
			}
			if err := account.VerifyRegistryNotResetWithFleet(context.Background(), confd, st); err == nil {
				t.Error("the startup check passed after the import; it must still refuse the lost ledger")
			}
		})
	}
}

// With no tenants left, a missing ledger still carries the history's
// high-water mark over, so ids handed out before are not handed out again.
func TestImportRegistryFromHistoryWithEmptyFleet(t *testing.T) {
	st := newStore(t)
	rep, err := Import(context.Background(), st, t.TempDir(), &fakeHistory{max: 1010}, fleetOf(0))
	if err != nil {
		t.Fatal(err)
	}
	reg, err := account.Parse([]byte(get(t, st, account.RegistryFileName)))
	if err != nil {
		t.Fatal(err)
	}
	if reg.NextAccountID != 1010 || len(rep.Withheld) != 0 {
		t.Fatalf("next_account_id = %d, withheld %v; want 1010 imported", reg.NextAccountID, rep.Withheld)
	}
}

// Two spellings of one tenant's subset cannot both become the one record the
// store keeps: refuse rather than silently drop one.
func TestImportRefusesTwoSubsetFilesForOneTenant(t *testing.T) {
	confd := t.TempDir()
	write(t, confd, "_federation/db-b.yaml", "metrics: [up]\n")
	write(t, confd, "_federation/db-b.yml", "metrics: []\n")
	st := newStore(t)
	_, err := Import(context.Background(), st, confd, nil, nil)
	if err == nil || !strings.Contains(err.Error(), "db-b.yaml") || !strings.Contains(err.Error(), "db-b.yml") {
		t.Fatalf("err = %v, want a refusal naming both files", err)
	}
	if got := get(t, st, statestore.SubsetKey("db-b")); got != "<absent>" {
		t.Errorf("store holds %q after the refusal", got)
	}

	// Once the store holds the tenant's subset, the conf.d copies are stale
	// and no longer block the boot.
	if _, err := st.Mutate(context.Background(), statestore.SubsetKey("db-b"), statestore.Meta{},
		func([]byte) ([]byte, error) { return []byte("metrics: [up]\n"), nil }); err != nil {
		t.Fatal(err)
	}
	if _, err := Import(context.Background(), st, confd, nil, nil); err != nil {
		t.Fatalf("stale duplicates blocked the import: %v", err)
	}
}

func ptr(s string) *string { return &s }
