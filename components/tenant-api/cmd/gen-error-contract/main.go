// Command gen-error-contract writes the client contract of tenant-api's error
// codes (#3202): tools/portal/src/interactive/tools/_common/data/tenant-api-errors.json,
// one entry per code of internal/errcode with its remedy and what the
// handler's kind table answers it with. The portal reads it instead of
// writing the codes down a second time.
//
//	go run ./cmd/gen-error-contract           # write the contract
//	go run ./cmd/gen-error-contract --check   # exit 1 if the committed copy drifted
//
// Run from components/tenant-api (`make error-contract` does). Exit codes:
// 0 wrote / in sync · 1 drift (--check) · 2 caller error (bad arguments, the
// module or the output not found, a write that failed).
//
// Per code: remedy is errcode's; statuses, retry_after_s and kinds are read
// from the kinds answered with it (handler.KindSpecs). retryable is "always"
// or "never" when every such kind agrees (and, for a code no kind answers —
// a code only a handler or middleware writes directly — from the remedy);
// "depends" would mean one code with two meanings, which the handler's
// TestKindTable_OneCodeOneMeaning and this command's tests refuse.
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

const generatedNote = "GENERATED from components/tenant-api internal/errcode and the handler's kind table " +
	"by cmd/gen-error-contract — DO NOT EDIT. Run `make error-contract`."

// The values of entry.Retryable.
const (
	retryAlways  = "always"
	retryNever   = "never"
	retryDepends = "depends"
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

// build is the contract for codes and kinds: one entry per code, sorted by
// code. A kind answered with a code codes does not list is an error.
func build(codes []errcode.Spec, kinds []handler.KindSpec) (contract, error) {
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
	out := contract{Generated: generatedNote, Codes: make([]entry, 0, len(codes))}
	for _, c := range codes {
		e := entry{Code: c.Code, Remedy: c.Remedy, Statuses: []int{}, Kinds: []string{}}
		ks := byCode[c.Code]
		retryable, notRetryable := 0, 0
		var retryAfter []int
		for _, k := range ks {
			if !slices.Contains(e.Statuses, k.Status) {
				e.Statuses = append(e.Statuses, k.Status)
			}
			e.Kinds = append(e.Kinds, string(k.Kind))
			if k.Retryable {
				retryable++
			} else {
				notRetryable++
			}
			retryAfter = append(retryAfter, k.RetryAfterS)
		}
		slices.Sort(e.Statuses)
		slices.Sort(e.Kinds)
		switch {
		case len(ks) == 0 && c.Remedy.Retryable(), retryable > 0 && notRetryable == 0:
			e.Retryable = retryAlways
		case len(ks) == 0, notRetryable > 0 && retryable == 0:
			e.Retryable = retryNever
		default:
			e.Retryable = retryDepends
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

// repoRoot is the repository root: two levels above the tenant-api module
// root, found by walking up from dir to its go.mod.
func repoRoot(dir string) (string, error) {
	for d := dir; ; d = filepath.Dir(d) {
		mod, err := os.ReadFile(filepath.Join(d, "go.mod"))
		if err == nil {
			if !bytes.Contains(mod, []byte("module github.com/vencil/tenant-api\n")) {
				return "", fmt.Errorf("%s/go.mod is not the tenant-api module; run from components/tenant-api", d)
			}
			return filepath.Dir(filepath.Dir(d)), nil
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
	path, shown := *out, *out
	if path == "" {
		wd, err := os.Getwd()
		if err != nil {
			fmt.Fprintf(stderr, "ERROR: %v\n", err)
			return 2
		}
		root, err := repoRoot(wd)
		if err != nil {
			fmt.Fprintf(stderr, "ERROR: %v\n", err)
			return 2
		}
		path, shown = filepath.Join(root, filepath.FromSlash(contractRel)), contractRel
	}
	c, err := build(errcode.Specs(), handler.KindSpecs())
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
			fmt.Fprintf(stderr, "DRIFT: %s is stale vs internal/errcode and the kind table; run `make error-contract`\n", shown)
			return 1
		}
		fmt.Fprintf(stdout, "OK: %s matches internal/errcode and the kind table (%d codes).\n", shown, len(c.Codes))
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
