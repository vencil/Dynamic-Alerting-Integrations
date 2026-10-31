package gitops

// The shared key verdict on a write the generator passed (ADR-038 decision
// 5): which of the keys of the tenants the write touches no reader reads.
// An error finding about a tenant the write is for refuses the write
// (keyVerdictRefusal, 400 as the key check that came before it); one the
// file already held counts too. Everything else is turned into lines of the
// write's warnings, under the same disclosure rules as every other message
// a write returns (#1530, #3039):
//
//   - a finding in a file this write changes is about the tenants it names:
//     about a tenant the write is for, it is shown (the caller's write for
//     it was admitted), in a direct batch on that tenant's op; about another
//     tenant the file declares, only when the caller may name it
//     (TenantNames);
//   - every other finding (a profile, a platform file, a file the write
//     does not change) is the platform's: shown only to a caller who sees
//     the whole tree (WithTreeVisible), and once per write — to anyone else
//     one line says only that there is something, and the details go to
//     the log;
//   - a verdict that did not complete says so, with its reason only: the
//     generator's message can carry a platform path or a program's stderr.
//
// The verdict judges the tree after the write; it is not compared with the
// tree before it, so a finding can be one the write did not cause. A write
// that changes nothing carries no verdict (most paths do not ask the
// generator at all; a PR-mode PUT asks before it finds nothing to commit,
// and drops the answer).

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"strings"

	"github.com/vencil/tenant-api/internal/errkind"
)

// KeyVerdict is the generator container's key verdict on a passed write
// (its `key_verdict`).
type KeyVerdict struct {
	// Status is KeyVerdictComplete or KeyVerdictNotCompleted.
	Status string
	// Reason is why a verdict did not complete: the generator's reasons
	// (no_time, deadline, memory, error, undecidable, platform_file; an
	// older generator's preempted) or
	// KeyVerdictNotReported.
	Reason string
	// Findings are the keys no reader reads (complete only); Truncated
	// counts those the generator did not send.
	Findings  []KeyFinding
	Truncated int
}

// KeyFinding is one finding of a KeyVerdict: a key (Key, as the file
// spells it) written in File (relative to conf.d; "" when no single file
// applies) that reaches Tenants. Kind is unread, disable_noop or
// disable_partial; Severity the one ADR-038 decision 4 gives the gates once
// they block; Reason served-values' reason for an unread key.
type KeyFinding struct {
	Severity string
	Kind     string
	Key      string
	File     string
	Tenants  []string
	Reason   string
}

// KeyVerdict statuses and the reason of a verdict the generator did not
// report.
const (
	KeyVerdictComplete     = "complete"
	KeyVerdictNotCompleted = "not_completed"
	// KeyVerdictNotReported: the generator passed the write without a key
	// verdict (an image older than ADR-038 decision 5).
	KeyVerdictNotReported = "not_reported"
)

// maxVerdictLines is the most lines one rendering returns (one tenant's op,
// or a whole write's flat list); the rest is counted in one more line.
const maxVerdictLines = 100

// treeVisibleKey is the context key WithTreeVisible sets.
type treeVisibleKey struct{}

// WithTreeVisible marks ctx as a request whose caller may see the whole
// tree (the handler's treePathsVisible): the key verdict's findings about
// platform files and profiles are shown to it.
func WithTreeVisible(ctx context.Context, visible bool) context.Context {
	return context.WithValue(ctx, treeVisibleKey{}, visible)
}

// TreeVisibleOf is what WithTreeVisible set on ctx; false when unset.
func TreeVisibleOf(ctx context.Context) bool {
	v, _ := ctx.Value(treeVisibleKey{}).(bool)
	return v
}

// The texts of a key verdict's lines.
const (
	msgVerdictPrefix         = "key verdict"
	msgVerdictOtherWithheld  = "key verdict: there are findings about platform files, profiles or tenants you cannot view (the details are in the server log)"
	msgVerdictMoreLinesFmt   = "key verdict: %d more line(s) not shown"
	msgVerdictTruncatedFmt   = "key verdict: %d more finding(s) the generator did not send"
	msgVerdictNotCompleteFmt = "key verdict: not completed (%s); the keys of this write were not judged"
)

