package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"

	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

// `tenant-api config check --kind scope` (ADR-027 §7, §9 item 7): the
// offline list of tenants the enforced scope axes stop granting because they
// carry no label, or because their metadata cannot be read. Every input is read with the function the server reads it
// with — the RBAC config with rbac.ParseCandidateConfig, the organization
// mapping with tenantorg.ParseConfig, the tenants with the loader GET
// /api/v1/tenants is served from (handler.LoadTenantScopeLabels) — and the
// judgement is rbac's (RBACConfig.UnlabeledScopeGaps), so this file only
// reads, assembles and prints.

// scopeCheckSchema names the JSON document `config check --kind scope` prints.
const scopeCheckSchema = "tenant-api.scope-check/v1"

// Sources a scopeConfigProblem names.
const (
	scopeSourceRBAC       = "rbac"
	scopeSourceTenantOrgs = "tenant_orgs"
	scopeSourceTenants    = "tenants"
)

// scopeTenantProblemMetadataIncomplete is the reason of a tenant whose file
// is usable but whose metadata is unknown (TenantSummary.metadata_incomplete).
const scopeTenantProblemMetadataIncomplete = "metadata_incomplete"

type scopeCheckArgs struct {
	ConfigDir      string
	RBACPath       string // "" = ConfigDir/_rbac.yaml
	TenantOrgsPath string // "" = ConfigDir/_tenant_orgs.yaml, absent = empty mapping
	ProxyConfig    string // the oauth2-proxy config file; "" = proxyconfig.Default()
}

// scopeConfigProblem is an input the server would refuse.
type scopeConfigProblem struct {
	Source  string `json:"source"`
	Kind    string `json:"kind"`
	Message string `json:"message"`
}

// scopeTenantProblem is a tenant whose environment/domain cannot be read, so
// its metadata axes are not judged.
type scopeTenantProblem struct {
	Tenant string `json:"tenant"`
	// Reason is the tenant's config_error (unreadable, not_regular_file,
	// malformed_yaml, invalid_config) or metadata_incomplete.
	Reason           string `json:"reason"`
	GitManagedReason string `json:"git_managed_reason,omitempty"`
}

type scopeCheckSummary struct {
	Tenants        int            `json:"tenants"`
	Rules          int            `json:"rules"`
	Findings       int            `json:"findings"`
	FindingsByAxis map[string]int `json:"findings_by_axis"`
	TenantProblems int            `json:"tenant_problems"`
	ConfigProblems int            `json:"config_problems"`
}

type scopeCheckDoc struct {
	Schema         string               `json:"schema"`
	Findings       []rbac.ScopeGap      `json:"findings"`
	TenantProblems []scopeTenantProblem `json:"tenant_problems"`
	ConfigProblems []scopeConfigProblem `json:"config_problems"`
	Summary        scopeCheckSummary    `json:"summary"`
}

