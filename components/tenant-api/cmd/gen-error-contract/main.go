// Command gen-error-contract writes the client contract of tenant-api's error
// codes (#3202): tools/portal/src/interactive/tools/_common/data/tenant-api-errors.json,
// one entry per code of internal/errcode with its remedy and what the
// handler's kind table and the direct answers write it with — for the portal
// to read instead of writing the codes down a second time (it does, through
// tools/portal/src/interactive/tools/_common/api/classify-error.js).
//
//	go run ./cmd/gen-error-contract           # write the contract and patch docs/
//	go run ./cmd/gen-error-contract --check   # exit 1 if the committed copies drifted
//
// Run from components/tenant-api (`make error-contract` does; `make api-docs`
// runs it after swag). Exit codes:
// 0 wrote / in sync · 1 drift (--check) · 2 caller error (bad arguments, the
// module or the output not found, a write that failed).
//
// Per code: remedy is errcode's; kinds, retry_after_s and part of statuses
// are read from the kinds answered with it (handler.KindSpecs); the rest of
// statuses from the answers the handlers and middleware write directly,
// which scan.go reads from the source — fail-closed: a writer call whose
// status or code is not a literal stops the command (exit 2) with its place.
// Every (status, code) must fit the code's remedy (remedyAllows). retryable is
// "always" or "never", read from the remedy: a kind whose Retryable disagrees
// with its code's remedy (one code, two meanings) stops the command, so the
// contract has no third value.
// retry_after_s is the kind table's Retry-After when every kind of the code
// has the same one, else null; an answer's own Retry-After header /
// retry_after_s always wins over it.
//
// extra (#3272) is the envelope fields beyond the fixed ones the code's
// answers may carry, with their JSON types: internal/errcode's declarations
// (extra.go). Every field a handler sets must be declared for the code it is
// sent under, and every declared field must be set somewhere — both read from
// the source by extra_scan.go — and no declared field may be named like a
// fixed envelope field (handler.EnvelopeFieldNames); otherwise the command
// stops (exit 2). The same declarations are written into the OpenAPI
// ErrorResponse in docs/ (swagger.go), which --check verifies as well.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/handler"
)

// contractRel is the contract's path from the repository root.
const contractRel = "tools/portal/src/interactive/tools/_common/data/tenant-api-errors.json"

// docsRel is swag's output directory, from the module root.
const docsRel = "docs"

const generatedNote = "GENERATED from components/tenant-api internal/errcode, the handler's kind table and the direct answers " +
	"by cmd/gen-error-contract — DO NOT EDIT. Run `make error-contract`."

// The values of entry.Retryable.
const (
	retryAlways = "always"
	retryNever  = "never"
)

type contract struct {
	Generated string  `json:"_generated"`
	Codes     []entry `json:"codes"`
}

type entry struct {
	Code        errcode.Code   `json:"code"`
	Remedy      errcode.Remedy `json:"remedy"`
	Retryable   string         `json:"retryable"`
	Statuses    []int          `json:"statuses"`
	RetryAfterS *int           `json:"retry_after_s"`
	Kinds       []string       `json:"kinds"`
	Extra       []extraEntry   `json:"extra"`
}

// extraEntry is one envelope field a code's answers may carry (#3272).
type extraEntry struct {
	Name  string            `json:"name"`
	Type  errcode.ExtraType `json:"type"`
	Items errcode.ExtraType `json:"items,omitempty"`
}

