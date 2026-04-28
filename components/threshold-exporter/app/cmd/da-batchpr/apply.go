package main

// `da-batchpr apply` — open or update tenant chunk PRs from a Plan.
//
// Input contract:
//
//   --plan plan.json        # Plan JSON (from BuildPlan)
//   --emit-dir ./emit/      # C-9 emit output (recursive walk).
//                           # Files are bucketed per Plan.Items[i] via
//                           # batchpr.AllocateFiles before Apply.
//   --repo owner/name       # GitHub repo
//   --base-branch main      # Default: main
//   --branch-prefix p/      # Optional; empty → batchpr default
//   --commit-author "Name <email>"   # Optional; empty → git config
//   --workdir ./repo        # Local clone (CWD for git ops)
//   --dry-run               # Run orchestration without remote ops
//   --inter-call-delay-ms N # Soften GitHub secondary rate limits
//   --report apply-report.md # Markdown report (default: stdout)
//   --result-json out.json   # Full ApplyResult JSON (default: -)
//
// Output: report markdown + JSON result. Exit 0 if every item
// ended in {Created, SkippedExisting, DryRun, EmptyFiles}; exit 1
// if any item is Failed; exit 2 on caller errors.

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/vencil/threshold-exporter/internal/batchpr"
)

// applyFlags is the parsed configuration for one apply run.
type applyFlags struct {
	planPath             string
	emitDir              string
	repoFlag             string
	baseBranch           string
	branchPrefix         string
	commitAuthor         string
	workdir              string
	dryRun               bool
	interCallDelayMillis int
	reportPath           string
	resultJSONPath       string
	help                 bool
}

func parseApplyFlags(args []string, errOut io.Writer) (*applyFlags, error) {
	fs := flag.NewFlagSet(programName+" apply", flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &applyFlags{}

	fs.StringVar(&f.planPath, "plan", "", "Path to a Plan JSON file (from BuildPlan). Required.")
	fs.StringVar(&f.emitDir, "emit-dir", "", "C-9 emit output directory (recursive walk). Required.")
	fs.StringVar(&f.repoFlag, "repo", "", "GitHub repo, 'owner/name'. Required.")
	fs.StringVar(&f.baseBranch, "base-branch", "main", "Branch new PRs target.")
	fs.StringVar(&f.branchPrefix, "branch-prefix", "", "Prefix for generated branches; empty → batchpr default.")
	fs.StringVar(&f.commitAuthor, "commit-author", "", "Author for branch commits, 'Name <email>'. Empty → git config.")
	fs.StringVar(&f.workdir, "workdir", "", "Local clone of the target repo (CWD for git ops). Required.")
	fs.BoolVar(&f.dryRun, "dry-run", false, "Run orchestration without git or GitHub API calls.")
	fs.IntVar(&f.interCallDelayMillis, "inter-call-delay-ms", 0, "Per-item delay (ms) between OpenPR calls.")
	fs.StringVar(&f.reportPath, "report", "-", "Write the human-readable report to this file ('-' = stdout).")
	fs.StringVar(&f.resultJSONPath, "result-json", "-", "Write the JSON ApplyResult to this file ('-' = stdout).")
	fs.BoolVar(&f.help, "help", false, "Print usage and exit.")
	fs.BoolVar(&f.help, "h", false, "Alias for --help.")

	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s apply [flags]\n", programName)
		fmt.Fprintf(errOut, "Open or update tenant chunk PRs from a C-10 Plan.\n\n")
		fs.PrintDefaults()
	}

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
}

// cmdApply is the production dispatcher: parses flags, constructs
// production clients, hands off to runApply (the testable core).
func cmdApply(args []string, stdout, errOut io.Writer) int {
	f, err := parseApplyFlags(args, errOut)
	if err != nil {
		return exitCallerErr
	}
	if f.help {
		return exitOK
	}
	if f.planPath == "" {
		fmt.Fprintf(errOut, "%s apply: --plan is required\n", programName)
		return exitCallerErr
	}
	if f.emitDir == "" {
		fmt.Fprintf(errOut, "%s apply: --emit-dir is required\n", programName)
		return exitCallerErr
	}
	repo, err := parseRepoFlag(f.repoFlag)
	if err != nil {
		fmt.Fprintf(errOut, "%s apply: %v\n", programName, err)
		return exitCallerErr
	}
	repo.BaseBranch = f.baseBranch
	git, pr, err := makeClients(f.workdir, repo)
	if err != nil {
		fmt.Fprintf(errOut, "%s apply: %v\n", programName, err)
		return exitCallerErr
	}
	return runApply(f, repo, stdout, errOut, git, pr, os.Stdin)
}

