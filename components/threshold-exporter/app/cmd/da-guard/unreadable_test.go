package main

// unreadable_test.go — #2588: `da-guard effective` and the default guard
// name the paths the walk cannot stat, read or list (unreadable:
// [{file, reason}]) exactly as served-values does (#2115), and exit 3.
// Before, both dropped them silently: a tenant in such a file vanished, a
// value an unreadable `_defaults.yaml` gave vanished from every tenant, and
// the run exited 0.
//
// Every shape that needs a permission bit is skipped as root (chmod does not
// stop root); the dangling-symlink shapes run as any user, so CI (non-root)
// runs all of them and a root run still measures one tenant file and one
// `_defaults.yaml`.
//
// Seams: none — t.TempDir() trees through run().

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// unreadableBase: tenant-a writes its own value; tenant-b, in a
// sub-directory, writes none — everything it has comes from _defaults.yaml.
func unreadableBase() map[string]string {
	return map[string]string{
		"_defaults.yaml":    "defaults:\n  mysql_connections: 80\n",
		"tenant-a.yaml":     "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
		"sub/tenant-b.yaml": "tenants:\n  tenant-b: {}\n",
	}
}

func symlinkOrSkipT(t *testing.T, target, link string) {
	t.Helper()
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("os.Symlink unavailable here (%v); CI measures this on ubuntu-latest", err)
	}
}

func chmodT(t *testing.T, p string, mode, restore os.FileMode) {
	t.Helper()
	if err := os.Chmod(p, mode); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(p, restore) })
}

type unreadableShape struct {
	name    string
	nonRoot bool // needs a permission bit root ignores
	breakIt func(t *testing.T, dir string)
	want    []skippedFile
	// tenants left in the effective output; tenantB is tenant-b's
	// effective_config when it is among them.
	tenants []string
	tenantB map[string]any
}

func unreadableShapes() []unreadableShape {
	return []unreadableShape{
		{
			name: "dangling symlink tenant file",
			breakIt: func(t *testing.T, dir string) {
				symlinkOrSkipT(t, "missing.yaml", filepath.Join(dir, "tenant-c.yaml"))
			},
			want:    []skippedFile{{"tenant-c.yaml", config.UnreadableStatError}},
			tenants: []string{"tenant-a", "tenant-b"},
			tenantB: map[string]any{"mysql_connections": float64(80)},
		},
		{
			// The #2588 extreme: a tenant with no value of its own reads as
			// "nothing configured" when the defaults it inherits are gone.
			name: "dangling symlink _defaults.yaml",
			breakIt: func(t *testing.T, dir string) {
				p := filepath.Join(dir, "_defaults.yaml")
				if err := os.Remove(p); err != nil {
					t.Fatal(err)
				}
				symlinkOrSkipT(t, "missing.yaml", p)
			},
			want:    []skippedFile{{"_defaults.yaml", config.UnreadableStatError}},
			tenants: []string{"tenant-a", "tenant-b"},
			tenantB: map[string]any{},
		},
		{
			name:    "tenant file 0000",
			nonRoot: true,
			breakIt: func(t *testing.T, dir string) {
				chmodT(t, filepath.Join(dir, "tenant-a.yaml"), 0, 0o644)
			},
			want:    []skippedFile{{"tenant-a.yaml", config.UnreadableReadError}},
			tenants: []string{"tenant-b"},
			tenantB: map[string]any{"mysql_connections": float64(80)},
		},
		{
			name:    "_defaults.yaml 0000",
			nonRoot: true,
			breakIt: func(t *testing.T, dir string) {
				chmodT(t, filepath.Join(dir, "_defaults.yaml"), 0, 0o644)
			},
			want:    []skippedFile{{"_defaults.yaml", config.UnreadableReadError}},
			tenants: []string{"tenant-a", "tenant-b"},
			tenantB: map[string]any{},
		},
		{
			name:    "sub-directory 0000",
			nonRoot: true,
			breakIt: func(t *testing.T, dir string) {
				chmodT(t, filepath.Join(dir, "sub"), 0, 0o755)
			},
			want:    []skippedFile{{"sub", config.UnreadableWalkError}},
			tenants: []string{"tenant-a"},
		},
		{
			name:    "sub-directory 0644",
			nonRoot: true,
			breakIt: func(t *testing.T, dir string) {
				chmodT(t, filepath.Join(dir, "sub"), 0o644, 0o755)
			},
			want:    []skippedFile{{"sub/tenant-b.yaml", config.UnreadableStatError}},
			tenants: []string{"tenant-a"},
		},
	}
}