// runScopeCheck runs `config check --kind scope` and returns the exit code:
// 0 nothing found, 1 a finding or a config problem, 2 an input cannot be
// opened. A tenant problem alone does not set it: such a tenant is a finding
// wherever a restricting rule applies to it (UnlabeledScopeGaps), and the
// server grants it as before under every other rule.
func runScopeCheck(a scopeCheckArgs, stdout, stderr io.Writer) int {
	fail := func(format string, args ...any) int {
		fmt.Fprintf(stderr, "config check: "+format+"\n", args...)
		return 2
	}
	if a.RBACPath == "-" || a.TenantOrgsPath == "-" {
		return fail("--kind scope reads files, not standard input")
	}
	ident, err := loadProxyIdentity(a.ProxyConfig)
	if err != nil {
		return fail("--oauth2-proxy-config: %v", err)
	}
	claims := ident.ClaimHeaders
	if _, err := os.ReadDir(a.ConfigDir); err != nil {
		return fail("--config-dir: %v", err)
	}

	rbacPath := a.RBACPath
	if rbacPath == "" {
		rbacPath = filepath.Join(a.ConfigDir, "_rbac.yaml")
	}
	rbacData, err := readConfigInput(rbacPath, nil)
	if err != nil {
		return fail("rbac: %v", err)
	}
	orgsData, err := readTenantOrgsInput(a.ConfigDir, a.TenantOrgsPath)
	if err != nil {
		return fail("tenant-orgs: %v", err)
	}

	doc := scopeCheckDoc{
		Schema:         scopeCheckSchema,
		Findings:       []rbac.ScopeGap{},
		TenantProblems: []scopeTenantProblem{},
		ConfigProblems: []scopeConfigProblem{},
	}
	refused := func(source string, err error) {
		p := problemOf(err)
		doc.ConfigProblems = append(doc.ConfigProblems, scopeConfigProblem{Source: source, Kind: p.Kind, Message: p.Message})
	}

	rbacCfg, err := rbac.ParseCandidateConfig(rbacData, claims)
	if err != nil {
		refused(scopeSourceRBAC, err)
	}
	orgs, err := tenantorg.ParseConfig(orgsData)
	if err != nil {
		refused(scopeSourceTenantOrgs, err)
	}
	rows, err := handler.LoadTenantScopeLabels(a.ConfigDir)
	if err != nil {
		// The listing the server refuses (two files for one tenant); a
		// directory that cannot be read was answered above.
		refused(scopeSourceTenants, err)
	}

	for _, r := range rows {
		if !r.MetadataUnknown() {
			continue
		}
		reason := r.ConfigError
		if reason == "" {
			reason = scopeTenantProblemMetadataIncomplete
		}
		doc.TenantProblems = append(doc.TenantProblems,
			scopeTenantProblem{Tenant: r.ID, Reason: reason, GitManagedReason: r.GitManagedReason})
	}
	sort.SliceStable(doc.TenantProblems, func(i, j int) bool {
		return doc.TenantProblems[i].Tenant < doc.TenantProblems[j].Tenant
	})

	// Findings are judged only when every input reads: an RBAC config the
	// server refuses has no rules to judge, an organization mapping it
	// refuses has no orgs, and a refused listing has no tenants.
	if len(doc.ConfigProblems) == 0 {
		labels := make([]rbac.TenantLabels, 0, len(rows))
		for _, r := range rows {
			labels = append(labels, rbac.TenantLabels{
				ID:              r.ID,
				Environment:     r.Environment,
				Domain:          r.Domain,
				MetadataUnknown: r.MetadataUnknown(),
				Orgs:            orgs.TenantOrgs[r.ID],
			})
		}
		if gaps := rbacCfg.UnlabeledScopeGaps(labels); gaps != nil {
			doc.Findings = gaps
		}
		doc.Summary.Rules = len(rbacCfg.Groups)
	}

	byAxis := map[string]int{
		string(rbac.ScopeAxisEnvironment): 0,
		string(rbac.ScopeAxisDomain):      0,
		string(rbac.ScopeAxisOrg):         0,
	}
	for _, g := range doc.Findings {
		byAxis[string(g.Axis)]++
	}
	doc.Summary.Tenants = len(rows)
	doc.Summary.Findings = len(doc.Findings)
	doc.Summary.FindingsByAxis = byAxis
	doc.Summary.TenantProblems = len(doc.TenantProblems)
	doc.Summary.ConfigProblems = len(doc.ConfigProblems)

	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(doc); err != nil {
		return fail("write output: %v", err)
	}
	if len(doc.Findings) > 0 || len(doc.ConfigProblems) > 0 {
		return 1
	}
	return 0
}

// readTenantOrgsInput reads the organization mapping: path when given (it
// must exist), else configDir/_tenant_orgs.yaml, whose absence is the empty
// mapping — what tenantorg.NewManager and tenantorg.OrgsInTree read a
// missing file as.
func readTenantOrgsInput(configDir, path string) ([]byte, error) {
	if path != "" {
		return readConfigInput(path, nil)
	}
	path = filepath.Join(configDir, "_tenant_orgs.yaml")
	if _, err := os.Lstat(path); errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	return readConfigInput(path, nil)
}
