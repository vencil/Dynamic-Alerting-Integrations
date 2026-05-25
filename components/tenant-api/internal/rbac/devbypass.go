package rbac

// Dev-auth-bypass: a LOCAL-DEV-ONLY substitute for the oauth2-proxy identity
// layer. See ADR-022 for the decision + the four-layer safety rationale.
//
// Layers:
//  1. Default OFF — the middleware is only mounted when --dev-bypass-auth is set.
//  2. Observability tripwire — every response carries `X-Dev-Auth-Bypass: active`,
//     /metrics exposes `tenant_api_dev_auth_bypass_active 1`, and startup logs a
//     loud WARN, so the mode is impossible to run unnoticed in ANY environment.
//  3. Runtime poison pill — DevBypassK8sGuard panics if the flag is set inside a
//     Kubernetes cluster (production), which SAST cannot see at deploy time.
//  4. SAST block — #448 IaC lint forbids TA_DEV_BYPASS_AUTH in helm/k8s manifests.
//
// Identity-only: this injects an identity when the upstream proxy did not; RBAC
// is still fully enforced against the injected group. It does NOT grant extra
// permissions — the injected group's access comes from _rbac.yaml as usual.

import (
	"net/http"
)

// DevBypassMiddleware injects a dev identity (email + groups) when no
// oauth2-proxy header is present, and marks every response with the
// `X-Dev-Auth-Bypass: active` tripwire header (Layer 2). A real forwarded
// identity is never overridden. Mounted only when --dev-bypass-auth is set.
func DevBypassMiddleware(devEmail, devGroups string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// Layer 2 tripwire: make the dangerous mode detectable on every
			// response (monitoring / proxies / curl -I), in any environment.
			w.Header().Set("X-Dev-Auth-Bypass", "active")

			// Inject identity ONLY when the upstream proxy did not. Never
			// override a real oauth2-proxy identity.
			if r.Header.Get("X-Forwarded-Email") == "" {
				r.Header.Set("X-Forwarded-Email", devEmail)
				r.Header.Set("X-Forwarded-Groups", devGroups)
			}
			next.ServeHTTP(w, r)
		})
	}
}

// InKubernetes reports whether the process is running inside a Kubernetes
// cluster. Signals: the kubelet-injected KUBERNETES_SERVICE_HOST env var
// (present in every normal pod) and the default service-account token mount.
// The funcs are injectable so the guard is unit-testable without a cluster.
func InKubernetes(getenv func(string) string, exists func(string) bool) bool {
	if getenv("KUBERNETES_SERVICE_HOST") != "" {
		return true
	}
	return exists("/var/run/secrets/kubernetes.io")
}

// DevBypassK8sGuard panics if --dev-bypass-auth is requested inside a
// Kubernetes cluster (Layer 3 runtime poison pill). Fail-closed: an auth
// bypass must NEVER run in a production cluster. SAST (Layer 4) blocks the
// env var from manifests at deploy time; this is the runtime backstop for
// manual or extreme misconfiguration that SAST cannot see.
func DevBypassK8sGuard(getenv func(string) string, exists func(string) bool) {
	if InKubernetes(getenv, exists) {
		panic("FATAL: --dev-bypass-auth is strictly forbidden inside Kubernetes clusters " +
			"(KUBERNETES_SERVICE_HOST or serviceaccount mount detected). This flag is local-dev-only.")
	}
}