type effectiveUnreadableOut struct {
	ParseFailed []string      `json:"parse_failed"`
	Unreadable  []skippedFile `json:"unreadable"`
	Tenants     map[string]struct {
		EffectiveConfig map[string]any `json:"effective_config"`
	} `json:"tenants"`
}

func effectiveUnreadable(t *testing.T, dir string) (int, effectiveUnreadableOut, string) {
	t.Helper()
	code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir)
	var doc effectiveUnreadableOut
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("effective stdout is not JSON (%v): %q; stderr=%q", err, stdout, stderr)
	}
	return code, doc, stderr
}

func servedUnreadable(t *testing.T, dir string) []skippedFile {
	t.Helper()
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir)
	var doc struct {
		Unreadable []skippedFile `json:"unreadable"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("served-values stdout is not JSON (%v, exit %d): %q; stderr=%q", err, code, stdout, stderr)
	}
	return doc.Unreadable
}

type guardUnreadableOut struct {
	SourceFiles []string      `json:"source_files"`
	ParseFailed []string      `json:"parse_failed"`
	Unreadable  []skippedFile `json:"unreadable"`
	Report      *struct {
		Summary struct {
			TotalTenants int `json:"total_tenants"`
		} `json:"summary"`
	} `json:"report"`
}

func guardJSON(t *testing.T, dir string, extra ...string) (int, guardUnreadableOut, string) {
	t.Helper()
	args := append([]string{"--config-dir", dir, "--format", "json"}, extra...)
	code, stdout, stderr := runOnce(t, args...)
	var doc guardUnreadableOut
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("guard stdout is not JSON (%v, exit %d): %q; stderr=%q", err, code, stdout, stderr)
	}
	return code, doc, stderr
}

// TestUnreadable_EffectiveAndGuardMatchServedValues: per shape, effective and
// the whole-tree guard exit 3 and list exactly served-values' unreadable.
func TestUnreadable_EffectiveAndGuardMatchServedValues(t *testing.T) {
	t.Parallel()
	for _, s := range unreadableShapes() {
		t.Run(s.name, func(t *testing.T) {
			t.Parallel()
			if s.nonRoot && os.Geteuid() == 0 {
				t.Skip("running as root: chmod does not stop root; the dangling-symlink shapes run for every user")
			}
			dir := writeParityTree(t, unreadableBase())
			s.breakIt(t, dir)

			served := servedUnreadable(t, dir)
			if !reflect.DeepEqual(served, s.want) {
				t.Fatalf("precondition: served-values unreadable = %v, want %v", served, s.want)
			}

			code, doc, stderr := effectiveUnreadable(t, dir)
			if code != exitParseFailed {
				t.Errorf("effective exit = %d, want %d; stderr=%q", code, exitParseFailed, stderr)
			}
			if !reflect.DeepEqual(doc.Unreadable, served) {
				t.Errorf("effective unreadable = %v, served-values %v", doc.Unreadable, served)
			}
			if len(doc.ParseFailed) != 0 {
				t.Errorf("effective parse_failed = %v, want []", doc.ParseFailed)
			}
			if ids := keysOf(doc.Tenants); !reflect.DeepEqual(ids, s.tenants) {
				t.Errorf("effective tenants = %v, want %v", ids, s.tenants)
			}
			if s.tenantB != nil {
				if got := doc.Tenants["tenant-b"].EffectiveConfig; !reflect.DeepEqual(got, s.tenantB) {
					t.Errorf("tenant-b effective_config = %v, want %v", got, s.tenantB)
				}
			}
			if !strings.Contains(stderr, "da-guard effective: 1 path(s) cannot be read: "+s.want[0].File) {
				t.Errorf("effective stderr does not name the path: %q", stderr)
			}

			gcode, gdoc, gstderr := guardJSON(t, dir)
			if gcode != exitParseFailed {
				t.Errorf("guard exit = %d, want %d; stderr=%q", gcode, exitParseFailed, gstderr)
			}
			if !reflect.DeepEqual(gdoc.Unreadable, served) {
				t.Errorf("guard unreadable = %v, served-values %v", gdoc.Unreadable, served)
			}
			if !strings.Contains(gstderr, "da-guard: 1 path(s) cannot be read: "+s.want[0].File) {
				t.Errorf("guard stderr does not name the path: %q", gstderr)
			}
			// The cap line must not claim the root defaults file is missing
			// when it is there but unreadable.
			if s.want[0].File == "_defaults.yaml" &&
				!strings.Contains(gstderr, "built-in default (root _defaults.yaml absent or unreadable)") {
				t.Errorf("guard cap line does not cover an unreadable root defaults file: %q", gstderr)
			}
		})
	}
}

// The scoped guard: a scope whose only tenant file cannot be read used to be
// the vacuously-safe exit 0 (total_tenants 0). Now exit 3, naming it.
func TestUnreadable_GuardScope_EmptyScopeIsNotSafe(t *testing.T) {
	t.Parallel()
	for _, s := range []struct {
		name    string
		nonRoot bool
		scope   string // root-relative; "" = sub
		breakIt func(t *testing.T, dir string)
		want    []skippedFile
	}{
		{
			name: "dangling symlink as the scope's only tenant file",
			breakIt: func(t *testing.T, dir string) {
				p := filepath.Join(dir, "sub", "tenant-b.yaml")
				if err := os.Remove(p); err != nil {
					t.Fatal(err)
				}
				symlinkOrSkipT(t, "missing.yaml", p)
			},
			want: []skippedFile{{"sub/tenant-b.yaml", config.UnreadableStatError}},
		},
		{
			name:    "scope directory 0000",
			nonRoot: true,
			breakIt: func(t *testing.T, dir string) {
				chmodT(t, filepath.Join(dir, "sub"), 0, 0o755)
			},
			want: []skippedFile{{"sub", config.UnreadableWalkError}},
		},
		{
			// A directory ABOVE the scope that cannot be listed but can be
			// searched (0311): the scope itself still stats, and every
			// tenant under it is lost — the directory rule's "contains the
			// scope" branch.
			name:    "directory containing the scope 0311",
			nonRoot: true,
			scope:   "team/sub2",
			breakIt: func(t *testing.T, dir string) {
				if err := os.MkdirAll(filepath.Join(dir, "team", "sub2"), 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(dir, "team", "sub2", "t.yaml"),
					[]byte("tenants:\n  tenant-x:\n    mysql_connections: 60\n"), 0o644); err != nil {
					t.Fatal(err)
				}
				chmodT(t, filepath.Join(dir, "team"), 0o311, 0o755)
			},
			want: []skippedFile{{"team", config.UnreadableWalkError}},
		},
	} {
		t.Run(s.name, func(t *testing.T) {
			t.Parallel()
			if s.nonRoot && os.Geteuid() == 0 {
				t.Skip("running as root: chmod does not stop root")
			}
			dir := writeParityTree(t, unreadableBase())
			s.breakIt(t, dir)
			rel := s.scope
			if rel == "" {
				rel = "sub"
			}
			scope := filepath.Join(dir, filepath.FromSlash(rel))
			code, doc, stderr := guardJSON(t, dir, "--scope", scope)
			if code != exitParseFailed {
				t.Fatalf("exit = %d, want %d; stderr=%q", code, exitParseFailed, stderr)
			}
			if doc.Report == nil || doc.Report.Summary.TotalTenants != 0 {
				t.Errorf("report = %+v, want the empty-scope report", doc.Report)
			}
			if !reflect.DeepEqual(doc.Unreadable, s.want) {
				t.Errorf("unreadable = %v, want %v", doc.Unreadable, s.want)
			}
			mcode, md, _ := runOnce(t, "--config-dir", dir, "--scope", scope)
			if mcode != exitParseFailed {
				t.Errorf("md exit = %d, want %d", mcode, exitParseFailed)
			}
			for _, want := range []string{"### Files the exporter cannot read", "`" + s.want[0].File + "` (" + s.want[0].Reason + ")", "NOT a safe result"} {
				if !strings.Contains(md, want) {
					t.Errorf("md report lacks %q:\n%s", want, md)
				}
			}
			if strings.Contains(md, "vacuously safe") {
				t.Errorf("md report still says vacuously safe:\n%s", md)
			}
		})
	}
}

// Scope rule: what bears on a scoped run is ParseFailed's rule — a path at or
// below the scope, or a `_` file in a directory above it. A tenant file
// outside the scope does not; the root `_defaults.yaml` does. A directory the
// walk cannot list follows the directory rule only: a `_`-named directory
// beside the scope does not bear on it.
func TestUnreadable_GuardScope_FollowsTheParseFailedRule(t *testing.T) {
	t.Parallel()
	t.Run("unlistable _ directory beside the scope", func(t *testing.T) {
		t.Parallel()
		if os.Geteuid() == 0 {
			t.Skip("running as root: chmod does not stop root")
		}
		files := unreadableBase()
		files["_archive/old.yaml"] = "tenants:\n  tenant-old:\n    mysql_connections: 1\n"
		dir := writeParityTree(t, files)
		chmodT(t, filepath.Join(dir, "_archive"), 0, 0o755)
		// Precondition: the whole tree does see it, so the shape is live.
		if wcode, wdoc, _ := guardJSON(t, dir); wcode != exitParseFailed ||
			!reflect.DeepEqual(wdoc.Unreadable, []skippedFile{{"_archive", config.UnreadableWalkError}}) {
			t.Fatalf("precondition: whole-tree run exit %d unreadable %v", wcode, wdoc.Unreadable)
		}
		code, doc, stderr := guardJSON(t, dir, "--scope", filepath.Join(dir, "sub"))
		mustOK(t, code, stderr)
		if doc.Unreadable != nil {
			t.Errorf("unreadable = %v, want absent", doc.Unreadable)
		}
	})
	t.Run("tenant file outside the scope", func(t *testing.T) {
		t.Parallel()
		dir := writeParityTree(t, unreadableBase())
		symlinkOrSkipT(t, "missing.yaml", filepath.Join(dir, "tenant-c.yaml"))
		code, doc, stderr := guardJSON(t, dir, "--scope", filepath.Join(dir, "sub"))
		mustOK(t, code, stderr)
		if doc.Unreadable != nil {
			t.Errorf("unreadable = %v, want absent", doc.Unreadable)
		}
	})
	t.Run("root _defaults.yaml above the scope", func(t *testing.T) {
		t.Parallel()
		dir := writeParityTree(t, unreadableBase())
		p := filepath.Join(dir, "_defaults.yaml")
		if err := os.Remove(p); err != nil {
			t.Fatal(err)
		}
		symlinkOrSkipT(t, "missing.yaml", p)
		code, doc, stderr := guardJSON(t, dir, "--scope", filepath.Join(dir, "sub"))
		if code != exitParseFailed {
			t.Fatalf("exit = %d, want %d; stderr=%q", code, exitParseFailed, stderr)
		}
		if want := []skippedFile{{"_defaults.yaml", config.UnreadableStatError}}; !reflect.DeepEqual(doc.Unreadable, want) {
			t.Errorf("unreadable = %v, want %v", doc.Unreadable, want)
		}
		if doc.Report == nil || doc.Report.Summary.TotalTenants != 1 {
			t.Errorf("report = %+v, want tenant-b checked", doc.Report)
		}
		_, md, _ := runOnce(t, "--config-dir", dir, "--scope", filepath.Join(dir, "sub"))
		if strings.Contains(md, "safe to merge") || !strings.Contains(md, "### Files the exporter cannot read") {
			t.Errorf("md report must name the file and not say safe to merge:\n%s", md)
		}
	})
}

// A config-named symlink to a directory is skipped, not unreadable: exit 0
// and unreadable [] for effective, nothing for the guard.
func TestUnreadable_DirectorySymlinkIsNotUnreadable(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, unreadableBase())
	if err := os.Mkdir(filepath.Join(dir, "realdir"), 0o755); err != nil {
		t.Fatal(err)
	}
	symlinkOrSkipT(t, "realdir", filepath.Join(dir, "x.yaml"))
	code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir)
	mustOK(t, code, stderr)
	if !strings.Contains(stdout, `"unreadable": []`) {
		t.Errorf("effective unreadable must be an empty list:\n%s", stdout)
	}
	gcode, gdoc, gstderr := guardJSON(t, dir)
	mustOK(t, gcode, gstderr)
	if gdoc.Unreadable != nil {
		t.Errorf("guard unreadable = %v, want absent", gdoc.Unreadable)
	}
}

func TestUnreadable_CleanTree_EffectiveListsEmpty(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, unreadableBase())
	code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir)
	mustOK(t, code, stderr)
	if !strings.Contains(stdout, `"unreadable": []`) {
		t.Errorf("effective unreadable must be present as []:\n%s", stdout)
	}
}

// A chain file that does not decode stops the resolve (no tenant); the path
// the walk could not read is still named, by effective and the guard.
func TestUnreadable_DecodeStopStillNamesUnreadable(t *testing.T) {
	t.Parallel()
	files := unreadableBase()
	files["sub/_defaults.yaml"] = "defaults: [\n"
	dir := writeParityTree(t, files)
	symlinkOrSkipT(t, "missing.yaml", filepath.Join(dir, "tenant-c.yaml"))
	want := []skippedFile{{"tenant-c.yaml", config.UnreadableStatError}}

	code, doc, stderr := effectiveUnreadable(t, dir)
	if code != exitParseFailed {
		t.Fatalf("effective exit = %d, want %d; stderr=%q", code, exitParseFailed, stderr)
	}
	if !reflect.DeepEqual(doc.ParseFailed, []string{"sub/_defaults.yaml"}) || !reflect.DeepEqual(doc.Unreadable, want) {
		t.Errorf("effective parse_failed = %v unreadable = %v, want [sub/_defaults.yaml] %v",
			doc.ParseFailed, doc.Unreadable, want)
	}
	if len(doc.Tenants) != 0 {
		t.Errorf("effective tenants = %v, want none: the resolve stopped", keysOf(doc.Tenants))
	}

	gcode, gdoc, gstderr := guardJSON(t, dir)
	if gcode != exitParseFailed {
		t.Fatalf("guard exit = %d, want %d; stderr=%q", gcode, exitParseFailed, gstderr)
	}
	if !reflect.DeepEqual(gdoc.ParseFailed, []string{"sub/_defaults.yaml"}) || !reflect.DeepEqual(gdoc.Unreadable, want) {
		t.Errorf("guard parse_failed = %v unreadable = %v", gdoc.ParseFailed, gdoc.Unreadable)
	}
	if !strings.Contains(gstderr, "cannot be read: tenant-c.yaml (stat_error)") {
		t.Errorf("guard stderr does not name the unreadable path: %q", gstderr)
	}
	_, md, _ := runOnce(t, "--config-dir", dir)
	if !strings.Contains(md, "`tenant-c.yaml` (stat_error)") {
		t.Errorf("md decode-stop report does not name the unreadable path:\n%s", md)
	}
}

// A --config-dir the walk cannot list at all is not an empty tree: exit 2
// with the reason, as served-values and effective refuse it — never the
// vacuously-safe 0 (#2588 review F1).
func TestUnreadable_UnlistableConfigDir_ExitsTwo(t *testing.T) {
	t.Parallel()
	if os.Geteuid() == 0 {
		t.Skip("running as root: chmod does not stop root")
	}
	dir := writeParityTree(t, unreadableBase())
	chmodT(t, dir, 0, 0o755)
	for _, args := range [][]string{
		{"--config-dir", dir},
		{"--config-dir", dir, "--format", "json"},
		{effectiveCmd, "--config-dir", dir},
		{servedValuesCmd, "--config-dir", dir},
	} {
		code, _, stderr := runOnce(t, args...)
		if code != exitCallerErr {
			t.Errorf("%v: exit = %d, want %d; stderr=%q", args, code, exitCallerErr, stderr)
		}
	}
	_, _, stderr := runOnce(t, "--config-dir", dir)
	if !strings.Contains(stderr, "cannot list configDir") {
		t.Errorf("guard stderr does not say why: %q", stderr)
	}
}
