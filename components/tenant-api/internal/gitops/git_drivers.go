package gitops

// #3175: git runs a program the conf repo's git config names whenever it
// handles a file whose .gitattributes picks that program — a clean filter on
// `git add` and `git status`, a smudge filter on `git checkout`, a textconv or
// external diff driver on `git diff` / `git log -p`, a merge driver on a
// merge. The program comes from git config (operator-controlled), but which
// files it runs on comes from .gitattributes, which anyone who can commit to
// the conf repo controls; and since #3176 `git status` runs for a preview
// (POST /tenants/{id}/diff, /validate), which only needs read permission. So a
// caller with read permission could make tenant-api run whatever the config
// names, as often as they like.
//
// tenant-api therefore refuses to run against a repository whose git config
// names any such program (gitDriverKeyPattern), from whichever scope: the
// system and global files, the repository's own, a worktree's, a file pulled
// in by include / includeIf, or GIT_CONFIG_COUNT / GIT_CONFIG_PARAMETERS
// ("command"). There is no allow list: a conf repo that needs one (git-lfs,
// git-crypt) cannot be served by tenant-api. Neutralising the drivers with
// `-c filter.<name>.clean=` was rejected (#3175): git would then commit the
// working-tree bytes (an lfs pointer's or git-crypt's plaintext, not what the
// filter would store) and report every filtered file as modified.
//
// Hooks (gitcmd.go) are a different trust decision: they run on tenant-api's
// own commands only, from a directory the operator controls, never chosen by
// a commit. They stay supported.
//
// WHERE IT IS CHECKED: at startup (main calls CheckGitDrivers before any
// other git command), and again before every git command a request can
// cause — every write (lockTreeRecovered, the first step under the writer
// lock, and again after every branch switch: checkGitDriversIn), the
// write preview (PreviewPut) and the PR-mode tree check (TreeOnBase) —
// because the config can change while tenant-api runs (an
// init container or a sidecar editing .git/config, a mounted global config
// replaced).
//
// THE CHECK RUNS NOTHING ELSE: `git config --get-regexp` reads config files
// and prints entries; it runs no filter, diff or merge driver, and no
// core.fsmonitor hook (and every tenant-api git command passes
// core.fsmonitor=false anyway, gitcmd.go). TestGitDrivers_CheckRunsNoDriver
// pins that.

import (
	"errors"
	"fmt"
	"os/exec"
	"strings"
)

// gitDriverKeyPattern matches the git config keys that name a program git
// runs on file contents chosen by .gitattributes. git matches it against the
// canonical key: section and variable lower-cased, the subsection (the
// driver's name) as written.
const gitDriverKeyPattern = `^(filter\..+\.(clean|smudge|process)|diff\..+\.(textconv|command)|merge\..+\.driver)$`

// ErrGitDriverConfigured marks (errors.Is) a refusal because the git config
// tenant-api runs under names a program git would run on file contents
// (GitDriverError), or because that could not be checked.
var ErrGitDriverConfigured = errors.New("git config names a program git runs on file contents")

// GitConfigEntry is one git config key found, with the scope git read it
// from (system, global, local, worktree, command).
type GitConfigEntry struct {
	Scope string
	Key   string
}

// GitDriverError is the refusal: the keys found and their scopes. It
// carries no value — a value is a command line, which can hold a path or a
// credential — so its text can go to the log and to stderr at startup.
// The text is never shown to an API client (the handlers answer a fixed
// INTERNAL_ERROR).
type GitDriverError struct {
	Found []GitConfigEntry
}

func (e *GitDriverError) Error() string {
	parts := make([]string, 0, len(e.Found))
	for _, f := range e.Found {
		parts = append(parts, f.Scope+" "+f.Key)
	}
	return "git config names programs git would run on conf repo files, which tenant-api refuses to run (#3175): " +
		strings.Join(parts, ", ") +
		" — remove these keys from that scope's git config (values not shown); there is no allow list"
}

// Is makes a GitDriverError ErrGitDriverConfigured.
func (e *GitDriverError) Is(target error) bool { return target == ErrGitDriverConfigured }

// CheckGitDrivers refuses (ErrGitDriverConfigured) when the git config that
// tenant-api's git commands in the conf repository read names a filter,
// diff or merge driver program (see the file comment). It runs git as every
// other tenant-api git command does — same binary, same environment, from
// the repository — so it sees what they would. Failing to read the config
// at all refuses too: what cannot be read cannot be shown to be clean.
func (w *Writer) CheckGitDrivers() error {
	return w.checkGitDriversIn(w.gitDir)
}

// checkGitDriversIn is CheckGitDrivers for the git context of dir, as git
// will be in when it runs there (F1 of #3175's review). A conditional
// include decides by that context: `includeIf "onbranch:…"` by the branch
// HEAD names, `includeIf "gitdir:…"` by the repository directory — a
// linked worktree has its own. The check under the lock covers the context
// HEAD is in when the lock is taken, which need not be base (a PR branch a
// failed return left behind, an operator's checkout). So the check runs
// again wherever tenant-api changes the context, before git runs on files
// in the new one: in the serving tree after every move to base
// (checkoutBaseClean: a PR write anchoring on base, its return and its
// abort, the direct-mode return from a stranded PR branch), right after a
// PR write's `checkout -b` (WritePRChecked), and inside a batch's staging
// worktree after `worktree add --no-checkout`, before its index or files
// are populated (openStaging).
func (w *Writer) checkGitDriversIn(dir string) error {
	out, err := w.gitOutput(dir, "config", "-z", "--show-scope", "--get-regexp", gitDriverKeyPattern)
	if err != nil {
		// Exit status 1: no key matched. Anything else — an unreadable or
		// malformed config file, a missing directory, a timeout — is a
		// config tenant-api cannot vouch for.
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) && exitErr.ExitCode() == 1 {
			return nil
		}
		return fmt.Errorf("%w: cannot read the git config to check it: %v", ErrGitDriverConfigured, err)
	}
	found := parseGitConfigScopes(out)
	if len(found) == 0 {
		// Exit 0 with nothing parseable is not a "no match".
		return fmt.Errorf("%w: git config matched but printed no key", ErrGitDriverConfigured)
	}
	return &GitDriverError{Found: found}
}

// parseGitConfigScopes reads `git config -z --show-scope --get-regexp`
// output: "<scope>\0<key>\n<value>\0" per entry, or "<scope>\0<key>\0"
// for a key written without "=". The value is dropped here, unread.
func parseGitConfigScopes(out []byte) []GitConfigEntry {
	var found []GitConfigEntry
	fields := strings.Split(string(out), "\x00")
	for i := 0; i+1 < len(fields); i += 2 {
		key, _, _ := strings.Cut(fields[i+1], "\n")
		if fields[i] == "" || key == "" {
			continue
		}
		found = append(found, GitConfigEntry{Scope: fields[i], Key: key})
	}
	return found
}