// verdictReasons are the texts of a verdict that did not complete. A
// reason not listed (a newer generator) is shown as its name.
var verdictReasons = map[string]string{
	"no_time":             "too little of the judgement's time was left for it",
	"deadline":            "the judgement's time ran out",
	"preempted":           "it yielded to another write waiting for the generator", // a generator before ADR-038's 2026-10-30 revision
	"memory":              "it ran out of memory",
	"error":               "it failed",
	"undecidable":         "no root platform file was read, so threshold keys cannot be judged",
	"platform_file":       "the write changes a platform file, which only a whole-tree verdict judges",
	KeyVerdictNotReported: "the generator container did not report one; it predates ADR-038 decision 5",
}

// verdictKinds say what a finding's Kind means.
var verdictKinds = map[string]string{
	"unread":          "is read by no reader",
	"disable_noop":    "is a disable that switches nothing off",
	"disable_partial": "is disabled, but siblings it inherits still serve",
}

// verdictAdvice is a key verdict rendered for one caller: the lines for
// each tenant of the write (byTenant), those about the whole write (whole),
// and the line saying it was not judged (notJudged), which concerns every
// tenant of the write.
type verdictAdvice struct {
	byTenant  map[string][]string
	whole     []string
	notJudged string
}

// adviseKeyVerdict renders kv on files for the caller of ctx (see the file
// comment). nil kv renders nothing.
func adviseKeyVerdict(ctx context.Context, files []ProposedFile, kv *KeyVerdict) verdictAdvice {
	adv := verdictAdvice{byTenant: map[string][]string{}}
	if kv == nil {
		return adv
	}
	if kv.Status != KeyVerdictComplete {
		reason := verdictReasons[kv.Reason]
		switch {
		case reason == "" && kv.Reason == "":
			reason = "no reason given"
		case reason == "":
			reason = kv.Reason
		}
		adv.notJudged = fmt.Sprintf(msgVerdictNotCompleteFmt, reason)
		return adv
	}
	names, treeVisible := TenantNamesOf(ctx), TreeVisibleOf(ctx)
	written := make(map[string]map[string]bool, len(files))
	for _, f := range files {
		ts := written[f.Path]
		if ts == nil {
			ts = map[string]bool{}
			written[f.Path] = ts
		}
		for _, t := range f.Tenants {
			ts[t] = true
		}
	}
	// One RBAC answer per tenant, as DiscloseKeyValidation asks each
	// tenant once: every Shown is an org-read check that counts a deny the
	// enforce flip reads, and this runs under the writer lock.
	shown := map[string]bool{}
	mayName := func(t string) bool {
		v, ok := shown[t]
		if !ok {
			v = names.Shown(t)
			shown[t] = v
		}
		return v
	}
	withheld := 0
	for _, f := range kv.Findings {
		ts := written[f.File]
		if f.File == "" || ts == nil || len(f.Tenants) == 0 {
			// Not in a file this write changes: the platform's.
			if treeVisible {
				adv.whole = append(adv.whole, verdictLine(f, f.Tenants))
			} else {
				withheld++
			}
			continue
		}
		// In a file this write changes: each tenant's. A tenant the write
		// is for is the caller's own (its write was admitted), as
		// DiscloseKeyValidation shows the written tenant's messages; another
		// tenant the file declares is judged too, and shown as names says.
		for _, t := range f.Tenants {
			switch {
			case ts[t]:
				adv.byTenant[t] = append(adv.byTenant[t], verdictLine(f, []string{t}))
			case mayName(t):
				adv.whole = append(adv.whole, verdictLine(f, []string{t}))
			default:
				withheld++
			}
		}
	}
	// The count of findings not sent counts ones about other tenants and
	// platform files too: a number only for a caller who may see them all
	// (like msgOtherPlace*, the others are told only that there is
	// something).
	switch {
	case kv.Truncated > 0 && treeVisible:
		adv.whole = append(adv.whole, fmt.Sprintf(msgVerdictTruncatedFmt, kv.Truncated))
	case kv.Truncated > 0:
		withheld++
	}
	if withheld > 0 {
		adv.whole = append(adv.whole, msgVerdictOtherWithheld)
		slog.Info("key verdict: findings withheld from the caller", "withheld", withheld,
			"not_sent", kv.Truncated, "findings", kv.Findings)
	}
	return adv
}

