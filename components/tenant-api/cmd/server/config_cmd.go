package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

// configCmdUsage is printed for `tenant-api config` misuse.
const configCmdUsage = `usage: tenant-api config check --kind rbac|tenant-orgs [--identity-claim-headers k=Header,...] FILE|-

Judges an admin config file with the parse function the server reads it with
(ADR-036 §12) and prints one JSON document (schema tenant-api.config-check/v1):
"config" is the decoded config, with the field names of the YAML, or
"problem" says why the server would refuse it (kind "unreadable": parse-tree
refuses the bytes; "rejected": the decode or the config's checks refuse it).
Exactly one of the two is not null. An empty file can be read: it is the
empty config, as in the server. "-" reads standard input.

--kind rbac is also checked against the declared identity claims, taken from
--identity-claim-headers or TA_IDENTITY_CLAIM_HEADERS as the server takes them.

Exit codes: 0 can be read, 1 refused, 2 usage or the input cannot be opened.`

// configCheckSchema names the JSON document `tenant-api config check` prints.
const configCheckSchema = "tenant-api.config-check/v1"

type configCheckDoc struct {
	Schema  string   `json:"schema"`
	Kind    string   `json:"kind"`
	Config  any      `json:"config"`
	Problem *problem `json:"problem"`
}

// configSubcommand reports whether argv asks for `tenant-api config ...`.
func configSubcommand(argv []string) bool { return len(argv) > 1 && argv[1] == "config" }

// runConfigCmd runs `tenant-api config check ...` and returns the exit code.
func runConfigCmd(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "check" {
		fmt.Fprintln(stderr, configCmdUsage)
		return 2
	}
	fs := flag.NewFlagSet("tenant-api config check", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { fmt.Fprintln(stderr, configCmdUsage) }
	kind := fs.String("kind", "", "What the file is: rbac or tenant-orgs")
	claimHeaders := registerIdentityClaimFlag(fs)
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	if fs.NArg() != 1 || (*kind != "rbac" && *kind != "tenant-orgs") {
		fmt.Fprintln(stderr, configCmdUsage)
		return 2
	}
	data, err := readConfigInput(fs.Arg(0), stdin)
	if err != nil {
		fmt.Fprintf(stderr, "config check: %v\n", err)
		return 2
	}

	doc := configCheckDoc{Schema: configCheckSchema, Kind: *kind}
	switch *kind {
	case "rbac":
		claims, cerr := rbac.ParseClaimHeaders(*claimHeaders)
		if cerr != nil {
			// The declaration, not the file, is wrong: the server would not
			// start with it either.
			fmt.Fprintf(stderr, "config check: --identity-claim-headers: %v\n", cerr)
			return 2
		}
		c, perr := rbac.ParseCandidateConfig(data, claims)
		doc.Config, err = c, perr
	case "tenant-orgs":
		c, perr := tenantorg.ParseConfig(data)
		doc.Config, err = c, perr
	}
	code := 0
	if err != nil {
		doc.Config, doc.Problem, code = nil, problemOf(err), 1
	}
	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	if werr := enc.Encode(doc); werr != nil {
		fmt.Fprintf(stderr, "config check: write output: %v\n", werr)
		return 2
	}
	return code
}

// readConfigInput reads path, or standard input for "-".
func readConfigInput(path string, stdin io.Reader) ([]byte, error) {
	if path == "-" {
		b, err := io.ReadAll(stdin)
		if err != nil {
			return nil, fmt.Errorf("read stdin: %w", err)
		}
		return b, nil
	}
	fi, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if fi.IsDir() {
		return nil, fmt.Errorf("%s is a directory", path)
	}
	return os.ReadFile(path)
}