// runApply is the testable entry point. Production passes
// ShellGitClient + GHPRClient; tests pass in-memory fakes.
func runApply(f *applyFlags, repo batchpr.Repo, stdout, errOut io.Writer, git batchpr.GitClient, pr batchpr.PRClient, stdin io.Reader) int {
	plan := &batchpr.Plan{}
	if err := readInputJSON(f.planPath, stdin, plan); err != nil {
		fmt.Fprintf(errOut, "%s apply: %v\n", programName, err)
		return exitCallerErr
	}
	if len(plan.Items) == 0 {
		fmt.Fprintf(errOut, "%s apply: Plan has zero items\n", programName)
		return exitCallerErr
	}

	files, err := walkFilesDir(f.emitDir, "")
	if err != nil {
		fmt.Fprintf(errOut, "%s apply: read --emit-dir: %v\n", programName, err)
		return exitCallerErr
	}

	itemFiles, allocWarnings := batchpr.AllocateFiles(plan, files)

	in := batchpr.ApplyInput{
		Plan:                 plan,
		ItemFiles:            itemFiles,
		Repo:                 repo,
		BranchPrefix:         f.branchPrefix,
		CommitAuthor:         f.commitAuthor,
		DryRun:               f.dryRun,
		InterCallDelayMillis: f.interCallDelayMillis,
	}
	result, err := batchpr.Apply(context.Background(), in, git, pr)
	if err != nil {
		fmt.Fprintf(errOut, "%s apply: %v\n", programName, err)
		return exitCallerErr
	}
	// Merge AllocateFiles warnings into the result so the report +
	// JSON output surface them alongside per-item Status. Prepend
	// (not append) so the chronological order is preserved:
	// allocation runs BEFORE Apply, so its warnings come first.
	if len(allocWarnings) > 0 {
		result.Warnings = append(allocWarnings, result.Warnings...)
	}

	if err := writeReport(f.reportPath, stdout, renderApplyReport(repo, in, result)); err != nil {
		fmt.Fprintf(errOut, "%s apply: write report: %v\n", programName, err)
		return exitCallerErr
	}
	if err := writeJSON(f.resultJSONPath, stdout, result); err != nil {
		fmt.Fprintf(errOut, "%s apply: write result JSON: %v\n", programName, err)
		return exitCallerErr
	}
	return exitCodeForApply(result.Summary)
}

// renderApplyReport produces the customer-facing Markdown summary
// for an Apply run. Mirrors the shape of the Refresh / RefreshSource
// reports for consistency: header → summary counts → per-item table
// → warnings. ApplyResult itself doesn't carry a ReportMarkdown
// field (the PR-2 contract; preserved here to keep this PR's diff
// focused on the CLI surface).
func renderApplyReport(repo batchpr.Repo, in batchpr.ApplyInput, r *batchpr.ApplyResult) string {
	out := strings.Builder{}
	out.WriteString("# Apply report\n\n")
	out.WriteString(fmt.Sprintf("**Repo**: `%s` (base branch: `%s`)\n", repo.FullName(), repo.BaseBranch))
	if in.BranchPrefix != "" {
		out.WriteString(fmt.Sprintf("**Branch prefix**: `%s`\n", in.BranchPrefix))
	}
	if in.DryRun {
		out.WriteString("**Mode**: dry-run (no git or GitHub API calls executed)\n")
	}
	if r.BasePRNumber > 0 {
		out.WriteString(fmt.Sprintf("**Base PR**: #%d\n", r.BasePRNumber))
	}
	out.WriteString("\n")

	out.WriteString("## Summary\n\n")
	out.WriteString(fmt.Sprintf("- Total items: %d\n", r.Summary.TotalItems))
	out.WriteString(fmt.Sprintf("- Created: %d\n", r.Summary.CreatedCount))
	out.WriteString(fmt.Sprintf("- Skipped (existing): %d\n", r.Summary.SkippedExistingCount))
	if r.Summary.DryRunCount > 0 {
		out.WriteString(fmt.Sprintf("- Dry-run: %d\n", r.Summary.DryRunCount))
	}
	if r.Summary.EmptyFilesCount > 0 {
		out.WriteString(fmt.Sprintf("- Empty files (skipped): %d\n", r.Summary.EmptyFilesCount))
	}
	if r.Summary.FailedCount > 0 {
		out.WriteString(fmt.Sprintf("- Failed: %d\n", r.Summary.FailedCount))
	}
	if r.Summary.BasePlaceholderRewrites > 0 {
		out.WriteString(fmt.Sprintf("- Base placeholder rewrites: %d\n", r.Summary.BasePlaceholderRewrites))
	}
	out.WriteString("\n")

	out.WriteString("## Per-item outcomes\n\n")
	out.WriteString("| # | Kind | Branch | Status | PR | Notes |\n")
	out.WriteString("|---|------|--------|--------|----|-------|\n")
	for _, it := range r.Items {
		notes := "—"
		if it.ErrorMessage != "" {
			notes = it.ErrorMessage
		}
		prCol := "—"
		if it.PRNumber > 0 {
			prCol = fmt.Sprintf("[#%d](%s)", it.PRNumber, it.PRURL)
		}
		out.WriteString(fmt.Sprintf("| %d | %s | `%s` | %s | %s | %s |\n",
			it.PlanItemIndex, it.Kind, it.BranchName, it.Status, prCol, notes))
	}
	out.WriteString("\n")

	if len(r.Warnings) > 0 {
		out.WriteString("## Warnings\n\n")
		for _, w := range r.Warnings {
			out.WriteString(fmt.Sprintf("- %s\n", w))
		}
		out.WriteString("\n")
	}
	return out.String()
}
