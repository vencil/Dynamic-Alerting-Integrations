// da-batchpr is the CLI wrapper around internal/batchpr that closes
// the customer-runnable surface for the C-10 Batch PR Pipeline.
//
// v2.8.0 Phase .c C-10 PR-5 deliverable: turns the library
// (PR-1 planner, PR-2 apply, PR-3 refresh-after-base-merged,
// PR-4 refresh-after-source-rule-fix) into a single binary
// customers run from CI / a pre-merge hook / a one-off operator
// command.
//
// Subcommands
// -----------
//
//	apply           Open or update tenant chunk PRs from a Plan
//	                produced by C-9 emit + C-10 BuildPlan.
//	refresh         After Base PR merges, rebase tenant branches
//	                onto the merged main HEAD (PR-3 mode).
//	refresh-source  Re-apply data-layer hot-fix files into existing
//	                tenant branches (PR-4 mode).
//	--version       Print version and exit.
//	--help          Print usage and exit.
//
// JSON-input-first contract
// -------------------------
// Each subcommand reads its primary input as a JSON file (or
// stdin) and writes its result as JSON + a Markdown report. This
// keeps the CLI dumb and scriptable — the smart parts (parser,
// cluster, emit, cross-ref) live above batchpr in C-8 / C-9 and
// the Python da-tools wrapper. Convenience flags (e.g. `--base-
// merged-sha` for `refresh`) are deliberately deferred to a future
// CLI-polish PR; today, customers serialise their batchpr.*Input
// to JSON and feed it in.
//
// Exit codes (stable contract for CI YAML / hook scripts):
//
//	0  clean run, all targets succeeded or skipped acceptably
//	1  one or more targets failed (per-target Failed status)
//	2  caller error (bad flags, missing/invalid path, IO failure)
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"log"
	"os"
	"path/filepath"
	"strings"

	"github.com/vencil/threshold-exporter/internal/batchpr"
)

// Version is overridden at build time via `-ldflags "-X main.Version=..."`.
var Version = "dev"

// programName is the binary name printed in usage / errors. Kept
// in a var so tests can swap it without disturbing os.Args[0].
var programName = "da-batchpr"

// exit codes — referenced from tests too.
const (
	exitOK        = 0
	exitFailures  = 1
	exitCallerErr = 2
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run is the testable entry point. Routes to the right subcommand
// dispatcher based on args[0]. Returns an exit code; main() passes
// it to os.Exit.
func run(args []string, stdout, errOut io.Writer) int {
	// Force log output to errOut so the report stream stays clean
	// when subcommands write reports to stdout.
	log.SetOutput(errOut)
	log.SetFlags(0)

	if len(args) < 1 {
		printUsage(errOut)
		return exitCallerErr
	}
	sub := args[0]
	rest := args[1:]
	switch sub {
	case "apply":
		return cmdApply(rest, stdout, errOut)
	case "refresh":
		return cmdRefresh(rest, stdout, errOut)
	case "refresh-source":
		return cmdRefreshSource(rest, stdout, errOut)
	case "--version", "-v":
		fmt.Fprintf(stdout, "%s %s\n", programName, Version)
		return exitOK
	case "--help", "-h", "help":
		printUsage(stdout)
		return exitOK
	default:
		fmt.Fprintf(errOut, "%s: unknown subcommand %q\n", programName, sub)
		printUsage(errOut)
		return exitCallerErr
	}
}

// printUsage writes the top-level usage to w. Each subcommand has
// its own --help; this is just the dispatcher entry.
func printUsage(w io.Writer) {
	fmt.Fprintf(w, "Usage: %s <subcommand> [flags]\n\n", programName)
	fmt.Fprintf(w, "Subcommands:\n")
	fmt.Fprintf(w, "  apply           Open or update tenant chunk PRs from a Plan.\n")
	fmt.Fprintf(w, "  refresh         Rebase tenant branches after Base PR merges.\n")
	fmt.Fprintf(w, "  refresh-source  Re-apply data-layer hot-fix into tenant branches.\n")
	fmt.Fprintf(w, "  --version       Print version and exit.\n")
	fmt.Fprintf(w, "  --help          Print this usage and exit.\n\n")
	fmt.Fprintf(w, "Run '%s <subcommand> --help' for subcommand flags.\n", programName)
	fmt.Fprintf(w, "\nExit codes:\n  0  clean\n  1  per-target failures\n  2  caller error\n")
}

// --- Shared helpers ---------------------------------------------

// readInputJSON reads a JSON document from `path` and unmarshals
// into out. `path` of "-" or "" reads from stdin (the latter
// matches the convention "no flag means stdin").
func readInputJSON(path string, stdin io.Reader, out interface{}) error {
	var r io.Reader
	switch path {
	case "", "-":
		r = stdin
	default:
		f, err := os.Open(path)
		if err != nil {
			return fmt.Errorf("open %q: %w", path, err)
		}
		defer f.Close()
		r = f
	}
	dec := json.NewDecoder(r)
	dec.DisallowUnknownFields()
	if err := dec.Decode(out); err != nil {
		return fmt.Errorf("parse JSON: %w", err)
	}
	return nil
}

// writeJSON marshals v as indented JSON to `path`. `path` of "-" or
// "" writes to stdout. Trailing newline appended for terminal-
// friendly output.
func writeJSON(path string, stdout io.Writer, v interface{}) error {
	body, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal JSON: %w", err)
	}
	body = append(body, '\n')
	switch path {
	case "", "-":
		_, err = stdout.Write(body)
		return err
	default:
		return os.WriteFile(path, body, 0o644)
	}
}

