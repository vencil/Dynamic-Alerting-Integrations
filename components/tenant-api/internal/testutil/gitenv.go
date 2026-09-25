package testutil

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// gitAutoMaintenanceOff is the config DisableGitAutoMaintenance writes.
var gitAutoMaintenanceOff = [][2]string{
	{"maintenance.auto", "false"},
	{"gc.auto", "0"},
	{"receive.autogc", "false"},
}

// DisableGitAutoMaintenance turns off git's automatic maintenance for every
// git process this test binary starts, directly or through the code under
// test, including the git processes those start in other repositories. Call
// it from TestMain before m.Run() in any package whose tests run git, and call
// the returned cleanup after m.Run() (before os.Exit).
//
// Why: after `git commit`, git 2.55 (the CI runners' version) can start
// `git maintenance run --auto` in the background, and `git receive-pack` runs
// its own auto gc in the repository being pushed to. Either process keeps
// writing under objects/ while t.TempDir's cleanup deletes the repo, so a test
// whose body passed fails with "TempDir RemoveAll cleanup: unlinkat
// .../objects: directory not empty". The same race hit the Python dry-run
// gate (PR #1966) and this module's handler tests (PR #1979, and again on the
// bare remote of a local-transport push).
//
// The settings go into a temporary global config file named by
// GIT_CONFIG_GLOBAL. GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n would not do: git
// strips those from the environment when it spawns receive-pack or
// upload-pack in the other repository of a local-transport push or fetch, so
// the bare remote's auto gc still ran. GIT_CONFIG_GLOBAL is not stripped. The
// file first includes whatever global config git read before, so a
// developer's or CI's own settings (user.*, safe.directory, ...) keep working;
// the settings above come after the include and win.
func DisableGitAutoMaintenance() (cleanup func(), err error) {
	var b strings.Builder
	for _, p := range effectiveGlobalConfigs() {
		if strings.ContainsAny(p, "\n\r") {
			return nil, fmt.Errorf("global git config path %q contains a newline", p)
		}
		fmt.Fprintf(&b, "[include]\n\tpath = %s\n", quoteConfigValue(p))
	}
	for _, kv := range gitAutoMaintenanceOff {
		section, key, _ := strings.Cut(kv[0], ".")
		fmt.Fprintf(&b, "[%s]\n\t%s = %s\n", section, key, kv[1])
	}

	dir, err := os.MkdirTemp("", "tenant-api-gitconfig-")
	if err != nil {
		return nil, err
	}
	cleanup = func() { _ = os.RemoveAll(dir) }
	file := filepath.Join(dir, "gitconfig")
	if err := os.WriteFile(file, []byte(b.String()), 0o600); err != nil {
		cleanup()
		return nil, err
	}
	if err := os.Setenv("GIT_CONFIG_GLOBAL", file); err != nil {
		cleanup()
		return nil, err
	}
	return cleanup, nil
}

// effectiveGlobalConfigs returns the global config files git reads when
// GIT_CONFIG_GLOBAL is left as it is, in the order git reads them (later
// wins). A set GIT_CONFIG_GLOBAL replaces both defaults; set to the empty
// string, git reads no global config at all. A listed file that does not
// exist is harmless: git skips an include whose target is missing.
func effectiveGlobalConfigs() []string {
	if v, ok := os.LookupEnv("GIT_CONFIG_GLOBAL"); ok {
		if v == "" {
			return nil
		}
		// git resolves a relative GIT_CONFIG_GLOBAL against its working
		// directory, but an include path against the including file.
		if !filepath.IsAbs(v) && !strings.HasPrefix(v, "~") {
			if abs, err := filepath.Abs(v); err == nil {
				v = abs
			}
		}
		return []string{v}
	}
	var paths []string
	home := os.Getenv("HOME")
	if xdg := os.Getenv("XDG_CONFIG_HOME"); xdg != "" {
		paths = append(paths, filepath.Join(xdg, "git", "config"))
	} else if home != "" {
		paths = append(paths, filepath.Join(home, ".config", "git", "config"))
	}
	if home != "" {
		paths = append(paths, filepath.Join(home, ".gitconfig"))
	}
	return paths
}

// quoteConfigValue renders s as a double-quoted git config value.
func quoteConfigValue(s string) string {
	s = strings.ReplaceAll(s, `\`, `\\`)
	s = strings.ReplaceAll(s, `"`, `\"`)
	return `"` + s + `"`
}
