package confd

import (
	"fmt"
	"regexp"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// ⛔ ONE VERDICT ON WHICH conf.d BYTES CAN BE READ, ALSO IN tenant-api'S OWN
// READS AND WRITES (ADR-036 §2 "Go 端的讀取者也照這份判定", §7 step 7, #3206).
//
// pkg/config judges the files it decodes (step 6: the exporter, da-guard and
// tenant-api's read plane through it). tenant-api also decodes conf.d bytes
// itself: the body a write commits (gitops.validateShape), the file a
// whole-file write replaces (the end-of-life guard's baseline), and its own
// management files — `_tenant_orgs.yaml`, the RBAC file, the groups and views
// records and the legacy AccountID registry the state store imports. Each of
// those calls Judge before its first decode, so it refuses exactly the bytes
// parse-tree refuses (`da-guard parse-tree`, which the route generator reads),
// with the same parsetree.Kind, and decodes only bytes parse-tree reads.
//
// What a refusal then means is each caller's existing handling of a file that
// does not parse — never "read it as empty": several of these files are the
// authorization boundary, and an empty RBAC or org map is a different grant,
// not a missing one.

// Judge is parse-tree's verdict on data: nil when parsetree.Judge reads it,
// else a *cfg.UnreadableFileError — the type pkg/config's decode entries
// return, so cfg.UnreadableKindOf names the kind wherever it is wrapped.
//
// It calls parsetree.Judge itself rather than going through pkg/config's
// verdict cache (unexported there): the same function of the same bytes, so
// the verdict cannot differ — only the cost of judging a body twice, which
// is bounded by TA_MAX_TENANT_DOC_BYTES before either runs.
func Judge(data []byte) error {
	if bad := parsetree.Judge(data); bad != nil {
		return &cfg.UnreadableFileError{Kind: bad.Kind, Message: bad.Message}
	}
	return nil
}

// UnreadableForCaller is what an API caller is told about bytes Judge
// refused: what was refused, the kind and the line, never parse-tree's own
// message. That message quotes what it refuses (a repeated key, a tag, a
// yaml.v3 error), and the bytes may be a whole file several tenants share — a
// batch patch validates the merged file — so it could hand one tenant
// another's key or value (#2903). The caller logs the full error.
func UnreadableForCaller(what string, err error) string {
	kind, ok := cfg.UnreadableKindOf(err)
	if !ok {
		return what + " cannot be read (the details are in the server log)"
	}
	line := ""
	if m := unreadableLine.FindStringSubmatch(err.Error()); m != nil {
		line = fmt.Sprintf(", line %s", m[1])
	}
	return fmt.Sprintf("%s cannot be read: parse-tree refuses it (kind %s%s), and so do threshold-exporter "+
		"and the route generator (the details are in the server log)", what, kind, line)
}

var unreadableLine = regexp.MustCompile(`\bline (\d+)\b`)
