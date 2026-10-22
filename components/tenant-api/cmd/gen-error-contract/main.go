// Command gen-error-contract writes the client contract of tenant-api's error
// codes (#3202): tools/portal/src/interactive/tools/_common/data/tenant-api-errors.json,
// one entry per code of internal/errcode with its remedy and what the
// handler's kind table and the direct answers write it with — for the portal
// to read instead of writing the codes down a second time (it does, through
// tools/portal/src/interactive/tools/_common/api/classify-error.js).
//
//	go run ./cmd/gen-error-contract           # write the contract
//	go run ./cmd/gen-error-contract --check   # exit 1 if the committed copy drifted
//
// Run from components/tenant-api (`make error-contract` does). Exit codes:
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
	"github.com/vencil/tenant-api/internal/handler"
)

// contractRel is the contract's path from the repository root.
const contractRel = "tools/portal/src/interactive/tools/_common/data/tenant-api-errors.json"

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
		e := entry{Code: c.Code, Remedy: c.Remedy, Statuses: []int{}, Kinds: []string{}}
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
	c, err := build(errcode.Specs(), handler.KindSpecs(), direct)
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	want, err := render(c)
	if err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	if *check {
		got, err := os.ReadFile(path)
		switch {
		case errors.Is(err, os.ErrNotExist):
			fmt.Fprintf(stderr, "DRIFT: %s is missing; run `make error-contract`\n", shown)
			return 1
		case err != nil:
			fmt.Fprintf(stderr, "ERROR: %v\n", err)
			return 2
		case !bytes.Equal(got, want):
			fmt.Fprintf(stderr, "DRIFT: %s is stale vs internal/errcode, the kind table and the direct answers; run `make error-contract`\n", shown)
			return 1
		}
		fmt.Fprintf(stdout, "OK: %s matches internal/errcode, the kind table and the direct answers (%d codes).\n", shown, len(c.Codes))
		return 0
	}
	if err := os.WriteFile(path, want, 0o644); err != nil {
		fmt.Fprintf(stderr, "ERROR: %v\n", err)
		return 2
	}
	fmt.Fprintf(stdout, "WROTE: %s (%d codes)\n", shown, len(c.Codes))
	return 0
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}
