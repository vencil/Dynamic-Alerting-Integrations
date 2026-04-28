package main

// `da-batchpr refresh` — rebase tenant branches after Base PR merges
// (PR-3 mode).
//
// Input contract:
//
//   --input refresh.json    # batchpr.RefreshInput JSON (default: stdin).
//                           # All fields directly serialise — no separate
//                           # files-dir needed (refresh doesn't write files).
//   --workdir ./repo        # Local clone (CWD for git ops). Required.
//   --report refresh-report.md  # Markdown report (default: stdout)
//   --result-json out.json     # Full RefreshResult JSON (default: stdout)
//
// Refresh's RefreshInput contains its own Repo + BaseMergedSHA +
// BaseMergedPRNumber + Targets + DryRun, so we don't need separate
// flags for those. The CLI is a thin wrapper around the library
// call.

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/vencil/threshold-exporter/internal/batchpr"
)

type refreshFlags struct {
	inputPath      string
	workdir        string
	reportPath     string
	resultJSONPath string
	help           bool
}

func parseRefreshFlags(args []string, errOut io.Writer) (*refreshFlags, error) {
	fs := flag.NewFlagSet(programName+" refresh", flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &refreshFlags{}

	fs.StringVar(&f.inputPath, "input", "-",
		"Path to a RefreshInput JSON file ('-' = stdin).")
	fs.StringVar(&f.workdir, "workdir", "",
		"Local clone of the target repo (CWD for git ops). Required.")
	fs.StringVar(&f.reportPath, "report", "-",
		"Write the Markdown refresh-report to this file ('-' = stdout).")
	fs.StringVar(&f.resultJSONPath, "result-json", "-",
		"Write the JSON RefreshResult to this file ('-' = stdout).")
	fs.BoolVar(&f.help, "help", false, "Print usage and exit.")
	fs.BoolVar(&f.help, "h", false, "Alias for --help.")

	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s refresh [flags]\n", programName)
		fmt.Fprintf(errOut, "Rebase tenant branches after Base PR merges (PR-3 mode).\n\n")
		fs.PrintDefaults()
	}

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
}

func cmdRefresh(args []string, stdout, errOut io.Writer) int {
	f, err := parseRefreshFlags(args, errOut)
	if err != nil {
		return exitCallerErr
	}
	if f.help {
		return exitOK
	}
	// We need to read the input first to know the Repo for client
	// construction. The input drives client wiring.
	in := batchpr.RefreshInput{}
	if err := readInputJSON(f.inputPath, os.Stdin, &in); err != nil {
		fmt.Fprintf(errOut, "%s refresh: %v\n", programName, err)
		return exitCallerErr
	}
	git, pr, err := makeClients(f.workdir, in.Repo)
	if err != nil {
		fmt.Fprintf(errOut, "%s refresh: %v\n", programName, err)
		return exitCallerErr
	}
	return runRefresh(f, in, stdout, errOut, git, pr)
}

func runRefresh(f *refreshFlags, in batchpr.RefreshInput, stdout, errOut io.Writer, git batchpr.GitClient, pr batchpr.PRClient) int {
	result, err := batchpr.Refresh(context.Background(), in, git, pr)
	if err != nil {
		fmt.Fprintf(errOut, "%s refresh: %v\n", programName, err)
		return exitCallerErr
	}
	if err := writeReport(f.reportPath, stdout, result.ReportMarkdown); err != nil {
		fmt.Fprintf(errOut, "%s refresh: write report: %v\n", programName, err)
		return exitCallerErr
	}
	if err := writeJSON(f.resultJSONPath, stdout, result); err != nil {
		fmt.Fprintf(errOut, "%s refresh: write result JSON: %v\n", programName, err)
		return exitCallerErr
	}
	return exitCodeForRefresh(result.Summary)
}
