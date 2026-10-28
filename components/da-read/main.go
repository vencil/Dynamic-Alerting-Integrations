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
//	da-read alertmanager <file|->
//
// `alertmanager` reads the file with Alertmanager's own config loader
// (github.com/prometheus/alertmanager/config.Load, the version of the amtool
// in the da-tools image; a version pin test holds the two equal) and prints
//
//	{"documents": [<the config, as Alertmanager loads it>]}
//
// keeping only the keys the text writes (alertmanager.go says how). `-` reads
// standard input — how the `alertmanager.yml` held in a ConfigMap is handed
// over once its outer layer is read the Kubernetes way (da-crdecode).
//
// Exit codes:
//
//	0  read; JSON on stdout
//	2  caller error, or the file is refused: bad arguments, the file cannot
//	   be read, a YAML document after the first has content, Alertmanager's loader
//	   refuses it, or a key the text writes is not in Alertmanager's reading
//	   (the reason is on stderr)
package main

import (
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

// stdinName is how standard input is named in messages.
const stdinName = "<stdin>"

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

// say writes one message to stderr; there is nowhere to report a failed
// write of it.
func say(stderr io.Writer, format string, args ...interface{}) {
	_, _ = fmt.Fprintf(stderr, format, args...)
}

func usage(stderr io.Writer) int {
	say(stderr, "usage: %s alertmanager <file|->\n", programName)
	say(stderr, "Reads an Alertmanager config with Alertmanager's own loader and prints "+
		"{\"documents\": [<config>]} as JSON, keeping only the keys the file writes. "+
		"`-` reads standard input.\n")
	return exitCallerErr
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) != 2 || args[0] != "alertmanager" || args[1] == "" ||
		args[1] == "-h" || args[1] == "--help" {
		return usage(stderr)
	}
	name := args[1]
	var data []byte
	var err error
	if name == "-" {
		name = stdinName
		data, err = io.ReadAll(stdin)
	} else {
		data, err = os.ReadFile(name)
	}
	if err != nil {
		say(stderr, "%s: %v\n", programName, err)
		return exitCallerErr
	}
	out, err := readAlertmanager(data)
	if err != nil {
		say(stderr, "%s: %s: %v\n", programName, name, err)
		return exitCallerErr
	}
	out = append(out, '\n')
	if _, err := stdout.Write(out); err != nil {
		say(stderr, "%s: writing stdout: %v\n", programName, err)
		return exitCallerErr
	}
	return exitOK
}
