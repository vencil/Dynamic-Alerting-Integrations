package main

import (
	"fmt"
	"io"
	"regexp"
	"sort"
	"strings"
)

// An unknown TA_* variable refuses the start (ADR-036 §12).
//
// The Helm chart passes tenant-api's settings as TA_* environment variables
// (so `kubectl exec ... tenant-api state show|restore` and `config check` read
// the server's settings). A command-line flag this build does not know stops
// it with a usage error; an environment variable it does not know would be
// ignored silently — a chart newer than its image would run with a setting
// quietly dropped. So the server and the `state` / `config` subcommands check
// the environment after parsing their flags: a TA_* name that no reader in
// this build reads is a usage error (exit 2), named, like an unknown flag.
//
// knownTAEnv (known_env.go) is generated from the reading sites themselves:
// TestKnownTAEnv_IsTheSourceList collects every TA_* name the module reads
// (the same go/ast walk env_readme_parity_test.go uses — envOrDefault,
// envBool, envNonEmptyOrDefault, parseDurationOrDefault(os.Getenv(..)) and
// every bare os.Getenv / os.LookupEnv) and fails when the list differs;
// `go test ./cmd/server -run TestKnownTAEnv -update-known-env` rewrites it.
// A list built at run time from the reads would miss the variables read only
// later (on a write, on a forge call) and those of the server's flags, which a
// subcommand never defines.

// serviceLinkRe matches the variables Kubernetes injects for every Service in
// the namespace (enableServiceLinks): a Service named `ta-x` yields
// TA_X_SERVICE_HOST, TA_X_PORT_8080_TCP and the like. They are not settings,
// and refusing them would make tenant-api fail on an unrelated Service name.
var serviceLinkRe = regexp.MustCompile(`_(SERVICE_HOST|SERVICE_PORT(_[A-Z0-9_]+)?|PORT(_[0-9]+_(TCP|UDP|SCTP)(_(ADDR|PORT|PROTO))?)?)$`)

// unknownTAEnv returns the TA_* names in environ (KEY=VALUE entries) that
// this build does not read, sorted and without duplicates.
func unknownTAEnv(environ []string) []string {
	known := make(map[string]bool, len(knownTAEnv))
	for _, n := range knownTAEnv {
		known[n] = true
	}
	seen := map[string]bool{}
	var out []string
	for _, kv := range environ {
		name, _, _ := strings.Cut(kv, "=")
		if !strings.HasPrefix(name, "TA_") || known[name] || seen[name] || serviceLinkRe.MatchString(name) {
			continue
		}
		seen[name] = true
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// checkTAEnv writes a usage error naming every unknown TA_* variable of
// environ to stderr and reports false; true when there is none.
func checkTAEnv(environ []string, stderr io.Writer) bool {
	unknown := unknownTAEnv(environ)
	if len(unknown) == 0 {
		return true
	}
	fmt.Fprintf(stderr, "tenant-api: unknown setting(s) in the environment: %s\n"+
		"This tenant-api does not read them. The image may be older than the Helm chart that set them "+
		"(the chart passes settings as TA_* variables): deploy the image the chart expects, or remove the "+
		"variable. Exit 2, as for an unknown flag.\n", strings.Join(unknown, ", "))
	return false
}
