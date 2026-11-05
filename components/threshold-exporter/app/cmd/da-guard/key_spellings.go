package main

// key-spellings: every #1231 spelling of each of a list of tenant-config
// keys, as JSON (#3556), so a Python reader that compares keys across
// spellings (Policy-as-Code, diagnose, backtest-threshold,
// threshold-recommend) asks the exporter's rule instead of keeping a copy.
//
// ⛔ NO SEMANTICS OF ITS OWN. Every field is config.SpellingsOf's, which is
// canonicalKeyFor and legacySpellingFor — the functions the exporter's
// merge, resolve and validation run.

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"unicode/utf8"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// keySpellingsCmd is the subcommand name: `da-guard key-spellings`.
const keySpellingsCmd = "key-spellings"

// keySpellingOut is one element of the stdout array, in input order.
type keySpellingOut struct {
	Key string `json:"key"`
	// Canonical: the key in the exporter's canonical spelling (Key itself
	// when no alias touches it).
	Canonical string `json:"canonical"`
	// Spellings: every spelling of the same threshold, Canonical first.
	Spellings []string `json:"spellings"`
}

func keySpellingsUsage(errOut io.Writer, fs *flag.FlagSet) func() {
	return func() {
		fmt.Fprintf(errOut, "Usage: %s %s < keys.json\n", programName, keySpellingsCmd)
		fmt.Fprintf(errOut, "Read, from stdin, a JSON array of tenant-config keys (strings) and print, as a JSON array\n"+
			"in the same order, {key, canonical, spellings} for each: canonical is the key in the exporter's\n"+
			"canonical spelling (a retired #1231 alias applied to the key's head, exactly or with _critical,\n"+
			"a {...} part kept as written), spellings every spelling of the same threshold, canonical first\n"+
			"([key] when no alias touches it).\n\n")
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok\n  2  caller error: an argument, stdin unreadable, or stdin not a JSON array of strings\n")
	}
}

// runKeySpellings is the subcommand's entry point.
func runKeySpellings(args []string, stdin io.Reader, stdout, errOut io.Writer) int {
	fs := flag.NewFlagSet(programName+" "+keySpellingsCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	fs.Usage = keySpellingsUsage(errOut, fs)
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitCallerErr
	}
	fail := func(format string, a ...any) int {
		fmt.Fprintf(errOut, "%s %s: "+format+"\n", append([]any{programName, keySpellingsCmd}, a...)...)
		return exitCallerErr
	}
	if fs.NArg() > 0 {
		return fail("unexpected argument %q (the keys are read from stdin)", fs.Arg(0))
	}
	raw, err := io.ReadAll(stdin)
	if err != nil {
		return fail("stdin: %v", err)
	}
	keys, err := decodeKeyList(raw)
	if err != nil {
		return fail("stdin: %v", err)
	}
	out := make([]keySpellingOut, 0, len(keys))
	for _, k := range keys {
		canon, spellings := config.SpellingsOf(k)
		out = append(out, keySpellingOut{Key: k, Canonical: canon, Spellings: spellings})
	}
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(out); err != nil {
		return fail("%v", err)
	}
	if _, err := stdout.Write(b.Bytes()); err != nil {
		return fail("stdout: %v", err)
	}
	return exitOK
}

// errNotKeyList is every refusal of decodeKeyList.
var errNotKeyList = errors.New("not a JSON array of strings")

// decodeKeyList reads raw as exactly one JSON array whose elements are all
// strings. Anything else — another JSON value, null, a non-string element,
// trailing data, bytes that are not UTF-8 (encoding/json would replace them
// with U+FFFD and answer for a key nobody asked) — is an error, never a
// partial answer.
func decodeKeyList(raw []byte) ([]string, error) {
	if !utf8.Valid(raw) {
		return nil, fmt.Errorf("%w: not UTF-8", errNotKeyList)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if t, err := dec.Token(); err != nil || t != json.Delim('[') {
		return nil, fmt.Errorf("%w: does not start with [ (%v)", errNotKeyList, tokenOrErr(t, err))
	}
	keys := []string{}
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return nil, fmt.Errorf("%w: element %d: %v", errNotKeyList, len(keys), err)
		}
		k, ok := t.(string)
		if !ok {
			return nil, fmt.Errorf("%w: element %d is not a string (%v)", errNotKeyList, len(keys), tokenOrErr(t, nil))
		}
		keys = append(keys, k)
	}
	if _, err := dec.Token(); err != nil { // ']'
		return nil, fmt.Errorf("%w: %v", errNotKeyList, err)
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("%w: data after the array", errNotKeyList)
	}
	return keys, nil
}
