// da-read reads a configuration file with the loader of the program that
// consumes it, and prints that program's reading as JSON (ADR-036 §12,
// #3206). The Python tools that need such a file ask this binary instead of
// reading the YAML themselves, so the file means to them what it means to its
// consumer.
//
// It is its own Go module so that the consumers' libraries stay out of the
// two production modules (threshold-exporter, tenant-api), whose go.mod feeds
// image builds, Trivy scans and release provenance — the rule
// tests/alertmanager-inhibit/go.mod states for the Alertmanager dependency.
//
// Usage:
//
//	da-read alertmanager [--show-loader-reason] <file|->
//	da-read prometheus <file|->
//	da-read prometheus-rules <file|->...
//	da-read promql-selectors <series> -
//	da-read promql-label-flow <label> -
//
// When Alertmanager's loader refuses a file, `alertmanager` prints a fixed
// sentence, not the loader's reason: that text is Alertmanager's own prose
// and quotes the file's values (#3703). --show-loader-reason prints the
// reason too — for a person reading their own file, never a caller that
// relays the message further.
//
// `alertmanager` reads the file with Alertmanager's own config loader
// (github.com/prometheus/alertmanager/config.Load, the version of the amtool
// in the da-tools image) and `prometheus` with Prometheus' own
// (github.com/prometheus/prometheus/config.Load, every service discovery
// registered, the version of the deployed Prometheus); version pin tests hold
// both. Each prints
//
//	{"documents": [<the config, as its consumer loads it>]}
//
// keeping only the keys the text writes (alertmanager.go, yamlwalk.go say
// how).
//
// `prometheus-rules` reads each rule file with rulefmt.Parse, as Prometheus'
// rule manager loads it (prometheus_rules.go), and prints one entry per
// argument, in argument order:
//
//	{"answer": "refused" (only when a file is refused),
//	 "files": [{"path": <as given; "<stdin>" for ->,
//	            "documents": [<the rule groups>] or null,
//	            "problem": null or {"message": <why the loader refuses it>}}]}
//
// `documents` and `problem` are never both null and never both set. A file
// that cannot be opened is no entry: the run prints nothing (exit codes).
//
// `promql-selectors` reads a JSON array of PromQL expressions on standard
// input and prints, per expression, the vector selectors that could select
// the series <series> as Prometheus' parser and label matchers read them
// (promql_selectors.go): {"documents": [[[{"selector", "start", "end"}, …], …]]}.
//
// `promql-label-flow` reads a JSON object on standard input — the PromQL
// "expressions", the "records" they may read and the "aliases" of <label>
// (promql_label_flow.go) — and prints, per expression, every `and` /
// `unless` matching on <label> and whether its left side keeps <label>:
// {"documents": [[[{"op", "start", "end", "lhs"}, …], …]]}.
//
// `-` reads standard input — how a file held in a ConfigMap is handed over
// once its outer layer is read the Kubernetes way (da-crdecode); for
// `prometheus-rules` at most once.
//
// Exit codes (every subcommand):
//
//	0  every file read; JSON on stdout
//	2  the input is refused, or da-read could not answer. Only a refusal
//	   prints a refusal document on stdout (#3943):
//
//	     {"answer": "refused"}
//
//	   — a YAML document after the first that has content, the consumer's
//	   loader refuses the input (or panics on it), a key the text writes is
//	   not in the consumer's reading, an expression the PromQL parser
//	   refuses. `prometheus-rules` prints its whole JSON with that `answer`
//	   beside `files`, the refused files with their problem. The document
//	   holds only those constants, never text of the input; the reason is on
//	   stderr, prefixed with the file's name. Bad arguments (the usage), a
//	   file that cannot be opened, standard input that cannot be read or
//	   stdout that cannot be written print no refusal document: they are no
//	   answer about the input, and a caller must not take them for one.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
)

// programName prefixes every stderr line.
const programName = "da-read"

const (
	exitOK        = 0
	exitCallerErr = 2
)

// stdinName is how standard input is named in messages and in `path`.
const stdinName = "<stdin>"

