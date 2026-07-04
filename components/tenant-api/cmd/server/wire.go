package main

// PR-mode backend bootstrap (ADR-011).
//
// `direct` write-back is the boring default — no platform integration
// at all. The two PR-mode variants (GitHub PRs, GitLab MRs) each need
// to: validate required token / repo / project flags, build the
// platform Client, optionally point it at a self-hosted API endpoint,
// run a startup ValidateToken probe, and create the matching Tracker.
//
// In v2.7.0 this lived inline in main(); the resulting 50+ lines of
// switch-case made main() hard to read. PR-5 extracts it here so
// main() shows the wiring shape (flag-parse → managers → backend →
// router) without a paragraph of GitHub vs GitLab branching in the
// middle.

import (
	"fmt"
	"log"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/federation/token"
	gh "github.com/vencil/tenant-api/internal/github"
	gl "github.com/vencil/tenant-api/internal/gitlab"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

// prBackendFlags are the CLI-flag values consumed by wirePRBackend.
// Bundling them avoids passing 6 positional args to the helper and
// makes the contract obvious at the call site in main.go.
type prBackendFlags struct {
	Mode           string        // raw value of --write-mode / TA_WRITE_MODE
	GitHubRepo     string        // owner/repo
	GitHubBase     string        // PR target branch
	GitLabProject  string        // group/project or numeric ID
	GitLabBranch   string        // MR target branch
	ReloadInterval time.Duration // tracker poll cadence
}

// wirePRBackend resolves the PR-mode variant, builds the
// corresponding platform.Client + platform.Tracker, and returns the
// normalized WriteMode. Direct mode returns (nil, nil, WriteModeDirect).
//
// On missing required env vars / flags the helper calls log.Fatalf —
// matching pre-PR-5 behavior. Token-validation failures are logged at
// WARN (the deployment may have a deferred secret rotation; PR ops
// will surface the auth failure when actually invoked).
func wirePRBackend(f prBackendFlags) (platform.Client, platform.Tracker, handler.WriteMode) {
	wm := handler.WriteMode(f.Mode)
	switch wm {
	case handler.WriteModePR, handler.WriteModePRGitHub:
		// Normalize "pr" alias → "pr-github" so downstream comparisons
		// don't have to handle both. This was the v2.6.0 behavior;
		// preserving it.
		wm = handler.WriteModePR
		ghToken := os.Getenv("TA_GITHUB_TOKEN")
		if ghToken == "" {
			log.Fatalf("FATAL: TA_GITHUB_TOKEN is required when write-mode=pr/pr-github")
		}
		if f.GitHubRepo == "" {
			log.Fatalf("FATAL: --github-repo (or TA_GITHUB_REPO) is required when write-mode=pr/pr-github")
		}
		ghClient, err := gh.NewClient(ghToken, f.GitHubRepo, f.GitHubBase)
		if err != nil {
			log.Fatalf("FATAL: github client: %v", err)
		}
		if gheURL := os.Getenv("TA_GITHUB_API_URL"); gheURL != "" {
			ghClient.SetBaseURL(gheURL)
		}
		if err := ghClient.ValidateToken(); err != nil {
			slog.Warn("github token validation failed", "error", err, "note", "PR operations may fail")
		}
		slog.Info("github PR write-back mode enabled", "repo", f.GitHubRepo, "base", f.GitHubBase)
		return ghClient, gh.NewTracker(ghClient, f.ReloadInterval), wm

	case handler.WriteModePRGitLab:
		glToken := os.Getenv("TA_GITLAB_TOKEN")
		if glToken == "" {
			log.Fatalf("FATAL: TA_GITLAB_TOKEN is required when write-mode=pr-gitlab")
		}
		if f.GitLabProject == "" {
			log.Fatalf("FATAL: --gitlab-project (or TA_GITLAB_PROJECT) is required when write-mode=pr-gitlab")
		}
		glClient, err := gl.NewClient(glToken, f.GitLabProject, f.GitLabBranch)
		if err != nil {
			log.Fatalf("FATAL: gitlab client: %v", err)
		}
		if glURL := os.Getenv("TA_GITLAB_API_URL"); glURL != "" {
			glClient.SetBaseURL(glURL)
		}
		if err := glClient.ValidateToken(); err != nil {
			slog.Warn("gitlab token validation failed", "error", err, "note", "MR operations may fail")
		}
		slog.Info("gitlab MR write-back mode enabled", "project", f.GitLabProject, "target", f.GitLabBranch)
		return glClient, gl.NewTracker(glClient, f.ReloadInterval), wm

	default:
		slog.Info("direct write mode (commit-on-write)")
		return nil, nil, handler.WriteModeDirect
	}
}

// federationFlags are the CLI-flag values consumed by wireFederation.
type federationFlags struct {
	KeyPath       string        // --federation-key (empty disables the endpoint)
	ConfigMapName string        // --federation-store (the store ConfigMap name)
	Namespace     string        // --federation-namespace (empty → pod's own namespace)
	TTL           time.Duration // --federation-token-ttl
}

// wireFederation builds the federation-token Manager backed by the
// ConfigMap RecordStore (ADR-020 Posture B). It also returns the
// resolved store namespace so the caller can log where the store
// actually lives (it may have been derived, not passed explicitly).
//
// An empty KeyPath disables the feature: it returns (nil, "", nil)
// *before* touching Kubernetes, so a deployment that does not use
// federation needs neither an in-cluster client nor any ConfigMap RBAC.
//
// The store ConfigMap must already exist — the Helm chart pre-creates
// it (sub-issue IV-2m) so tenant-api's RBAC can be get+update on one
// resourceName with no namespace-wide create. NewConfigMapStore fails
// loud on NotFound.
func wireFederation(f federationFlags) (*token.Manager, string, error) {
	if f.KeyPath == "" {
		return nil, "", nil
	}
	client, err := buildInClusterClientset()
	if err != nil {
		return nil, "", fmt.Errorf("federation: %w", err)
	}
	ns := f.Namespace
	if ns == "" {
		ns, err = inClusterNamespace()
		if err != nil {
			return nil, "", fmt.Errorf("federation: resolve namespace: %w", err)
		}
	}
	store, err := token.NewConfigMapStore(client, ns, f.ConfigMapName)
	if err != nil {
		return nil, "", err
	}
	mgr, err := token.NewManager(f.KeyPath, store, f.TTL)
	if err != nil {
		return nil, "", err
	}
	return mgr, ns, nil
}

// inClusterNamespace reads the pod's own namespace from the
// service-account projected volume — the standard path every
// in-cluster pod carries. An operator can bypass this with
// --federation-namespace (e.g. running the store ConfigMap in a
// dedicated monitoring namespace).
func inClusterNamespace() (string, error) {
	const saNamespacePath = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
	b, err := os.ReadFile(saNamespacePath)
	if err != nil {
		return "", fmt.Errorf("read %s (set --federation-namespace to override): %w", saNamespacePath, err)
	}
	ns := strings.TrimSpace(string(b))
	if ns == "" {
		return "", fmt.Errorf("%s is empty", saNamespacePath)
	}
	return ns, nil
}

// buildInClusterClientset builds a Kubernetes clientset from the in-cluster
// service-account config. Extracted from wireFederation (ADR-027 PR-1b-i) so
// the federation token store and the machine-identity auditor share one
// construction path — and one fail-loud contract: an in-cluster config that
// cannot be loaded is an error the caller turns fatal, never a silent skip.
func buildInClusterClientset() (kubernetes.Interface, error) {
	cfg, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("in-cluster k8s config: %w", err)
	}
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return nil, fmt.Errorf("build k8s client: %w", err)
	}
	return client, nil
}