// remedyAllows is the statuses an answer with a code of that remedy may
// have: a client reading the status alone must not be told the opposite of
// what the code says (a "resend" code on a 400, a "fix the request" code on
// a 503). To allow a status for a remedy (for example 412/423 for
// refresh_retry, or 410 for not_found), change it here.
func remedyAllows(r errcode.Remedy, status int) bool {
	switch r {
	case errcode.RemedyRefreshRetry, errcode.RemedyChooseOther, errcode.RemedyExisting:
		return status == 409
	case errcode.RemedyNotFound:
		return status == 404
	case errcode.RemedyRetryAfter:
		return status == 429 || status >= 500
	case errcode.RemedyFixRequest, errcode.RemedyPermission:
		return status >= 400 && status < 500
	case errcode.RemedyRefresh:
		return status == 409 || status >= 500
	case errcode.RemedyFixInRepo:
		return (status >= 400 && status < 500) || status == 500
	case errcode.RemedyOperator:
		return status >= 400
	}
	return false
}

// build is the contract for codes, kinds and the direct answers: one entry
// per code, sorted by code. A kind or direct answer with a code codes does
// not list, or with a status its code's remedy does not allow, is an error.
func build(codes []errcode.Spec, kinds []handler.KindSpec, direct []directUse) (contract, error) {
	byCode := map[errcode.Code][]handler.KindSpec{}
	known := map[errcode.Code]bool{}
	for _, c := range codes {
		known[c.Code] = true
	}
	for _, k := range kinds {
		if k.Code == "" {
			continue // not a failure (errkind.NoChanges)
		}
		if !known[k.Code] {
			return contract{}, fmt.Errorf("kind %q is answered with code %q, which internal/errcode does not list", k.Kind, k.Code)
		}
		byCode[k.Code] = append(byCode[k.Code], k)
	}
	remedy := map[errcode.Code]errcode.Remedy{}
	for _, c := range codes {
		remedy[c.Code] = c.Remedy
	}
	var bad []string
	directStatuses := map[errcode.Code][]int{}
	for _, d := range direct {
		if !known[d.Code] {
			return contract{}, fmt.Errorf("%s answers code %q, which internal/errcode does not list", d.Site, d.Code)
		}
		if !remedyAllows(remedy[d.Code], d.Status) {
			bad = append(bad, fmt.Sprintf("%s: %d %s (remedy %s)", d.Site, d.Status, d.Code, remedy[d.Code]))
		}
		directStatuses[d.Code] = append(directStatuses[d.Code], d.Status)
	}
	for _, k := range kinds {
		if k.Code != "" && !remedyAllows(remedy[k.Code], k.Status) {
			bad = append(bad, fmt.Sprintf("kind %s: %d %s (remedy %s)", k.Kind, k.Status, k.Code, remedy[k.Code]))
		}
	}
	if len(bad) > 0 {
		return contract{}, fmt.Errorf("answers whose status contradicts their code's remedy:\n  %s", strings.Join(bad, "\n  "))
	}
	for _, k := range kinds {
		if k.Code != "" && k.Retryable != remedy[k.Code].Retryable() {
			bad = append(bad, fmt.Sprintf("kind %s: retryable %t, but code %s (remedy %s) says %t",
				k.Kind, k.Retryable, k.Code, remedy[k.Code], remedy[k.Code].Retryable()))
		}
	}
	if len(bad) > 0 {
		return contract{}, fmt.Errorf("kinds whose retryable contradicts their code's remedy (one code, two meanings):\n  %s", strings.Join(bad, "\n  "))
	}
	out := contract{Generated: generatedNote, Codes: make([]entry, 0, len(codes))}
	for _, c := range codes {
		e := entry{Code: c.Code, Remedy: c.Remedy, Statuses: []int{}, Kinds: []string{}, Extra: []extraEntry{}}
		for _, name := range c.Extra {
			f, ok := errcode.LookupExtraField(name)
			if !ok {
				return contract{}, fmt.Errorf("code %s carries Extra %q, which internal/errcode does not declare", c.Code, name)
			}
			e.Extra = append(e.Extra, extraEntry{Name: f.Name, Type: f.Type, Items: f.Items})
		}
		slices.SortFunc(e.Extra, func(a, b extraEntry) int { return strings.Compare(a.Name, b.Name) })
		ks := byCode[c.Code]
		var retryAfter []int
		for _, k := range ks {
			if !slices.Contains(e.Statuses, k.Status) {
				e.Statuses = append(e.Statuses, k.Status)
			}
			e.Kinds = append(e.Kinds, string(k.Kind))
			retryAfter = append(retryAfter, k.RetryAfterS)
		}
		for _, st := range directStatuses[c.Code] {
			if !slices.Contains(e.Statuses, st) {
				e.Statuses = append(e.Statuses, st)
			}
		}
		slices.Sort(e.Statuses)
		slices.Sort(e.Kinds)
		e.Retryable = retryNever
		if c.Remedy.Retryable() {
			e.Retryable = retryAlways
		}
		if len(retryAfter) > 0 && retryAfter[0] > 0 && !slices.ContainsFunc(retryAfter, func(s int) bool { return s != retryAfter[0] }) {
			e.RetryAfterS = &retryAfter[0]
		}
		out.Codes = append(out.Codes, e)
	}
	slices.SortFunc(out.Codes, func(a, b entry) int { return strings.Compare(string(a.Code), string(b.Code)) })
	return out, nil
}

