package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/vencil/tenant-api/internal/proxyconfig"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

// configCmdUsage is printed for `tenant-api config` misuse.
const configCmdUsage = `usage: tenant-api config check --kind rbac|tenant-orgs [--oauth2-proxy-config PATH] FILE|-
       tenant-api config check --kind oauth2-proxy FILE|-
       tenant-api config check --kind scope --config-dir DIR [--rbac PATH] [--tenant-orgs PATH] [--oauth2-proxy-config PATH]

--kind rbac|tenant-orgs judges an admin config file with the parse function
the server reads it with (ADR-036 §12) and prints one JSON document (schema
tenant-api.config-check/v1): "config" is the decoded config, with the field
names of the YAML, or "problem" says why the server would refuse it (kind
"unreadable": parse-tree refuses the bytes; "rejected": the decode or the
config's checks refuse it). Exactly one of the two is not null. An empty file
can be read: it is the empty config, as in the server. "-" reads standard
input.

--kind rbac is also checked against the claims the oauth2-proxy config file
injects (--oauth2-proxy-config or TA_OAUTH2_PROXY_CONFIG, as the server takes
it; none given = no claims). A config file the server would refuse is a usage
error (exit 2): the server would not start with it.

--kind oauth2-proxy judges an oauth2-proxy structured config file (ADR-027
§2) as the server reads it: "config" is {email_header, groups_header,
claim_headers} (claim name -> header), or "problem" says why the server
refuses the file.

Exit codes: 0 can be read, 1 refused, 2 usage or the input cannot be opened.

--kind scope (ADR-027 §7) reads the RBAC config (--rbac, default
DIR/_rbac.yaml), the tenant-to-organization mapping (--tenant-orgs, default
DIR/_tenant_orgs.yaml; when that default file does not exist no tenant has an
organization, as in the server) and the tenants in DIR as GET
/api/v1/tenants reads them, and prints one JSON document (schema
tenant-api.scope-check/v1) listing, for every scope axis, each rule that
restricts it x each tenant the rule applies to that it does not grant:
"findings" entries {axis, rule, rule_index, tenant, reason}, axis
"environment" (the rule lists environments), "domain" (likewise) or "org"
(the rule has an org-scope, the tenant has no organization); reason
"unlabeled" (the tenant has no label on the axis: add it) or
"metadata_unknown" (the tenant's environment/domain cannot be read, and the
server's tenant list shows such a tenant under no rule restricting either:
fix the tenant file, or the root platform files when they cannot be read). Every scope axis is enforced, so each finding is a tenant the rule
does not grant. "tenant_problems" lists, for information, every tenant whose
environment/domain cannot be read (reason: the config_error, or
metadata_incomplete); it does not set the exit code — such a tenant matters
here only where a restricting rule applies to it, and then it is a finding.
"config_problems" lists an RBAC config, organization mapping or tenant
listing the server would refuse. The result depends only on the files; the
lists are sorted. Run it with the image you are upgrading to.

Exit codes: 0 no finding and no config problem, 1 any finding or config
problem, 2 usage or an input cannot be opened.`

// configCheckSchema names the JSON document `tenant-api config check` prints.
const configCheckSchema = "tenant-api.config-check/v1"

// proxyIdentityDoc is the "config" of `config check --kind oauth2-proxy`.
type proxyIdentityDoc struct {
	EmailHeader  string            `json:"email_header"`
	GroupsHeader string            `json:"groups_header"`
	ClaimHeaders map[string]string `json:"claim_headers"`
}

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
	kind := fs.String("kind", "", "What to check: rbac, tenant-orgs, oauth2-proxy (one FILE) or scope (--config-dir)")
	proxyConfig := registerOAuth2ProxyConfigFlag(fs)
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
			ProxyConfig:    *proxyConfig,
		}, stdout, stderr)
	}
	scopeOnly := false
	fs.Visit(func(f *flag.Flag) {
		switch f.Name {
		case "config-dir", "rbac", "tenant-orgs":
			scopeOnly = true
		}
	})
	if scopeOnly || fs.NArg() != 1 || (*kind != "rbac" && *kind != "tenant-orgs" && *kind != "oauth2-proxy") {
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
		ident, cerr := loadProxyIdentity(*proxyConfig)
		if cerr != nil {
			// The oauth2-proxy config, not the file, is wrong: the server
			// would not start with it either.
			fmt.Fprintf(stderr, "config check: --oauth2-proxy-config: %v\n", cerr)
			return 2
		}
		c, perr := rbac.ParseCandidateConfig(data, ident.ClaimHeaders)
		doc.Config, err = c, perr
	case "oauth2-proxy":
		ident, perr := proxyconfig.Parse(data)
		if perr == nil {
			doc.Config = proxyIdentityDoc{EmailHeader: ident.EmailHeader, GroupsHeader: ident.GroupsHeader, ClaimHeaders: ident.ClaimHeaders}
		}
		err = perr
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