// writeReport writes `body` to `path` or stdout. `path` of "-" or
// "" writes to stdout.
func writeReport(path string, stdout io.Writer, body string) error {
	switch path {
	case "", "-":
		_, err := io.WriteString(stdout, body)
		return err
	default:
		return os.WriteFile(path, []byte(body), 0o644)
	}
}

// walkFilesDir walks `root` recursively and returns a map of
// repo-relative path → file bytes. Used by the apply subcommand to
// load C-9 emit output, and by refresh-source to load patch files.
//
// `root` MUST exist and be a directory; missing/file → error.
// Symlinks are followed (filepath.Walk semantics).
//
// Path normalisation: returned keys use forward slashes regardless
// of host OS (matches the rest of the batchpr / config packages).
// The leading `root` segment is trimmed.
//
// `prefix` is prepended to each returned key (after the root trim);
// pass "" for the canonical "files keyed by their path under root"
// behaviour. Apply uses prefix="" because the per-item subdir IS
// the natural prefix; refresh-source uses prefix="" within each
// per-PR subdir.
func walkFilesDir(root, prefix string) (map[string][]byte, error) {
	info, err := os.Stat(root)
	if err != nil {
		return nil, fmt.Errorf("stat %q: %w", root, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("%q is not a directory", root)
	}
	out := make(map[string][]byte)
	err = filepath.Walk(root, func(path string, info fs.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if info.IsDir() {
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		// Normalise to forward-slash keys.
		rel = filepath.ToSlash(rel)
		if prefix != "" {
			rel = prefix + "/" + rel
		}
		body, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("read %q: %w", path, err)
		}
		out[rel] = body
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// makeClients constructs production GitClient + PRClient from the
// shared --workdir and --repo flags. Tests inject fakes directly
// into the run<X> functions; production cmd<X> dispatchers call
// this helper.
func makeClients(workdir string, repo batchpr.Repo) (batchpr.GitClient, batchpr.PRClient, error) {
	if workdir == "" {
		return nil, nil, fmt.Errorf("--workdir is required (path to the local clone of the target repo)")
	}
	if _, err := os.Stat(workdir); err != nil {
		return nil, nil, fmt.Errorf("--workdir %q: %w", workdir, err)
	}
	if repo.FullName() == "" {
		return nil, nil, fmt.Errorf("--repo (owner/name) is required")
	}
	if repo.BaseBranch == "" {
		return nil, nil, fmt.Errorf("--base-branch is required")
	}
	return batchpr.NewShellGitClient(workdir),
		batchpr.NewGHPRClient(repo),
		nil
}

// parseRepoFlag parses the conventional "owner/name" string into a
// Repo, leaving BaseBranch empty for the caller to fill from
// --base-branch.
func parseRepoFlag(s string) (batchpr.Repo, error) {
	parts := strings.SplitN(s, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return batchpr.Repo{}, fmt.Errorf("--repo must be 'owner/name' (got %q)", s)
	}
	return batchpr.Repo{Owner: parts[0], Name: parts[1]}, nil
}

// exitCodeForSummary maps a result summary to an exit code.
// Returns exitFailures iff at least one target ended up Failed.
// Skipped / NoChange / DryRun / Clean / Updated all count as
// success for exit purposes.
func exitCodeForApply(s batchpr.ApplySummary) int {
	if s.FailedCount > 0 {
		return exitFailures
	}
	return exitOK
}

func exitCodeForRefresh(s batchpr.RefreshSummary) int {
	if s.FailedCount > 0 || s.ConflictsCount > 0 {
		// Conflicts count as a failure exit-wise: a CI hook
		// running this should NOT treat "conflicts present" as a
		// green run — the human still needs to do the manual
		// rebase per refresh-report.md.
		return exitFailures
	}
	return exitOK
}

func exitCodeForRefreshSource(s batchpr.RefreshSourceSummary) int {
	if s.FailedCount > 0 {
		return exitFailures
	}
	return exitOK
}
