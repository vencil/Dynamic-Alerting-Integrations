package generator

import (
	"fmt"
	"strings"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/errkind"
)

// Results of a verdict (the service's `result`).
const (
	ResultPass     = "pass"
	ResultFindings = "findings"
	ResultRejected = "rejected"
)

// The service's 503 codes, passed through to the HTTP client as they are
// (ADR-036 §5). The strings are internal/errcode's (#3202): the service and
// tenant-api name these two failures alike, so they are not a second copy.
const (
	CodeValidatorUnavailable = string(errcode.ValidatorUnavailable)
	CodePolicyUnavailable    = string(errcode.PolicyUnavailable)
)

// Reasons of a *RejectedError. The first five are the service's own
// (`result: rejected`); the rest are tenant-api's.
const (
	ReasonDeadline   = "deadline"   // the judgement deadline passed (queued or judging)
	ReasonMemory     = "memory"     // the judgement process ran out of RLIMIT_DATA
	ReasonIncomplete = "incomplete" // the judgement process ended without a complete result
	ReasonGitRead    = "git_read"   // reading the base commit from git failed twice
	ReasonBusy       = "busy"       // the service's queue or admission budget is full
	// ReasonTooLarge: the service answered 413 — the files the write
	// produces, base64-encoded, are over its --max-body-bytes.
	ReasonTooLarge = "too_large"
	// ReasonInvalidRequest: the service answered 400 (e.g. a written path
	// whose parent is a file in the base tree).
	ReasonInvalidRequest = "invalid_request"
	// ReasonMismatch: what the service judged is still not what would be
	// written after one re-judgement (ADR-036 §4 "驗的和寫的必須是同一份").
	ReasonMismatch = "mismatch"
	// ReasonNoTime: too little of the request's time is left to (re)judge.
	ReasonNoTime = "no_time"
	// ReasonNormalized: git would commit other bytes than the ones judged —
	// a .gitattributes rule (eol, text, a clean filter…) rewrote a file as
	// it was staged (ADR-036 §4 "驗的和寫的必須是同一份"). Not retryable:
	// the same body is rewritten the same way.
	ReasonNormalized = "normalized"
)

// Reasons of a *RejectedError for a write the generator passed but whose
// key verdict did not complete (ADR-038 decision 5): the write is refused,
// since whether every key of it is read was not judged. Retryable only when
// time ran out (key_verdict_no_time, key_verdict_deadline).
const (
	ReasonKeyVerdictNoTime       = "key_verdict_no_time"
	ReasonKeyVerdictDeadline     = "key_verdict_deadline"
	ReasonKeyVerdictMemory       = "key_verdict_memory"
	ReasonKeyVerdictError        = "key_verdict_error"
	ReasonKeyVerdictUndecidable  = "key_verdict_undecidable"
	ReasonKeyVerdictPlatformFile = "key_verdict_platform_file"
)

// RejectionReasons lists every *RejectedError reason, in the order
// tenant_api_generator_rejections_total renders them.
var RejectionReasons = []string{
	ReasonDeadline, ReasonMemory, ReasonIncomplete, ReasonGitRead, ReasonBusy,
	ReasonTooLarge, ReasonInvalidRequest, ReasonMismatch, ReasonNoTime, ReasonNormalized,
	ReasonKeyVerdictNoTime, ReasonKeyVerdictDeadline, ReasonKeyVerdictMemory,
	ReasonKeyVerdictError, ReasonKeyVerdictUndecidable, ReasonKeyVerdictPlatformFile,
}

// Reasons of an *UnavailableError the service gives (its 503s, ADR-036 §5).
const (
	ReasonAmtoolMissing   = "amtool_missing"
	ReasonDaGuardMissing  = "da_guard_missing"
	ReasonGeneratorTooOld = "generator_too_old"
	ReasonInternalError   = "internal_error"
	ReasonPolicyUnusable  = "policy_unusable"
)

// Reasons of an *UnavailableError raised by tenant-api itself.
const (
	ReasonUnreachable = "unreachable"  // no connection, or no answer before the deadline
	ReasonBadResponse = "bad_response" // an answer that cannot be read
	ReasonInternal    = "internal"     // tenant-api could not build the request
	ReasonUnknown     = "unknown"      // a 503 without a reason
	// ReasonPolicyUnreadable: tenant-api cannot read its own copy of the
	// deployment policy file (--deploy-policy), so it cannot check that the
	// generator judged with the same one.
	ReasonPolicyUnreadable = "policy_unreadable"
)