// answerRefused is the `answer` of the refusal document (#3943): printed on
// stdout, with exit 2, only when the input is refused — never for a usage
// error, a file that cannot be opened or an I/O failure, which exit 2 too.
// The caller (scripts/tools/_lib_consumer_read.py, REFUSAL_KEY /
// REFUSED) takes exit 2 as a refusal only with this document, so it never
// reads a run that could not answer as an answer about the input.
// da-crdecode prints the same document; tests/shared/
// test_reader_refusal_document.py holds both binaries to the caller's
// definition. ⛔ Constants only: no text of the input (ADR-036, #3703).
const answerRefused = "refused"

// refusal is the refusal document of the single-input subcommands.
type refusal struct {
	Answer string `json:"answer"`
}

// refuse writes the refusal document and exits 2.
func refuse(stdout, stderr io.Writer) int {
	b, err := json.Marshal(refusal{Answer: answerRefused})
	if err != nil {
		say(stderr, "%s: %v\n", programName, err)
		return exitCallerErr
	}
	return write(stdout, stderr, append(b, '\n'), exitCallerErr)
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

// say writes one message to stderr; there is nowhere to report a failed
// write of it.
func say(stderr io.Writer, format string, args ...interface{}) {
	_, _ = fmt.Fprintf(stderr, format, args...)
}

func usage(stderr io.Writer) int {
	say(stderr, "usage: %s alertmanager [%s] <file|->\n", programName, showLoaderReasonFlag)
	say(stderr, "       %s prometheus <file|->\n", programName)
	say(stderr, "       %s prometheus-rules <file|->...\n", programName)
	say(stderr, "       %s promql-selectors <series> -\n", programName)
	say(stderr, "       %s promql-label-flow <label> -\n", programName)
	say(stderr, "Reads an Alertmanager config, a prometheus.yml or Prometheus rule files with "+
		"their consumer's own loader and prints the reading as JSON, keeping only the keys "+
		"the file writes: {\"documents\": [<config>]}, or for prometheus-rules "+
		"{\"files\": [{\"path\", \"documents\", \"problem\"}]}. `-` reads standard input "+
		"(at most once). promql-selectors reads a JSON array of PromQL expressions on "+
		"standard input and prints, per expression, the selectors that could select "+
		"<series>; promql-label-flow reads {\"expressions\", \"records\", \"aliases\"} and prints, per expression, whether the left side of "+
		"each `and`/`unless on(<label>)` keeps <label>. Exit 2 when the input is refused, "+
		"with {\"answer\": \"refused\"} on stdout (prometheus-rules: beside \"files\"); "+
		"exit 2 without it when da-read could not answer (bad arguments, a file that cannot be "+
		"opened, an I/O error). A refusal by Alertmanager's loader leaves the loader's reason "+
		"out (it can quote the file's values) unless "+showLoaderReasonFlag+" is given.\n")
	return exitCallerErr
}

// readers are the single-file subcommands.
var readers = map[string]func([]byte) ([]byte, error){
	"alertmanager": func(d []byte) ([]byte, error) { return readAlertmanager(d, false) },
	"prometheus":   readPrometheus,
}

func isFileArg(a string) bool {
	return a != "" && a != "-h" && a != "--help"
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) < 2 {
		return usage(stderr)
	}
	for _, a := range args[1:] {
		if !isFileArg(a) {
			return usage(stderr)
		}
	}
	if args[0] == "prometheus-rules" {
		return runRules(args[1:], stdin, stdout, stderr)
	}
	if args[0] == "promql-label-flow" {
		if len(args) != 3 || args[2] != "-" {
			return usage(stderr)
		}
		data, err := io.ReadAll(stdin)
		if err != nil {
			say(stderr, "%s: %v\n", programName, err)
			return exitCallerErr
		}
		out, err := guarded(func(d []byte) ([]byte, error) { return readPromQLLabelFlow(args[1], d) }, data)
		if err != nil {
			say(stderr, "%s: %s: %v\n", programName, stdinName, err)
			return refuse(stdout, stderr)
		}
		return write(stdout, stderr, append(out, '\n'), exitOK)
	}
	if args[0] == "promql-selectors" {
		if len(args) != 3 || args[2] != "-" {
			return usage(stderr)
		}
		data, err := io.ReadAll(stdin)
		if err != nil {
			say(stderr, "%s: %v\n", programName, err)
			return exitCallerErr
		}
		out, err := guarded(func(d []byte) ([]byte, error) { return readPromQLSelectors(args[1], d) }, data)
		if err != nil {
			say(stderr, "%s: %s: %v\n", programName, stdinName, err)
			return refuse(stdout, stderr)
		}
		return write(stdout, stderr, append(out, '\n'), exitOK)
	}
	read, ok := readers[args[0]]
	if args[0] == "alertmanager" && args[1] == showLoaderReasonFlag {
		if len(args) != 3 {
			return usage(stderr)
		}
		read = func(d []byte) ([]byte, error) { return readAlertmanager(d, true) }
		args = []string{args[0], args[2]}
	}
	if !ok || len(args) != 2 {
		return usage(stderr)
	}
	name, data, err := open(args[1], stdin)
	if err != nil {
		say(stderr, "%s: %v\n", programName, err)
		return exitCallerErr
	}
	out, err := guarded(read, data)
	if err != nil {
		say(stderr, "%s: %s: %v\n", programName, name, err)
		return refuse(stdout, stderr)
	}
	return write(stdout, stderr, append(out, '\n'), exitOK)
}