// checkExtra holds the Extra fields the handlers set (uses, extra_scan.go) to
// codes' declarations: each is declared for the code it is sent under — a
// kind's code is the kind table's — and each declared (code, field) is set
// by some handler. No declared field may be one of fixed, the envelope's
// fixed field names: it would replace that field.
func checkExtra(codes []errcode.Spec, kinds []handler.KindSpec, uses []extraUse, fixed []string) error {
	kindCode := map[errkind.Kind]errcode.Code{}
	for _, k := range kinds {
		kindCode[k.Kind] = k.Code
	}
	declared := map[errcode.Code][]string{}
	var bad []string
	for _, c := range codes {
		declared[c.Code] = c.Extra
		for _, f := range c.Extra {
			if slices.Contains(fixed, f) {
				bad = append(bad, fmt.Sprintf("code %s declares Extra %q, a fixed envelope field (it would replace it)", c.Code, f))
			}
		}
	}
	set := map[errcode.Code]map[string]bool{}
	for _, u := range uses {
		code, via := u.Code, ""
		if u.Kind != "" {
			c, ok := kindCode[u.Kind]
			if !ok || c == "" {
				bad = append(bad, fmt.Sprintf("%s: Extra %q for kind %s, which the kind table answers with no code", u.Site, u.Field, u.Kind))
				continue
			}
			code, via = c, " (kind "+string(u.Kind)+")"
		}
		if !slices.Contains(declared[code], u.Field) {
			bad = append(bad, fmt.Sprintf("%s: sets Extra %q on code %s%s, which internal/errcode does not declare for it (extra.go codeExtra)",
				u.Site, u.Field, code, via))
			continue
		}
		if set[code] == nil {
			set[code] = map[string]bool{}
		}
		set[code][u.Field] = true
	}
	for _, c := range codes {
		for _, f := range c.Extra {
			if !set[c.Code][f] {
				bad = append(bad, fmt.Sprintf("code %s declares Extra %q, which no handler sets for it; drop it from extra.go codeExtra", c.Code, f))
			}
		}
	}
	if len(bad) > 0 {
		slices.Sort(bad)
		return fmt.Errorf("the Extra fields disagree with internal/errcode's declarations:\n  %s", strings.Join(bad, "\n  "))
	}
	return nil
}

// render is c as the committed bytes: two-space JSON, LF, a final newline.
func render(c contract) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(c); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// moduleRoot is the tenant-api module root, found by walking up from dir to
// its go.mod; the repository root is two levels above it.
func moduleRoot(dir string) (string, error) {
	for d := dir; ; d = filepath.Dir(d) {
		mod, err := os.ReadFile(filepath.Join(d, "go.mod"))
		if err == nil {
			if !bytes.Contains(mod, []byte("module github.com/vencil/tenant-api\n")) {
				return "", fmt.Errorf("%s/go.mod is not the tenant-api module; run from components/tenant-api", d)
			}
			return d, nil
		}
		if !errors.Is(err, os.ErrNotExist) {
			return "", err
		}
		if filepath.Dir(d) == d {
			return "", errors.New("no go.mod above the working directory; run from components/tenant-api")
		}
	}
}

