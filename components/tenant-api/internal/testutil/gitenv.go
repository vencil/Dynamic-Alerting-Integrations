package testutil

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
)

// gitAutoMaintenanceOff is the config DisableGitAutoMaintenance writes.
var gitAutoMaintenanceOff = [][2]string{
	{"maintenance.auto", "false"},
	{"gc.auto", "0"},
	{"receive.autogc", "false"},
}

// gitDriverKey matches the git config keys tenant-api refuses to run with
// (#3175; gitops.gitDriverKeyPattern — not imported: gitops's tests import
// this package). git reports them with section and variable lower-cased.
var gitDriverKey = regexp.MustCompile(`^(filter\..+\.(clean|smudge|process)|diff\..+\.(textconv|command)|merge\..+\.driver)$`)

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
// the bare remote's auto gc still ran. GIT_CONFIG_GLOBAL is not stripped.
//
// The file first holds a copy of the system and global config git read
// before (includes resolved), so a developer's or CI's own settings
// (user.*, safe.directory, ...) keep working; the settings above come after
// the copy and win. GIT_CONFIG_NOSYSTEM is set, so the system file is read
// only through the copy.
//
// The copy leaves out the filter / diff / merge driver keys tenant-api
// refuses to start with (#3175): a host with git-lfs installed has
// filter.lfs.* in its system or global config, and every Writer a test
// builds would otherwise refuse every write. A test about those keys sets
// them itself.
func DisableGitAutoMaintenance() (cleanup func(), err error) {
	dir, err := os.MkdirTemp("", "tenant-api-gitconfig-")
	if err != nil {
		return nil, err
	}
	cleanup = func() { _ = os.RemoveAll(dir) }

	copied, err := driverFreeConfigCopy(dir)
	if err != nil {
		cleanup()
		return nil, err
	}
	var b strings.Builder
	b.WriteString(copied)
	for _, kv := range gitAutoMaintenanceOff {
		section, key, _ := strings.Cut(kv[0], ".")
		fmt.Fprintf(&b, "[%s]\n\t%s = %s\n", section, key, kv[1])
	}
	file := filepath.Join(dir, "gitconfig")
	if err := os.WriteFile(file, []byte(b.String()), 0o600); err != nil {
		cleanup()
		return nil, err
	}
	if err := os.Setenv("GIT_CONFIG_GLOBAL", file); err != nil {
		cleanup()
		return nil, err
	}
	if err := os.Setenv("GIT_CONFIG_NOSYSTEM", "1"); err != nil {
		cleanup()
		return nil, err
	}
	return cleanup, nil
}

// driverFreeConfigCopy renders, as config file text, every system and
// global entry git reads now — in the order it reads them, includes
// resolved — except the driver keys (gitDriverKey). git lists them from
// outside any repository (dir, with discovery stopped above it), so no
// repository's own config is copied; GIT_CONFIG_GLOBAL, an empty one
// included, and GIT_CONFIG_NOSYSTEM are honoured as git honours them.
func driverFreeConfigCopy(dir string) (string, error) {
	cmd := exec.Command("git", "-C", dir, "config", "--show-scope", "--includes", "-z", "--list")
	cmd.Env = append(os.Environ(), "GIT_CEILING_DIRECTORIES="+filepath.Dir(dir))
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("list the git config to copy: %v: %s", err, stderr.String())
	}
	// -z with --show-scope: "<scope>\0<key>\n<value>\0", or
	// "<scope>\0<key>\0" for a key written without "=".
	fields := strings.Split(string(out), "\x00")
	if len(fields)%2 != 1 || fields[len(fields)-1] != "" {
		return "", fmt.Errorf("unexpected git config listing %q", out)
	}
	var b strings.Builder
	for i := 0; i+1 < len(fields); i += 2 {
		scope, rest := fields[i], fields[i+1]
		if scope != "system" && scope != "global" {
			continue // command (GIT_CONFIG_COUNT …) stays in the environment
		}
		key, value, hasValue := strings.Cut(rest, "\n")
		if gitDriverKey.MatchString(key) {
			continue
		}
		section, sub, name, ok := splitConfigKey(key)
		if !ok {
			return "", fmt.Errorf("unexpected git config key %q", key)
		}
		if sub != "" {
			fmt.Fprintf(&b, "[%s %s]\n", section, quoteConfigValue(sub))
		} else {
			fmt.Fprintf(&b, "[%s]\n", section)
		}
		if hasValue {
			fmt.Fprintf(&b, "\t%s = %s\n", name, quoteConfigValue(value))
		} else {
			fmt.Fprintf(&b, "\t%s\n", name) // a key without "=": boolean true
		}
	}
	return b.String(), nil
}

// splitConfigKey splits a canonical git config key into its section, its
// subsection ("" when none; it may hold dots) and its variable name.
func splitConfigKey(key string) (section, sub, name string, ok bool) {
	first, last := strings.Index(key, "."), strings.LastIndex(key, ".")
	if first <= 0 || last == len(key)-1 {
		return "", "", "", false
	}
	section, name = key[:first], key[last+1:]
	if last > first {
		sub = key[first+1 : last]
	}
	return section, sub, name, true
}

// quoteConfigValue renders s as a double-quoted git config value (or
// subsection name).
func quoteConfigValue(s string) string {
	s = strings.ReplaceAll(s, `\`, `\\`)
	s = strings.ReplaceAll(s, `"`, `\"`)
	s = strings.ReplaceAll(s, "\n", `\n`)
	s = strings.ReplaceAll(s, "\t", `\t`)
	return `"` + s + `"`
}
