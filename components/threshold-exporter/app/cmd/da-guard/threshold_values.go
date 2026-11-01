package main

// threshold-values: how the exporter reads each of a list of threshold value
// texts, as JSON (#3267), for the unit-sanity lint — so that lint judges the
// number the exporter reads instead of reading the text with a Python copy
// of the exporter's grammar.
//
// ⛔ NO SEMANTICS OF ITS OWN. Every field of an answer is
// config.ReadThresholdText's, which is splitThresholdSeverity,
// parseThresholdValue (parseThresholdNumber) and RefusedValueAt — the
// readers the row resolvers run.

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// thresholdValuesCmd is the subcommand name: `da-guard threshold-values`.
const thresholdValuesCmd = "threshold-values"

// thresholdValueIn is one element of the stdin array.
type thresholdValueIn struct {
	// Key is the threshold key the text is written for, in canonical
	// spelling; only Refused depends on it (`<key>_critical` is read whole).
	Key *string `json:"key"`
	// Text is the value as written (`70`, `70:critical`, `disable`, ...).
	Text *string `json:"text"`
}

// thresholdValueOut is one element of the stdout array, in input order.
type thresholdValueOut struct {
	Key  string `json:"key"`
	Text string `json:"text"`
	// Number: the finite number the shared grammar reads (null: none).
	Number *float64 `json:"number"`
	// NonFinite: "NaN", "+Inf" or "-Inf" when the value half reads as one
	// (an overflow such as `1e999` included), else "". JSON has no
	// non-finite number, so it is spelled; Number is then null.
	NonFinite string `json:"non_finite"`
	// Severity: the suffix's severity ("warning" with none), "" when the
	// suffix is refused.
	Severity string `json:"severity"`
	// Refused: config.RefusedValueAt's reason for key ("" when read).
	Refused string `json:"refused"`
}

func thresholdValuesUsage(errOut io.Writer, fs *flag.FlagSet) func() {
	return func() {
		fmt.Fprintf(errOut, "Usage: %s %s < values.json\n", programName, thresholdValuesCmd)
		fmt.Fprintf(errOut, "Read, from stdin, a JSON array of {\"key\": \"<threshold key>\", \"text\": \"<value as written>\"}\n"+
			"and print, as a JSON array in the same order, how the exporter reads each text:\n"+
			"{key, text, number, non_finite, severity, refused}. number is the finite number the shared\n"+
			"value[:severity] grammar reads (null: none); non_finite is \"NaN\", \"+Inf\" or \"-Inf\" when the\n"+
			"number half reads as one (an overflow such as 1e999 included), else \"\"; severity is the suffix's\n"+
			"(\"warning\" with none; \"\" when the suffix is refused); refused is why the exporter's rows refuse\n"+
			"the value for key (\"non_finite\", \"unparsed\"; \"\" when read, a disable included) — a\n"+
			"<key>_critical value is read whole, so only refused depends on key.\n\n")
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok\n  2  caller error: an argument, stdin unreadable, or stdin not a JSON array of\n"+
			"     {key, text} string pairs\n")
	}
}

// runThresholdValues is the subcommand's entry point.
func runThresholdValues(args []string, stdin io.Reader, stdout, errOut io.Writer) int {
	fs := flag.NewFlagSet(programName+" "+thresholdValuesCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	fs.Usage = thresholdValuesUsage(errOut, fs)
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitCallerErr
	}
	fail := func(format string, a ...any) int {
		fmt.Fprintf(errOut, "%s %s: "+format+"\n", append([]any{programName, thresholdValuesCmd}, a...)...)
		return exitCallerErr
	}
	if fs.NArg() > 0 {
		return fail("unexpected argument %q (the values are read from stdin)", fs.Arg(0))
	}
	raw, err := io.ReadAll(stdin)
	if err != nil {
		return fail("stdin: %v", err)
	}
	in, err := decodeThresholdValues(raw)
	if err != nil {
		return fail("stdin: %v", err)
	}
	out := make([]thresholdValueOut, 0, len(in))
	for _, v := range in {
		out = append(out, thresholdValueOf(*v.Key, *v.Text))
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

// errNotPairs is every refusal of decodeThresholdValues.
var errNotPairs = errors.New("not a JSON array of {\"key\": string, \"text\": string}")

// decodeThresholdValues reads raw as exactly one JSON array of objects whose
// keys are exactly `key` and `text`, each once, each a string. Anything else
// — another JSON value, an element that is not such an object, a field
// missing, null, repeated or unknown (encoding/json would take `KEY` or
// `Key` for `key`, and the last of two; this reader takes neither), trailing
// data — is an error, never a partial answer.
func decodeThresholdValues(raw []byte) ([]thresholdValueIn, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	fail := func(format string, a ...any) ([]thresholdValueIn, error) {
		return nil, fmt.Errorf("%w: "+format, append([]any{errNotPairs}, a...)...)
	}
	if t, err := dec.Token(); err != nil || t != json.Delim('[') {
		return fail("does not start with [ (%v)", tokenOrErr(t, err))
	}
	in := []thresholdValueIn{}
	for dec.More() {
		i := len(in)
		if t, err := dec.Token(); err != nil || t != json.Delim('{') {
			return fail("element %d is not an object (%v)", i, tokenOrErr(t, err))
		}
		var v thresholdValueIn
		for dec.More() {
			kt, err := dec.Token()
			if err != nil {
				return fail("element %d: %v", i, err)
			}
			name, _ := kt.(string)
			vt, err := dec.Token()
			if err != nil {
				return fail("element %d: %v", i, err)
			}
			text, ok := vt.(string)
			if !ok {
				return fail("element %d: %q is not a string (%v)", i, name, tokenOrErr(vt, nil))
			}
			var slot **string
			switch name {
			case "key":
				slot = &v.Key
			case "text":
				slot = &v.Text
			default:
				return fail("element %d: field %q (only \"key\" and \"text\", exactly so spelt)", i, name)
			}
			if *slot != nil {
				return fail("element %d: field %q given twice", i, name)
			}
			*slot = &text
		}
		if _, err := dec.Token(); err != nil { // '}'
			return fail("element %d: %v", i, err)
		}
		if v.Key == nil || v.Text == nil {
			return fail("element %d needs both \"key\" and \"text\"", i)
		}
		in = append(in, v)
	}
	if _, err := dec.Token(); err != nil { // ']'
		return fail("%v", err)
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return fail("data after the array")
	}
	return in, nil
}

// tokenOrErr names what a refused token was, for the message.
func tokenOrErr(t json.Token, err error) string {
	if err != nil {
		return err.Error()
	}
	if t == nil {
		return "null"
	}
	return fmt.Sprintf("%v", t)
}

// thresholdValueOf is config.ReadThresholdText's answer in the output shape.
func thresholdValueOf(key, text string) thresholdValueOut {
	r := config.ReadThresholdText(key, text)
	out := thresholdValueOut{Key: key, Text: text, Severity: r.Severity, Refused: r.Refused}
	if r.HasNumber {
		n := r.Number
		out.Number = &n
	}
	if r.HasNonFinite {
		switch {
		case math.IsNaN(r.NonFinite):
			out.NonFinite = "NaN"
		case r.NonFinite > 0:
			out.NonFinite = "+Inf"
		default:
			out.NonFinite = "-Inf"
		}
	}
	return out
}
