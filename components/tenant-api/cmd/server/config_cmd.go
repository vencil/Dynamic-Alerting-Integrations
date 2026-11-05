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
       tenant-api config check --kind scope --config-dir DIR [--rbac PATH] [--tenant-orgs PATH] [--identity-claim-headers k=Header,...]

--kind rbac|tenant-orgs judges an admin config file with the parse function
the server reads it with (ADR-036 §12) and prints one JSON document (schema
tenant-api.config-check/v1): "config" is the decoded config, with the field
names of the YAML, or "problem" says why the server would refuse it (kind
"unreadable": parse-tree refuses the bytes; "rejected": the decode or the
config's checks refuse it). Exactly one of the two is not null. An empty file
can be read: it is the empty config, as in the server. "-" reads standard
input.

--kind rbac is also checked against the declared identity claims, taken from
--identity-claim-headers or TA_IDENTITY_CLAIM_HEADERS as the server takes them.

Exit codes: 0 can be read, 1 refused, 2 usage or the input cannot be opened.

--kind scope (ADR-027 §7) reads the RBAC config (--rbac, default
DIR/_rbac.yaml), the tenant-to-organization mapping (--tenant-orgs, default
DIR/_tenant_orgs.yaml; when that default file does not exist no tenant has an
organization, as in the server) and the tenants in DIR as GET
/api/v1/tenants reads them, and prints one JSON document (schema
tenant-api.scope-check/v1) listing, for every scope axis, each rule that
restricts it x each tenant the rule applies to that carries no label on it:
"findings" entries {axis, rule, rule_index, tenant}, axis "environment"
(the rule lists environments, the tenant has none), "domain" (likewise) or
"org" (the rule has an org-scope, the tenant has no organization). Every
scope axis is enforced, so each finding is a tenant the rule does not grant.
"tenant_problems" lists the tenants whose environment/domain cannot be read
(reason: the config_error, or metadata_incomplete); their metadata axes are
not judged. "config_problems" lists an RBAC config, organization mapping or
tenant listing the server would refuse. The result depends only on the files;
the lists are sorted. Run it with the image you are upgrading to.

Exit codes: 0 no finding and no problem, 1 any finding or problem, 2 usage or
an input cannot be opened.`

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
	kind := fs.String("kind", "", "What to check: rbac, tenant-orgs (one FILE) or scope (--config-dir)")
	claimHeaders := registerIdentityClaimFlag(fs)
	configDir := fs.String("config-dir", "", "--kind scope: the conf.d directory (tenant files)")
	rbacPath := fs.String("rbac", "", "--kind scope: the RBAC config (default DIR/_rbac.yaml)")
	tenantOrgsPath := fs.String("tenant-orgs", "", "--kind scope: the tenant-to-organization mapping (default DIR/_tenant_orgs.yaml)")
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	if !checkTAEnv(os.Environ(), stderr) {
		return 2
	}
	if *kind == "scope" {
		if fs.NArg() != 0 || *configDir == "" {
			fmt.Fprintln(stderr, configCmdUsage)
			return 2
		}
		return runScopeCheck(scopeCheckArgs{
			ConfigDir:      *configDir,
			RBACPath:       *rbacPath,
			TenantOrgsPath: *tenantOrgsPath,
			ClaimHeaders:   *claimHeaders,
		}, stdout, stderr)
	}
	scopeOnly := false
	fs.Visit(func(f *flag.Flag) {
		switch f.Name {
		case "config-dir", "rbac", "tenant-orgs":
			scopeOnly = true
		}
	})
	if scopeOnly || fs.NArg() != 1 || (*kind != "rbac" && *kind != "tenant-orgs") {
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
