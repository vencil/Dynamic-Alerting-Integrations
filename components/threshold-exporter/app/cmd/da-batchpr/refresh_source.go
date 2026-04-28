package main

// `da-batchpr refresh-source` — apply data-layer hot-fix files
// into existing tenant branches (PR-4 mode).
//
// Input contract:
//
//   --input refresh-source.json   # batchpr.RefreshSourceInput JSON.
//                                 # The Files map field is `json:"-"`
//                                 # (intentionally not serialised); each
//                                 # target's Files content is loaded from
//                                 # --patches-dir/<pr-number>/<repo-relative-path>.
//   --patches-dir ./patches/      # Per-target patch directory tree.
//                                 # Layout: <patches-dir>/<pr-number>/<file-paths>
//                                 # Empty subdir → empty Files (PatchSkippedNoChange).
//   --workdir ./repo              # Local clone (CWD for git ops). Required.
//   --report patch-plan.md        # Markdown report (default: stdout)
//   --result-json out.json        # Full RefreshSourceResult JSON

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"

	"github.com/vencil/threshold-exporter/internal/batchpr"
)

type refreshSourceFlags struct {
	inputPath      string
	patchesDir     string
	workdir        string
	reportPath     string
	resultJSONPath string
	help           bool
}

func parseRefreshSourceFlags(args []string, errOut io.Writer) (*refreshSourceFlags, error) {
	fs := flag.NewFlagSet(programName+" refresh-source", flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &refreshSourceFlags{}

	fs.StringVar(&f.inputPath, "input", "-",
		"Path to a RefreshSourceInput JSON file ('-' = stdin). The Files map is loaded from --patches-dir per target.")
	fs.StringVar(&f.patchesDir, "patches-dir", "",
		"Per-target patch directory; layout: <dir>/<pr-number>/<file-paths>. Required.")
	fs.StringVar(&f.workdir, "workdir", "",
		"Local clone of the target repo (CWD for git ops). Required.")
	fs.StringVar(&f.reportPath, "report", "-",
		"Write the Markdown patch-plan report to this file ('-' = stdout).")
	fs.StringVar(&f.resultJSONPath, "result-json", "",
		"Write the JSON RefreshSourceResult to this file ('-' = stdout, empty = skip). "+
			"Empty default avoids gluing markdown + JSON when --report defaults to stdout.")
	fs.BoolVar(&f.help, "help", false, "Print usage and exit.")
	fs.BoolVar(&f.help, "h", false, "Alias for --help.")

	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s refresh-source [flags]\n", programName)
		fmt.Fprintf(errOut, "Apply data-layer hot-fix files into existing tenant branches (PR-4 mode).\n\n")
		fs.PrintDefaults()
	}

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
}

func cmdRefreshSource(args []string, stdout, errOut io.Writer) int {
	f, err := parseRefreshSourceFlags(args, errOut)
	if err != nil {
		return exitCallerErr
	}
	if f.help {
		return exitOK
	}
	if f.patchesDir == "" {
		fmt.Fprintf(errOut, "%s refresh-source: --patches-dir is required\n", programName)
		return exitCallerErr
	}
	in := batchpr.RefreshSourceInput{}
	if err := readInputJSON(f.inputPath, os.Stdin, &in); err != nil {
		fmt.Fprintf(errOut, "%s refresh-source: %v\n", programName, err)
		return exitCallerErr
	}
	if err := loadTargetPatches(&in, f.patchesDir); err != nil {
		fmt.Fprintf(errOut, "%s refresh-source: %v\n", programName, err)
		return exitCallerErr
	}
	git, pr, err := makeClients(f.workdir, in.Repo)
	if err != nil {
		fmt.Fprintf(errOut, "%s refresh-source: %v\n", programName, err)
		return exitCallerErr
	}
	return runRefreshSource(f, in, stdout, errOut, git, pr)
}

// loadTargetPatches walks <patchesDir>/<pr-number>/ for each target
// and populates target.Files. Missing per-PR subdirs leave Files
// nil → orchestration records PatchSkippedNoChange. Other read
// errors fail-fast (caller error path).
func loadTargetPatches(in *batchpr.RefreshSourceInput, patchesDir string) error {
	info, err := os.Stat(patchesDir)
	if err != nil {
		return fmt.Errorf("stat --patches-dir %q: %w", patchesDir, err)
	}
	if !info.IsDir() {
		return fmt.Errorf("--patches-dir %q is not a directory", patchesDir)
	}
	for i := range in.Targets {
		t := &in.Targets[i]
		subdir := filepath.Join(patchesDir, strconv.Itoa(t.PRNumber))
		subInfo, err := os.Stat(subdir)
		if err != nil {
			if os.IsNotExist(err) {
				// No subdir for this PR → empty Files →
				// PatchSkippedNoChange. Not an error.
				continue
			}
			return fmt.Errorf("stat %q: %w", subdir, err)
		}
		if !subInfo.IsDir() {
			return fmt.Errorf("expected directory at %q (got file)", subdir)
		}
		files, err := walkFilesDir(subdir, "")
		if err != nil {
			return fmt.Errorf("read patches for PR #%d: %w", t.PRNumber, err)
		}
		t.Files = files
	}
	return nil
}

func runRefreshSource(f *refreshSourceFlags, in batchpr.RefreshSourceInput, stdout, errOut io.Writer, git batchpr.GitClient, pr batchpr.PRClient) int {
	result, err := batchpr.RefreshSource(context.Background(), in, git, pr)
	if err != nil {
		fmt.Fprintf(errOut, "%s refresh-source: %v\n", programName, err)
		return exitCallerErr
	}
	if err := writeReport(f.reportPath, stdout, result.ReportMarkdown); err != nil {
		fmt.Fprintf(errOut, "%s refresh-source: write report: %v\n", programName, err)
		return exitCallerErr
	}
	if f.resultJSONPath != "" {
		if err := writeJSON(f.resultJSONPath, stdout, result); err != nil {
			fmt.Fprintf(errOut, "%s refresh-source: write result JSON: %v\n", programName, err)
			return exitCallerErr
		}
	}
	return exitCodeForRefreshSource(result.Summary)
}
