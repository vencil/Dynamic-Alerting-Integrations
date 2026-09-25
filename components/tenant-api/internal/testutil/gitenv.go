package testutil

import (
	"fmt"
	"os"
	"strconv"
)

// gitAutoMaintenanceOff is the config DisableGitAutoMaintenance appends.
var gitAutoMaintenanceOff = [][2]string{
	{"maintenance.auto", "false"},
	{"gc.auto", "0"},
}

// DisableGitAutoMaintenance turns off git's automatic maintenance for every
// git process this test binary starts, directly or through the code under
// test. Call it from TestMain before m.Run() in any package whose tests run
// git.
//
// Why: after `git commit`, git 2.55 (the CI runners' version) can start
// `git maintenance run --auto` in the background. That process keeps writing
// under .git/objects while t.TempDir's cleanup deletes the repo, so a test
// whose body passed fails with "TempDir RemoveAll cleanup: unlinkat
// .../.git/objects: directory not empty". The same race hit the Python
// dry-run gate (PR #1966) and this module's handler tests (PR #1979).
//
// The settings go through GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n /
// GIT_CONFIG_VALUE_n rather than `-c` flags so that they also reach git
// commands the production code builds; gitops runs git with the inherited
// environment. Entries already present in the environment are kept: the new
// ones are appended after them.
func DisableGitAutoMaintenance() error {
	n := 0
	if v := os.Getenv("GIT_CONFIG_COUNT"); v != "" {
		c, err := strconv.Atoi(v)
		if err != nil || c < 0 {
			return fmt.Errorf("GIT_CONFIG_COUNT=%q is not a non-negative integer", v)
		}
		n = c
	}
	for i, kv := range gitAutoMaintenanceOff {
		if err := os.Setenv(fmt.Sprintf("GIT_CONFIG_KEY_%d", n+i), kv[0]); err != nil {
			return err
		}
		if err := os.Setenv(fmt.Sprintf("GIT_CONFIG_VALUE_%d", n+i), kv[1]); err != nil {
			return err
		}
	}
	return os.Setenv("GIT_CONFIG_COUNT", strconv.Itoa(n+len(gitAutoMaintenanceOff)))
}
