package gitops

// The AccountID registry's git-history high-water mark.
//
// The registry used to be conf.d/_account_registry.yaml, committed on every
// allocation. It now lives in tenant-api's state store (#2856); this read
// remains for the one-time import (internal/statemigrate), which raises the
// imported next_account_id to the highest value the file ever held in git so
// no id handed out before a revert is handed out again.

import (
	"context"
	"path/filepath"
	"regexp"
	"strconv"
)

// counterLine matches the registry's `<field>: <uint>` set as a diff ADDITION.
var counterLine = regexp.MustCompile(`(?m)^\+\s*([A-Za-z0-9_]+):\s*(\d+)\s*$`)

// HistoricalMaxUint returns the largest value the integer YAML field `field` has
// EVER held across the full git history of conf.d/<filename>, or 0 if the file
// has no history yet (a never-committed path is exit-0 + empty, not an error).
//
// This is the revert-proof high-water mark (ADR-021 §AccountID #1/#3): a
// `git revert` or hand-edit that LOWERS the field writes a NEW commit, but the
// higher value it held before still lives in an older commit's diff — so an
// allocator that floors its counter to this value can never re-issue an id a
// reverted-away commit already handed out (the cross-tenant log-merge). It is
// read-only (no mutex needed) and bounded by the regular git timeout.
//
// FAIL-CLOSED: a genuine git failure returns an error so the caller refuses to
// allocate against a history it cannot read.
func (w *Writer) HistoricalMaxUint(ctx context.Context, filename, field string) (uint32, error) {
	base := filepath.Base(filename)
	// `git log -p --format=` prints, as `+`-prefixed diff additions, every value
	// the field was ever set to (the first commit shows the whole file as
	// additions; each later change shows the new value). Scanning those additions
	// for `field: <n>` and taking the max yields the all-time high — immune to a
	// later decrease, which only adds a lower value, never removes the higher one.
	// --no-ext-diff / --no-textconv (#3175): the scan reads the blobs' own
	// lines, and no program from config or the environment runs for it.
	cmd, cctx, cancel := w.gitCmdWithTimeout(w.gitTimeout, "-C", w.gitDir, "log", "-p", "--no-ext-diff", "--no-textconv", "--format=", "--", base)
	defer cancel()
	out, err := cmd.CombinedOutput()
	if err != nil {
		return 0, w.gitErr(cctx, "log", err, out)
	}
	var maxVal uint32
	for _, m := range counterLine.FindAllSubmatch(out, -1) {
		if string(m[1]) != field {
			continue
		}
		if n, perr := strconv.ParseUint(string(m[2]), 10, 32); perr == nil && uint32(n) > maxVal {
			maxVal = uint32(n)
		}
	}
	return maxVal, nil
}