// Retryable reports whether a *RejectedError reason is answered 429 (retry
// later: the same write may pass) rather than 422 (it will not pass as it
// is).
func Retryable(reason string) bool {
	switch reason {
	case ReasonBusy, ReasonDeadline, ReasonGitRead, ReasonMismatch, ReasonNoTime,
		ReasonKeyVerdictNoTime, ReasonKeyVerdictDeadline:
		return true
	}
	return false
}

// FindingsError refuses a write the generator judged and found problems
// with (`result: findings`, ADR-036 §4). Findings lists them: the ones
// related to this write first.
type FindingsError struct {
	Findings []Finding
	// ComparedWithBase: the base commit was judged too, so every finding
	// carries PreExisting. False when there was no time left for it (or that
	// judgement gave no verdict).
	ComparedWithBase bool
	// RC is the generator's exit code.
	RC int
}

func (e *FindingsError) Error() string {
	return fmt.Sprintf("the generator refused the write: %d finding(s), exit code %d", len(e.Findings), e.RC)
}

// RejectedError refuses a write without a verdict on its content: the
// service could not judge it this time (its own reasons), or tenant-api
// could not get a judgement it may rely on (see the Reason constants).
type RejectedError struct {
	Reason  string
	Message string
}

func (e *RejectedError) Error() string {
	return fmt.Sprintf("the generator gave no verdict (%s): %s", e.Reason, e.Message)
}

// UnavailableError: the generator container cannot judge at all (ADR-036
// §5). Code is CodeValidatorUnavailable or CodePolicyUnavailable.
type UnavailableError struct {
	Code    string
	Reason  string
	Message string
}

func (e *UnavailableError) Error() string {
	msg := strings.TrimSpace(e.Message)
	if msg == "" {
		return fmt.Sprintf("%s (%s)", e.Code, e.Reason)
	}
	return fmt.Sprintf("%s (%s): %s", e.Code, e.Reason, msg)
}

func unavailable(reason, message string) *UnavailableError {
	return &UnavailableError{Code: CodeValidatorUnavailable, Reason: reason, Message: message}
}

// UnavailableReasons lists every *UnavailableError reason tenant-api knows:
// the service's and its own.
var UnavailableReasons = []string{
	ReasonAmtoolMissing, ReasonDaGuardMissing, ReasonGeneratorTooOld, ReasonInternalError, ReasonPolicyUnusable,
	ReasonUnreachable, ReasonBadResponse, ReasonInternal, ReasonUnknown, ReasonPolicyUnreadable,
}

// ClientReason is the reason a client is shown: reason itself when it is
// one tenant-api knows (RejectionReasons, UnavailableReasons), else
// ReasonUnknown. The service's reason is passed through to tenant-api as
// it came, so a newer or a broken service can put anything there; the
// original goes to the log with the error.
func ClientReason(reason string) string {
	for _, list := range [][]string{RejectionReasons, UnavailableReasons} {
		for _, r := range list {
			if r == reason {
				return reason
			}
		}
	}
	return ReasonUnknown
}

// ErrorKind is errkind.GeneratorFindings.
func (e *FindingsError) ErrorKind() errkind.Kind { return errkind.GeneratorFindings }

// ErrorKind splits on Retryable: a reason a resend may get past is
// errkind.GeneratorRejectedRetryable, the rest errkind.GeneratorRejected.
func (e *RejectedError) ErrorKind() errkind.Kind {
	if Retryable(e.Reason) {
		return errkind.GeneratorRejectedRetryable
	}
	return errkind.GeneratorRejected
}

// ErrorKind is errkind.PolicyUnavailable for CodePolicyUnavailable and
// errkind.ValidatorUnavailable for every other code.
func (e *UnavailableError) ErrorKind() errkind.Kind {
	if e.Code == CodePolicyUnavailable {
		return errkind.PolicyUnavailable
	}
	return errkind.ValidatorUnavailable
}