func run(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("gen-error-contract", flag.ContinueOnError)
	fs.SetOutput(stderr)
	check := fs.Bool("check", false, "verify the committed contract matches; exit 1 on drift")
	out := fs.String("out", "", "write (or check) this file instead of the repository's "+contractRel)
	docs := fs.String("docs", "", "patch (or check) the swag output in this directory instead of the module's "+docsRel+"/")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(stderr, "ERROR: unexpected arguments %q\n", fs.Args())
		return 2
	}
	wd, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	mod, err := moduleRoot(wd)
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	path, shown := *out, *out
	if path == "" {
		path = filepath.Join(filepath.Dir(filepath.Dir(mod)), filepath.FromSlash(contractRel))
		shown = contractRel
	}
	direct, err := scanDirect(mod)
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	extras, err := scanExtra(mod)
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	specs, fixed := errcode.Specs(), handler.EnvelopeFieldNames()
	if err := checkExtra(specs, handler.KindSpecs(), extras, fixed); err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	c, err := build(specs, handler.KindSpecs(), direct)
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	want, err := render(c)
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	docsDir, docsShown := *docs, *docs
	if docsDir == "" {
		docsDir = filepath.Join(mod, docsRel)
		docsShown = "components/tenant-api/" + docsRel
	}
	jsonDoc, err := os.ReadFile(filepath.Join(docsDir, swaggerJSON))
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	goDoc, err := os.ReadFile(filepath.Join(docsDir, swaggerGo))
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	wantDocs, err := patchedDocs(jsonDoc, goDoc, errcode.ExtraFields(), specs, fixed)
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	docNames := []string{swaggerJSON, swaggerGo, swaggerYAML}
	if *check {
		rc := 0
		got, err := os.ReadFile(path)
		switch {
		case errors.Is(err, os.ErrNotExist):
			fmt.Fprintf(stderr, "DRIFT: %s is missing; run `make error-contract`\n", shown)
			rc = 1
		case err != nil:
			fmt.Fprintf(stderr, "ERROR: %v\n", err)
			return 2
		case !bytes.Equal(got, want):
			fmt.Fprintf(stderr, "DRIFT: %s is stale vs internal/errcode, the kind table and the direct answers; run `make error-contract`\n", shown)
			rc = 1
		}
		for _, n := range docNames {
			got, err := os.ReadFile(filepath.Join(docsDir, n))
			if err != nil && !errors.Is(err, os.ErrNotExist) {
				fmt.Fprintf(stderr, "ERROR: %v\n", err)
				return 2
			}
			if !bytes.Equal(got, wantDocs[n]) {
				fmt.Fprintf(stderr, "DRIFT: %s/%s lacks the ErrorResponse Extra properties of internal/errcode; run `make api-docs`\n", docsShown, n)
				rc = 1
			}
		}
		if rc == 0 {
			fmt.Fprintf(stdout, "OK: %s and the ErrorResponse of %s match internal/errcode, the kind table and the direct answers (%d codes).\n", shown, docsShown, len(c.Codes))
		}
		return rc
	}
	if err := os.WriteFile(path, want, 0o644); err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	for _, n := range docNames {
		if err := os.WriteFile(filepath.Join(docsDir, n), wantDocs[n], 0o644); err != nil {
			fmt.Fprintf(stderr, "ERROR: %v\n", err)
			return 2
		}
	}
	fmt.Fprintf(stdout, "WROTE: %s (%d codes) and the ErrorResponse Extra properties of %s\n", shown, len(c.Codes), docsShown)
	return 0
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}
