package generator

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"sort"
	"strings"
	"sync/atomic"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
)

// ServiceReserve is the service's --deadline-reserve-seconds default: of
// the `deadline_ms` it is given, it keeps this much back for the caller
// (tenant-api's reconciliation and commit, ADR-036 §3). The chart does not
// override it.
const ServiceReserve = 5 * time.Second

// MinJudgeTime is the least time left at which a judgement is asked for:
// the service's reserve plus one second to judge in. With less, the write is
// refused as retryable (ReasonNoTime) without asking — the service would
// only answer `deadline` (ADR-036 §3 "剩餘時間不夠重驗").
const MinJudgeTime = ServiceReserve + time.Second

// FallbackDeadline is the request time assumed for a ctx that carries no
// deadline. Every write path has one (the request's, or an async task's
// --write-timeout); this only keeps a missing one from meaning "forever".
const FallbackDeadline = 30 * time.Second

// Gate is the gitops.WriteValidator of the generator container: it asks the
// service about every content-changing write and turns the answer into the
// write's fate (ADR-036 §4, §5).
type Gate struct {
	client *Client
	// policyPath is tenant-api's read-only copy of the deployment policy
	// file the service reads (--deploy-policy); "" when there is none.
	policyPath string
	rejections map[string]*atomic.Int64
	// minJudge is MinJudgeTime; a test seam (a test cannot wait seconds
	// for every deadline case).
	minJudge time.Duration
}

// NewGate returns the Gate for client; policyPath as Gate.policyPath.
func NewGate(client *Client, policyPath string) *Gate {
	g := &Gate{client: client, policyPath: policyPath, rejections: map[string]*atomic.Int64{}, minJudge: MinJudgeTime}
	for _, r := range RejectionReasons {
		g.rejections[r] = &atomic.Int64{}
	}
	return g
}

// Client is the service client the gate asks.
func (g *Gate) Client() *Client { return g.client }

// Rejections is how many writes were refused for each RejectionReasons
// reason (tenant_api_generator_rejections_total).
func (g *Gate) Rejections() map[string]int64 {
	out := make(map[string]int64, len(g.rejections))
	for r, n := range g.rejections {
		out[r] = n.Load()
	}
	return out
}

var _ gitops.WriteValidator = (*Gate)(nil)

// ValidateWrites asks the service about files over base and returns nil only
// for a `pass` whose `validated` names exactly base, every file's bytes and
// the deployment policy file tenant-api reads (ADR-036 §4 "驗的和寫的必須是
// 同一份"). A pass that does not match is judged once more; a second
// mismatch, or too little time to ask again, is a retryable refusal.
//
// The time given to the service is what is left of ctx's deadline.
func (g *Gate) ValidateWrites(ctx context.Context, base string, files []gitops.ProposedFile) error {
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(FallbackDeadline)
	}
	writes := make([]Write, len(files))
	for i, f := range files {
		writes[i] = Write{Path: f.Path, Content: f.Content}
	}
	for attempt := 1; ; attempt++ {
		policy, err := g.policyHash()
		if err != nil {
			return err
		}
		remaining := time.Until(deadline)
		if remaining < g.minJudge {
			what := "judge the write"
			if attempt > 1 {
				what = "judge the write again after the generator judged other content than tenant-api would write"
			}
			return g.reject(ReasonNoTime, fmt.Sprintf("%v of the request's time is left, too little to %s (at least %v is needed); retry", remaining.Round(time.Millisecond), what, g.minJudge))
		}
		v, err := g.client.Validate(ctx, base, writes, remaining)
		if err != nil {
			return g.fromCallError(ctx, err)
		}
		switch v.Result {
		case ResultRejected:
			return g.reject(v.Reason, v.Message)
		case ResultFindings:
			if v.StderrTail != "" {
				slog.Info("generator: write refused with findings", "base", base, "files", len(files), "stderr_tail", v.StderrTail)
			}
			return g.findings(ctx, deadline, base, files, v)
		}
		problem := mismatch(v.Validated, base, files, policy)
		if problem == "" {
			return nil
		}
		slog.Warn("generator: the judged content is not what would be written", "attempt", attempt, "problem", problem)
		if attempt >= 2 {
			return g.reject(ReasonMismatch, "the generator judged other content than tenant-api would write, twice: "+problem+"; retry")
		}
	}
}

// fromCallError is ValidateWrites' answer to a failed call: an
// *UnavailableError or *RejectedError as it came (a rejection is counted),
// a ctx that ended while waiting — deadline: the service did not answer in
// the time it was given, which only a wedged service does (ADR-036 §3), so
// VALIDATOR_UNAVAILABLE; cancelled: ctx's error, as every write path
// returns it.
func (g *Gate) fromCallError(ctx context.Context, err error) error {
	var rej *RejectedError
	if errors.As(err, &rej) {
		return g.reject(rej.Reason, rej.Message)
	}
	if isCtxErr(err) {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return unavailable(ReasonUnreachable, "the generator did not answer before the request's deadline")
		}
		return err
	}
	return err
}

func (g *Gate) reject(reason, message string) error {
	if n, ok := g.rejections[reason]; ok {
		n.Add(1)
	} else {
		// A reason this tenant-api does not know (a newer service): kept as
		// its own text, counted under incomplete — no verdict was given.
		g.rejections[ReasonIncomplete].Add(1)
	}
	return &RejectedError{Reason: reason, Message: message}
}