// keyVerdictSeverityError is the severity of a finding that refuses a write
// (ADR-038 decision 4).
const keyVerdictSeverityError = "error"

// KeyVerdictError refuses a write whose key verdict has error findings about
// a tenant the write is for (ADR-038 decision 5): ByTenant is, per such
// tenant, its findings' lines. Those are the caller's own (its write for the
// tenant was admitted), so they are shown in full. It unwraps to
// ErrValidation: the answer is the key check's, 400 BAD_REQUEST.
type KeyVerdictError struct {
	ByTenant map[string][]string
}

func (e *KeyVerdictError) Error() string {
	return fmt.Sprintf("%v: %s", ErrValidation, strings.Join(e.lines(), "; "))
}

// Unwrap makes errors.Is(err, ErrValidation) hold.
func (e *KeyVerdictError) Unwrap() error { return ErrValidation }

// ErrorKind is errkind.Validation.
func (e *KeyVerdictError) ErrorKind() errkind.Kind { return errkind.Validation }

// lines is every tenant's lines, in tenant order, capped (capVerdictLines).
func (e *KeyVerdictError) lines() []string {
	tenants := make([]string, 0, len(e.ByTenant))
	for t := range e.ByTenant {
		tenants = append(tenants, t)
	}
	sort.Strings(tenants)
	var out []string
	for _, t := range tenants {
		out = append(out, e.ByTenant[t]...)
	}
	return capVerdictLines(out)
}

// keyVerdictRefusal is the *KeyVerdictError of kv on files, nil when kv
// refuses nothing: an error finding in a file this write changes, about a
// tenant that file is written for. Another tenant the file declares, a
// platform file or a profile refuses nothing (ADR-038 decision 6) — their
// findings are reported as before (adviseKeyVerdict).
func keyVerdictRefusal(files []ProposedFile, kv *KeyVerdict) *KeyVerdictError {
	if kv == nil || kv.Status != KeyVerdictComplete {
		return nil
	}
	written := make(map[string]map[string]bool, len(files))
	for _, f := range files {
		if written[f.Path] == nil {
			written[f.Path] = map[string]bool{}
		}
		for _, t := range f.Tenants {
			written[f.Path][t] = true
		}
	}
	var out *KeyVerdictError
	for _, f := range kv.Findings {
		if f.Severity != keyVerdictSeverityError {
			continue
		}
		for _, t := range f.Tenants {
			if !written[f.File][t] {
				continue
			}
			if out == nil {
				out = &KeyVerdictError{ByTenant: map[string][]string{}}
			}
			out.ByTenant[t] = append(out.ByTenant[t], verdictLine(f, []string{t}))
		}
	}
	return out
}

// rejectBatchOnVerdict is err as a batch's answer: a *KeyVerdictError
// becomes a *BatchRejectedError whose first op that changed each refused
// tenant carries that tenant's lines, so the answer names the ops to fix;
// any other err is returned as it is.
func rejectBatchOnVerdict(err error, ops []BatchOp, outcomes []BatchOpOutcome) error {
	kve, ok := err.(*KeyVerdictError)
	if !ok {
		return err
	}
	out := append([]BatchOpOutcome(nil), outcomes...)
	placed := map[string]bool{}
	for i, op := range ops {
		lines, refused := kve.ByTenant[op.TenantID]
		if !refused || placed[op.TenantID] || outcomes[i].NoOp || outcomes[i].Err != nil {
			continue
		}
		out[i] = BatchOpOutcome{Err: &KeyVerdictError{ByTenant: map[string][]string{op.TenantID: lines}}}
		placed[op.TenantID] = true
	}
	if len(placed) != len(kve.ByTenant) {
		return kve // a refused tenant no op changed: not a batch's shape; refuse it whole
	}
	return &BatchRejectedError{Outcomes: out}
}

