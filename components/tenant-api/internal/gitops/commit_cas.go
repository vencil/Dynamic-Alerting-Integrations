package gitops

// The direct single-file commit bound to the generator's verdict (#2903,
// ADR-036 §4 "驗的和寫的必須是同一份"). The generator judged the tree at
// judgedBase; w.mu keeps every in-process write out, but a commit made from
// outside the process can land at any time. So the commit is made as an
// object whose parent is judgedBase, and HEAD is advanced onto it with a
// compare-and-swap (`update-ref HEAD <new> <judgedBase>`, as publishBatch
// does): if HEAD is no longer judgedBase, nothing is published.

import (
	"fmt"
	"log/slog"
	"strings"
	"time"
)

// tenantCommitMessage is the message of a tenant write's commit. trailer
// (optional) is appended to the body so an audit annotation (e.g. an
// admission-validator --force bypass) is permanently bound to the commit,
// not just an ephemeral log line.
func tenantCommitMessage(tenantID string, trailer ...string) string {
	msg := fmt.Sprintf("tenant/%s: update via portal\n\nTimestamp: %s\nSource: da-portal/tenant-manager",
		tenantID, time.Now().UTC().Format(time.RFC3339))
	if len(trailer) > 0 && trailer[0] != "" {
		msg += "\n\n" + trailer[0]
	}
	return msg
}

// commitIdentity is the author name and the committer of a write by
// authorEmail. The author name defaults to the email's local part; the
// committer is the service account from GIT_COMMITTER_NAME /
// GIT_COMMITTER_EMAIL (set in the K8s Deployment), falling back to the
// author (dev/local mode).
func (w *Writer) commitIdentity(authorEmail string) (authorName, committerName, committerEmail string) {
	authorName = authorEmail
	if at := strings.Index(authorEmail, "@"); at > 0 {
		authorName = authorEmail[:at]
	}
	committerName, committerEmail = w.committerName, w.committerEmail
	if committerName == "" {
		committerName = authorName
	}
	if committerEmail == "" {
		committerEmail = authorEmail
	}
	return authorName, committerName, committerEmail
}

// stagePaths stages every path in paths in dir in one `git add` (so the
// index never holds part of them for longer than this command), holds the
// staged blobs to validated (checkStagedAsValidated; on a refusal the paths
// are unstaged), and reports whether anything differs from HEAD.
func (w *Writer) stagePaths(dir string, paths []string, validated map[string][]byte) (changed bool, err error) {
	addArgs := append([]string{"-C", dir, "add", "--"}, paths...)
	addCmd, addCtx, addCancel := w.gitCmd(addArgs...)
	defer addCancel()
	if out, err := addCmd.CombinedOutput(); err != nil {
		return false, w.gitErr(addCtx, "add", err, out)
	}
	if err := w.checkStagedAsValidated(dir, validated); err != nil {
		if uerr := w.gitIn(dir, append([]string{"reset", "-q", "--"}, paths...)...); uerr != nil {
			slog.Warn("gitops: could not unstage a refused write", "error", uerr)
		}
		return false, err
	}
	// Exit 0 means nothing is staged — nothing to commit.
	statusCmd, _, statusCancel := w.gitCmd("-C", dir, "diff", "--cached", "--quiet")
	defer statusCancel()
	return statusCmd.Run() != nil, nil
}

// gitCommitOnto is gitCommit for a write the generator judged on base: it
// stages filePath (its blob held to validated), writes the commit object
// with base as its parent and advances HEAD onto it only if HEAD is still
// base. committed is false when nothing was staged. ErrConflict: HEAD moved
// — nothing was published; the caller puts the serving tree back
// (restoreFromHEAD). Caller holds w.mu.
func (w *Writer) gitCommitOnto(filePath, tenantID, authorEmail string, validated []byte, base string, trailer ...string) (committed bool, err error) {
	changed, err := w.stagePaths(w.gitDir, []string{filePath}, map[string][]byte{filePath: validated})
	if err != nil || !changed {
		return false, err
	}
	treeCmd, treeCtx, treeCancel := w.gitCmd("-C", w.gitDir, "write-tree")
	defer treeCancel()
	tree, err := treeCmd.Output()
	if err != nil {
		return false, w.gitErr(treeCtx, "write-tree", err, tree)
	}

	authorName, committerName, committerEmail := w.commitIdentity(authorEmail)
	ctCmd, ctCtx, ctCancel := w.gitCmd("-C", w.gitDir, "commit-tree", strings.TrimSpace(string(tree)),
		"-p", base, "-m", tenantCommitMessage(tenantID, trailer...))
	defer ctCancel()
	ctCmd.Env = append(ctCmd.Env, // keeps gitEnv: no inherited pathspec setting
		"GIT_AUTHOR_NAME="+authorName, "GIT_AUTHOR_EMAIL="+authorEmail,
		"GIT_COMMITTER_NAME="+committerName, "GIT_COMMITTER_EMAIL="+committerEmail)
	out, err := ctCmd.Output()
	if err != nil {
		return false, w.gitErr(ctCtx, "commit-tree", err, out)
	}
	commit := strings.TrimSpace(string(out))

	if w.beforeCASAdvance != nil {
		w.beforeCASAdvance()
	}
	if err := w.gitExec("update-ref", "-m", "tenant-api: tenant/"+tenantID, "HEAD", commit, base); err != nil {
		now, herr := w.currentHEAD()
		if herr == nil && now != base {
			slog.Warn("gitops: HEAD moved before the judged write was published",
				"tenant", tenantID, "judged_base", base, "head", now)
			return false, ErrConflict
		}
		return false, fmt.Errorf("advance HEAD: %w", err)
	}
	return true, nil
}