// machineAuditorFlags are the CLI-flag values consumed by wireMachineAuditor.
type machineAuditorFlags struct {
	Enabled     bool     // --machine-identity-audit
	Audience    string   // --machine-identity-audience (bound into every TokenReview; G4)
	IssuerAllow []string // --machine-identity-issuer (cluster-issuer allowlist; empty = any)
}

// wireMachineAuditor builds the ADR-027 machine-identity audit side-channel.
//
// When disabled it returns (nil, nil) BEFORE touching Kubernetes — a
// deployment that does not opt in needs no in-cluster client and no
// tokenreviews RBAC, exactly like federation's KeyPath=="" short-circuit.
//
// When enabled it builds the in-cluster clientset and, on failure, returns an
// error the caller turns into log.Fatalf. This is the MED-7 contract: the
// audit path MUST fail loud, never fail open. "Init failed → silently skip
// verification" is the precise anti-pattern this avoids — a misconfigured
// deployment that cannot verify tokens must not boot as if verification were
// happening. (The dev-bypass poison pill is the complementary guard for the
// opposite mistake: running an auth bypass INSIDE a cluster.)
func wireMachineAuditor(f machineAuditorFlags) (rbac.MachineIdentityAuditor, error) {
	if !f.Enabled {
		return nil, nil
	}
	// G4 (ADR-027): the bound audience is the last line against accepting any
	// ServiceAccount token — an empty audience would let a pod's default-audience
	// token verify. Enforce non-empty HERE so the binary holds the line
	// independently of the Helm `required` gate (raw-manifest / bare-binary
	// deploys never run Helm; the Helm gate also does not catch whitespace).
	// Fail loud, never fall through to "no audience".
	audience := strings.TrimSpace(f.Audience)
	if audience == "" {
		return nil, fmt.Errorf("--machine-identity-audience must be non-empty when --machine-identity-audit is set (ADR-027 G4)")
	}
	client, err := buildInClusterClientset()
	if err != nil {
		// MED-7: fail loud. Do not degrade to "no audit".
		return nil, fmt.Errorf("machine-identity audit: %w", err)
	}
	recorder := handler.NewIdentityAuditRecorder()
	// Bind the TRIMMED audience so the G4 check and the bound value agree: a
	// value like " tenant-api " passes the non-empty check but, bound verbatim
	// into the TokenReview, would match no real token and silently make every
	// audit verify_failed. Trimming keeps validation and use identical.
	return rbac.NewKSAResolver(client, audience, f.IssuerAllow, recorder), nil
}