// verdictLine is one finding's line, naming tenants.
func verdictLine(f KeyFinding, tenants []string) string {
	what := verdictKinds[f.Kind]
	if what == "" {
		what = "is reported as " + f.Kind
	}
	var b strings.Builder
	fmt.Fprintf(&b, "%s: %s: key %s", msgVerdictPrefix, f.Severity, f.Key)
	if f.File != "" {
		fmt.Fprintf(&b, " in %s", f.File)
	}
	fmt.Fprintf(&b, " %s", what)
	if f.Reason != "" {
		fmt.Fprintf(&b, " (%s)", f.Reason)
	}
	if len(tenants) > 0 {
		fmt.Fprintf(&b, " — tenant(s) %s", strings.Join(tenants, ", "))
	}
	return b.String()
}

// lines is the advice as one list, for a write whose notices are one list
// (a single write, a PR batch): the written tenants' lines in tenant order
// first — those are the write's own, kept when the cap cuts — then the
// whole write's, capped (capVerdictLines).
func (a verdictAdvice) lines() []string {
	if a.notJudged != "" {
		return []string{a.notJudged}
	}
	var out []string
	tenants := make([]string, 0, len(a.byTenant))
	for t := range a.byTenant {
		tenants = append(tenants, t)
	}
	sort.Strings(tenants)
	for _, t := range tenants {
		out = append(out, a.byTenant[t]...)
	}
	return capVerdictLines(append(out, a.whole...))
}

// capVerdictLines keeps the first maxVerdictLines lines and counts the
// rest in one more.
func capVerdictLines(lines []string) []string {
	if len(lines) <= maxVerdictLines {
		return lines
	}
	return append(lines[:maxVerdictLines:maxVerdictLines], fmt.Sprintf(msgVerdictMoreLinesFmt, len(lines)-maxVerdictLines))
}

// adviseBatch adds the advice to a committed direct batch's outcomes (one
// per op of ops): each tenant's lines on the first op for that tenant that
// changed something, and the whole write's on the first op that changed
// something — once, not on every op. (Not a batch-level list: an async
// batch's task keeps per-op results only.) A verdict that was not judged is
// said on every op that changed something: each op's keys were not judged,
// and a reader shown only some ops' results must not read the others as
// "nothing found". Each op's lines are capped (capVerdictLines), its own
// tenant's first.
func (a verdictAdvice) adviseBatch(ops []BatchOp, outcomes []BatchOpOutcome) {
	first := map[string]int{}
	whole := -1
	for i := range ops {
		if outcomes[i].NoOp || outcomes[i].Err != nil {
			continue
		}
		if whole < 0 {
			whole = i
		}
		if _, ok := first[ops[i].TenantID]; !ok {
			first[ops[i].TenantID] = i
		}
	}
	if whole < 0 {
		return // nothing changed: nothing was judged
	}
	if a.notJudged != "" {
		for i := range ops {
			if !outcomes[i].NoOp && outcomes[i].Err == nil {
				outcomes[i].Notices = append(outcomes[i].Notices, a.notJudged)
			}
		}
		return
	}
	lines := make([][]string, len(ops))
	for t, ls := range a.byTenant {
		i, ok := first[t]
		if !ok {
			i = whole
		}
		lines[i] = append(lines[i], ls...)
	}
	lines[whole] = append(lines[whole], a.whole...)
	for i, ls := range lines {
		if len(ls) > 0 {
			outcomes[i].Notices = append(outcomes[i].Notices, capVerdictLines(ls)...)
		}
	}
}
