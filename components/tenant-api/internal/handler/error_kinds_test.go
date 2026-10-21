package handler

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errkind"
)

// errkindConstants parses internal/errkind for every constant of type Kind:
// the list the table must cover, read from the source so a new Kind cannot
// be forgotten here as well as in the table.
func errkindConstants(t *testing.T) []errkind.Kind {
	t.Helper()
	fset := token.NewFileSet()
	paths, err := filepath.Glob(filepath.Join("..", "errkind", "*.go"))
	if err != nil {
		t.Fatal(err)
	}
	var out []errkind.Kind
	for _, path := range paths {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", path, err)
		}
		for _, decl := range f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				if id, ok := vs.Type.(*ast.Ident); !ok || id.Name != "Kind" {
					continue
				}
				for _, v := range vs.Values {
					lit, ok := v.(*ast.BasicLit)
					if !ok {
						t.Fatalf("errkind constant %v is not a string literal", vs.Names)
					}
					s, err := strconv.Unquote(lit.Value)
					if err != nil {
						t.Fatal(err)
					}
					out = append(out, errkind.Kind(s))
				}
			}
		}
	}
	if len(out) < 10 {
		t.Fatalf("read %d errkind constants; the parser no longer finds them", len(out))
	}
	return out
}

// TestKindTable_CoversEveryKindOnce: every errkind.Kind has exactly one row,
// every row is a Kind, and every kindRules row and fallback kind has a row —
// kindSpecOf never falls back to Internal for a Kind someone declared.
func TestKindTable_CoversEveryKindOnce(t *testing.T) {
	t.Parallel()
	declared := map[errkind.Kind]bool{}
	for _, k := range errkindConstants(t) {
		if declared[k] {
			t.Errorf("errkind declares %q twice", k)
		}
		declared[k] = true
	}
	rows := map[errkind.Kind]int{}
	for _, sp := range kindTable {
		rows[sp.Kind]++
		if !declared[sp.Kind] {
			t.Errorf("kindTable row %q is not an errkind constant", sp.Kind)
		}
		if sp.Kind != errkind.NoChanges && (sp.Status < 400 || sp.Code == "") {
			t.Errorf("kindTable row %q: status %d code %q — a failure needs an error status and a code", sp.Kind, sp.Status, sp.Code)
		}
	}
	for k := range declared {
		if rows[k] != 1 {
			t.Errorf("errkind.%s has %d kindTable rows, want 1", k, rows[k])
		}
	}
	ruled := map[errkind.Kind]bool{}
	for _, rl := range kindRules {
		if ruled[rl.Kind] {
			t.Errorf("kindRules lists %q twice: the second row is dead", rl.Kind)
		}
		ruled[rl.Kind] = true
		if rows[rl.Kind] != 1 {
			t.Errorf("kindRules row %q has no kindTable row", rl.Kind)
		}
		if !rl.typed && len(rl.sentinels) == 0 && rl.match == nil {
			t.Errorf("kindRules row %q matches nothing", rl.Kind)
		}
	}
	// The kinds no error is: an entry's fallback picks them.
	for _, k := range []errkind.Kind{errkind.BadRequest, errkind.Internal, errkind.ForgeCreateFailed} {
		if ruled[k] {
			t.Errorf("fallback kind %q has a kindRules row", k)
		}
		ruled[k] = true
	}
	for k := range declared {
		if !ruled[k] {
			t.Errorf("errkind.%s has neither a kindRules row nor a fallback that answers it", k)
		}
	}
}

// retryableWithoutRetryAfter are the retryable 5xx kinds that deliberately
// carry no Retry-After, each with why. Anything else retryable at 5xx must
// say when to come back (TestKindTable_RetryableServerErrorsSayWhen).
var retryableWithoutRetryAfter = map[errkind.Kind]string{
	// The write stopped before it committed because ITS OWN request (or async
	// task) was cancelled or reached its deadline, or one git command hit its
	// own deadline — nothing on the server is busy or recovering, so there is
	// no wait to pace: the caller may resend at once (msgWriteCancelled says
	// it is safe to retry).
	errkind.WriteCancelled: "the request's own cancellation or deadline; nothing server-side to wait for",
}

// TestKindTable_RetryableServerErrorsSayWhen: a retryable 5xx without
// Retry-After leaves an automated client to guess its back-off (#3226 found
// one: the forge PR/MR creation 503). Every Retryable row with Status >= 500
// has RetryAfterS > 0, or is a listed exception; an exception that no longer
// applies is reported too, so the list cannot outlive its reason.
func TestKindTable_RetryableServerErrorsSayWhen(t *testing.T) {
	t.Parallel()
	for _, sp := range kindTable {
		_, exempt := retryableWithoutRetryAfter[sp.Kind]
		needs := sp.Retryable && sp.Status >= 500
		switch {
		case needs && sp.RetryAfterS <= 0 && !exempt:
			t.Errorf("kindTable row %q: retryable %d with no RetryAfterS — give it one, or list it in "+
				"retryableWithoutRetryAfter with the reason", sp.Kind, sp.Status)
		case exempt && (!needs || sp.RetryAfterS > 0):
			t.Errorf("retryableWithoutRetryAfter lists %q, which is not a retryable 5xx without "+
				"Retry-After any more; drop the exception", sp.Kind)
		}
	}
}

// TestKindSpecs_IsACopy: the exported listing cannot edit the table.
func TestKindSpecs_IsACopy(t *testing.T) {
	t.Parallel()
	specs := KindSpecs()
	if len(specs) != len(kindTable) {
		t.Fatalf("KindSpecs has %d rows, the table %d", len(specs), len(kindTable))
	}
	specs[0].Status = 0
	if kindTable[0].Status == 0 {
		t.Fatal("KindSpecs returned the table itself")
	}
}