// policyHash is the SHA-256 (lowercase hex) of tenant-api's copy of the
// deployment policy file, nil when there is none. A copy that cannot be read
// is POLICY_UNAVAILABLE: tenant-api cannot check what the service judged
// with.
func (g *Gate) policyHash() (*string, error) {
	if g.policyPath == "" {
		return nil, nil
	}
	data, err := os.ReadFile(g.policyPath)
	if err != nil {
		return nil, &UnavailableError{Code: CodePolicyUnavailable, Reason: ReasonPolicyUnreadable,
			Message: "tenant-api cannot read the deployment policy file (--deploy-policy): " + err.Error()}
	}
	sum := sha256.Sum256(data)
	h := hex.EncodeToString(sum[:])
	return &h, nil
}

// mismatch is "" when v names exactly base, files and policy, else what
// differs.
func mismatch(v *Validated, base string, files []gitops.ProposedFile, policy *string) string {
	if v == nil {
		return "the answer names nothing it judged"
	}
	if v.BaseCommit != base {
		return fmt.Sprintf("base commit %s, not %s", v.BaseCommit, base)
	}
	if len(v.Files) != len(files) {
		return fmt.Sprintf("%d file(s) judged, %d to write", len(v.Files), len(files))
	}
	judged := make(map[string]ValidatedFile, len(v.Files))
	for _, f := range v.Files {
		judged[f.Path] = f
	}
	for _, f := range files {
		j, ok := judged[f.Path]
		if !ok {
			return fmt.Sprintf("%s was not judged", f.Path)
		}
		sum := sha256.Sum256(f.Content)
		if j.Deleted || j.SHA256 != hex.EncodeToString(sum[:]) {
			return fmt.Sprintf("%s was judged with other content", f.Path)
		}
	}
	switch {
	case policy == nil && v.PolicySHA256 == nil:
	case policy == nil:
		return "the generator judged with a deployment policy file, tenant-api has none (--deploy-policy)"
	case v.PolicySHA256 == nil:
		return "the generator judged without a deployment policy file, tenant-api has one (--deploy-policy)"
	case *policy != *v.PolicySHA256:
		return "the generator judged with another deployment policy file than tenant-api reads"
	}
	return ""
}

// findings builds the *FindingsError of a `findings` verdict: the findings
// that name a file or a tenant of this write are marked related and put
// first; then, if the time left allows another judgement, base is judged
// without the write and every finding it also reports is marked
// pre-existing (ADR-036 §4). The order and the marks only shape the message.
func (g *Gate) findings(ctx context.Context, deadline time.Time, base string, files []gitops.ProposedFile, v *Verdict) error {
	out := &FindingsError{}
	if v.RC != nil {
		out.RC = *v.RC
	}
	var configDir string
	if v.Findings != nil {
		configDir = v.Findings.ConfigDir
		out.Findings = append(out.Findings, v.Findings.Findings...)
	}
	paths, tenants := map[string]bool{}, map[string]bool{}
	for _, f := range files {
		paths[f.Path] = true
		for _, t := range f.Tenants {
			tenants[t] = true
		}
	}
	for i := range out.Findings {
		f := &out.Findings[i]
		f.Related = (f.File != nil && paths[*f.File]) || (f.Tenant != nil && tenants[*f.Tenant])
		f.PreExisting = nil
	}
	sort.SliceStable(out.Findings, func(i, j int) bool {
		return out.Findings[i].Related && !out.Findings[j].Related
	})
	if remaining := time.Until(deadline); remaining >= g.minJudge {
		bv, err := g.client.Validate(ctx, base, nil, remaining)
		switch {
		case err != nil:
			slog.Info("generator: base not judged for the pre-existing marks", "error", err)
		case bv.Result == ResultPass:
			out.ComparedWithBase = true
			mark(out.Findings, configDir, nil, "")
		case bv.Result == ResultFindings && bv.Findings != nil:
			out.ComparedWithBase = true
			mark(out.Findings, configDir, bv.Findings.Findings, bv.Findings.ConfigDir)
		}
	}
	return out
}

// mark sets PreExisting on every finding: whether before (the base's, read
// with baseDir as its config_dir) holds the same one.
func mark(findings []Finding, dir string, before []Finding, baseDir string) {
	seen := make(map[string]bool, len(before))
	for _, f := range before {
		seen[findingKey(f, baseDir)] = true
	}
	for i := range findings {
		pre := seen[findingKey(findings[i], dir)]
		findings[i].PreExisting = &pre
	}
}

// findingKey identifies a finding across two judgements. Each judgement
// runs in its own scratch tree, so that tree's path (config_dir) is
// replaced in the message first.
func findingKey(f Finding, configDir string) string {
	msg := f.Message
	if configDir != "" {
		msg = strings.ReplaceAll(msg, configDir, "<conf.d>")
	}
	s := func(p *string) string {
		if p == nil {
			return "\x00"
		}
		return *p
	}
	return strings.Join([]string{f.Kind, s(f.Tenant), s(f.Policy), s(f.File), s(f.Field), msg}, "\x1f")
}