// open reads the file argument *arg* (`-`: standard input) and names it.
func open(arg string, stdin io.Reader) (string, []byte, error) {
	if arg == "-" {
		data, err := io.ReadAll(stdin)
		return stdinName, data, err
	}
	data, err := os.ReadFile(arg)
	return arg, data, err
}

func write(stdout, stderr io.Writer, out []byte, rc int) int {
	if _, err := stdout.Write(out); err != nil {
		say(stderr, "%s: writing stdout: %v\n", programName, err)
		return exitCallerErr
	}
	return rc
}

// guarded is read(data) with any panic turned into a refusal of the file:
// a loader that panics on one input (Prometheus' config.Load does, on
// `alerting: {alertmanagers: [~]}`; the readers name their own loader,
// loaderPanic) refuses that file, with rc 2, the refusal document and no
// goroutine dump. ⛔ That is a refusal, not a fault of da-read (#3943): the
// consumer's own loader fails on the same file the same way, so "the
// consumer does not read this input" is the answer.
func guarded(read func([]byte) ([]byte, error), data []byte) (out []byte, err error) {
	defer func() {
		if r := recover(); r != nil {
			out, err = nil, fmt.Errorf("da-read panicked while reading this file (a bug; the file is refused): %v", r)
		}
	}()
	return read(data)
}

// ruleFile is one entry of `prometheus-rules`' output.
type ruleFile struct {
	Path      string            `json:"path"`
	Documents []json.RawMessage `json:"documents"`
	Problem   *problem          `json:"problem"`
}

type problem struct {
	Message string `json:"message"`
}

func runRules(files []string, stdin io.Reader, stdout, stderr io.Writer) int {
	stdinSeen := false
	for _, f := range files {
		if f == "-" {
			if stdinSeen {
				say(stderr, "%s: prometheus-rules: `-` (standard input) can be given once\n", programName)
				return usage(stderr)
			}
			stdinSeen = true
		}
	}
	// `answer` is the refusal document's (answerRefused) when a file is
	// refused, and absent when every file is read.
	out := struct {
		Answer string     `json:"answer,omitempty"`
		Files  []ruleFile `json:"files"`
	}{Files: make([]ruleFile, 0, len(files))}
	rc := exitOK
	for _, f := range files {
		name, data, err := open(f, stdin)
		if err != nil {
			// No answer about the file: nothing is printed (#3943).
			say(stderr, "%s: %v\n", programName, err)
			return exitCallerErr
		}
		entry := ruleFile{Path: name}
		doc, err := guarded(readRuleFile, data)
		if err != nil {
			out.Answer = answerRefused
			entry.Problem = &problem{Message: err.Error()}
			say(stderr, "%s: %s: %v\n", programName, name, err)
			rc = exitCallerErr
		} else {
			entry.Documents = []json.RawMessage{doc}
		}
		out.Files = append(out.Files, entry)
	}
	b, err := json.Marshal(out)
	if err != nil {
		say(stderr, "%s: %v\n", programName, err)
		return exitCallerErr
	}
	return write(stdout, stderr, append(b, '\n'), rc)
}
