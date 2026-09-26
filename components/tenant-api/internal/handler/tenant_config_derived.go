package handler

// Config-derived operational state for the tenant list (#1988 D1 PR-3).
//
// ⛔ ONE COMPUTATION, NOT A SECOND ONE. Whether a tenant is silenced or in
// maintenance is not a field of its file: profiles, the root `_defaults.yaml`
// `tenants:` block, subtree `_defaults.yaml`, `expires` and the platform's
// `maintenance` state filter all take part. The list used to read the raw
// per-file values (TenantSummary.SilentMode / Maintenance), and a re-derivation
// here would drift from the exporter the same way. So the config comes from
// config.LoadDir — the exporter's own cold load — and the reading from
// OperationalStatesAt(now).ByTenant, the calls the collector's /metrics is
// built from (pinned in the exporter module by
// TestLoadDirOperationalStatesMatchTheScrape).
//
// ⚠️ WHAT IT ANSWERS: "what threshold-exporter would emit for the conf.d this
// tenant-api holds", evaluated at request time — 「依設定推算」. It is not
// observed from Alertmanager or from the live exporter: a ConfigMap assembled
// flat drops subdirectory tenants, and a rollout lags the git tree.

import (
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/rbac"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// ConfigDerivedState is one tenant's silent-mode / maintenance state derived
// from config (「依設定推算」), not observed from Alertmanager.
type ConfigDerivedState struct {
	// Severities whose notifications the exporter's user_silent_mode would silence ("critical" / "warning"), sorted; [] when none.
	SilentTargets []string `json:"silent_targets"`
	// Whether the exporter would emit user_state_filter{filter="maintenance"} for this tenant.
	MaintenanceActive bool `json:"maintenance_active"`
}

// ConfigDerivedMeta describes how the response's config_derived states were
// obtained, and what the load could not read.
type ConfigDerivedMeta struct {
	// Request time the states were evaluated at (silences and maintenance windows with `expires` are judged against it).
	EvaluatedAt time.Time `json:"evaluated_at"`
	// When conf.d was last loaded into the cache the states were evaluated from.
	ConfigLoadedAt time.Time `json:"config_loaded_at"`
	// conf.d files threshold-exporter skips because they fail to parse — a broken file, not a missing tenant. Unrestricted callers (open mode, or an admin restricted on no org, environment or domain) get root-relative paths; any other caller gets only the file name, and only for a file named after a tenant it could see as a degraded list row.
	ParseFailedFiles []string `json:"parse_failed_files"`
	// Parse-failed files not named in parse_failed_files because the caller's RBAC scope does not cover them.
	ParseFailedHidden int `json:"parse_failed_hidden,omitempty"`
	// Set when conf.d could not be loaded at all; no tenant then has config_derived. The loader's own message (paths relative to conf.d) only for unrestricted callers; any other caller gets a fixed message, since the loader's message names other tenants.
	LoadError string `json:"load_error,omitempty"`
}

// configDerivation is one config.LoadDir of the config dir, held by the
// snapshot cache. The config is immutable once loaded; the reading is taken
// per request (statesAt), because `expires` moves with the clock, not with
// the files.
type configDerivation struct {
	config      *cfg.ThresholdConfig // nil when the load failed
	parseFailed []string             // scan keys, sorted; nil when none
	loadErr     string
}

func loadConfigDerivation(configDir string) configDerivation {
	// nil logger: LoadDir's WARN/ERROR lines are the exporter's to print;
	// here the parse failures are returned and surfaced in the response.
	c, parseFailed, err := cfg.LoadDir(configDir, nil)
	if err != nil {
		return configDerivation{loadErr: relativeToConfDir(err.Error(), configDir)}
	}
	return configDerivation{config: c, parseFailed: parseFailed}
}

// scopedLoadError is what a caller without unrestricted read sees when conf.d
// could not be loaded. ⛔ CONTENT-FREE ON PURPOSE: the loader's message names
// files and tenant ids (a duplicate-tenant rejection names both declaring
// files, i.e. another org's tenant), which scoped RBAC exists to withhold.
const scopedLoadError = "config could not be loaded; ask a platform admin"

// relativeToConfDir strips the server's absolute conf.d path out of a loader
// message, leaving paths relative to conf.d. The walker reports the RESOLVED
// root (symlinks followed), so every spelling of the root is stripped.
//
// ⛔ ONLY WHOLE PATH COMPONENTS. A root is removed where it is followed by a
// separator ("<root>/a.yaml" → "a.yaml") or stands alone ("<root>" → "."),
// never as a bare prefix: with conf.d `…/conf` symlinked to `…/conf-real`,
// replacing the substring `…/conf` turned `…/conf-real/a.yaml` into
// `.-real/a.yaml`. Longest root first, so no root is cut by a shorter one.
func relativeToConfDir(msg, configDir string) string {
	seen := map[string]bool{}
	var roots []string
	add := func(r string) {
		if r != "" && r != "." && r != string(filepath.Separator) && !seen[r] {
			seen[r] = true
			roots = append(roots, r)
		}
	}
	add(filepath.Clean(configDir))
	if abs, err := filepath.Abs(configDir); err == nil {
		add(abs)
		if resolved, err := filepath.EvalSymlinks(abs); err == nil {
			add(resolved)
		}
	}
	return stripRoots(msg, roots)
}

// stripRoots is relativeToConfDir's rewrite over an explicit root set.
//
// A root is rewritten only where it STARTS a path token — at the start of the
// message, or after whitespace, a quote, `(` or `:` — and only as whole path
// components: "<root>/x" becomes "x", "<root>" standing alone becomes ".".
// Anywhere else it is part of some other path (`/mnt/srv/conf/a.yaml` for
// root `/srv/conf`, or `/conf.d/team/conf.d/a.yaml` for root `conf.d`), and
// cutting it out of the middle produced `/mnta.yaml` / `teama.yaml`. A
// relative conf.d ("conf") can also start a token inside its absolute
// spelling's text, so the roots are applied longest first.
func stripRoots(msg string, roots []string) string {
	roots = append([]string(nil), roots...)
	sort.Slice(roots, func(i, j int) bool { return len(roots[i]) > len(roots[j]) })
	for _, root := range roots {
		msg = replaceRootTokens(msg, root)
	}
	return msg
}

func replaceRootTokens(msg, root string) string {
	sep := string(filepath.Separator)
	var b strings.Builder
	for {
		i := strings.Index(msg, root)
		if i < 0 {
			b.WriteString(msg)
			return b.String()
		}
		end := i + len(root)
		startsToken := i == 0 || isTokenStart(msg[i-1])
		switch {
		case !startsToken:
			b.WriteString(msg[:end])
		case strings.HasPrefix(msg[end:], sep):
			b.WriteString(msg[:i])
			end += len(sep)
		case end == len(msg) || !isPathByte(msg[end]):
			b.WriteString(msg[:i])
			b.WriteString(".")
		default:
			b.WriteString(msg[:end]) // a longer name that merely starts with root
		}
		msg = msg[end:]
	}
}

// isTokenStart reports whether a path may begin right after byte c.
func isTokenStart(c byte) bool {
	switch c {
	case ' ', '\t', '\n', '"', '\'', '`', '(', ':':
		return true
	}
	return false
}

func isPathByte(c byte) bool {
	return c == '/' || c == '\\' || c == '.' || c == '-' || c == '_' ||
		('0' <= c && c <= '9') || ('a' <= c && c <= 'z') || ('A' <= c && c <= 'Z') || c >= 0x80
}

// statesAt is the per-tenant reading at now; nil when the load failed.
func (d *configDerivation) statesAt(now time.Time) map[string]cfg.TenantOperationalState {
	if d.config == nil {
		return nil
	}
	return d.config.OperationalStatesAt(now).ByTenant(d.config)
}

// withConfigDerived returns a copy of in — the snapshot's summaries are
// shared across requests and never written — with each tenant's derived state
// attached. A tenant the load does not know (conf.d did not load, or the file
// declares the tenant under an id other than its file name) gets none.
func (s *tenantSnapshot) withConfigDerived(in []TenantSummary, now time.Time) []TenantSummary {
	states := s.derived.statesAt(now)
	out := make([]TenantSummary, len(in))
	for i, t := range in {
		if s.derived.config == nil {
			// No derivation (load error, write in progress, tree off base):
			// the raw values must not stand in for the unknown state.
			t.SilentMode, t.Maintenance = "", ""
		}
		if st, ok := states[t.ID]; ok {
			t.ConfigDerived = &ConfigDerivedState{SilentTargets: st.SilentTargets, MaintenanceActive: st.MaintenanceActive}
		}
		out[i] = t
	}
	return out
}

// configDerivedMeta is the search response's account of the derivation.
//
// ⛔ EVERYTHING HERE CAN NAME ANOTHER TENANT. The loader's error and the
// parse-failed paths carry tenant ids, file names and directory names, and
// the directory layout of conf.d is per-team as often as not. So only an
// UNRESTRICTED caller — open mode, or an admin whose rule restricts no org,
// environment or domain (rbac.PlatformAdminUnrestricted; NOT
// PlatformAdminNonOrgScoped, which ignores the metadata axes) — gets them as
// the loader reported them (paths relative to conf.d). Anyone else gets:
//   - load_error: a fixed, content-free message;
//   - parse_failed_files: the FILE NAME only, and only for a file named after
//     a tenant the caller could see as a degraded list row — the same
//     decision filterTenantsByRBAC makes for a tenant whose file is unusable
//     (ScopeAllowedUnknownMetadata: a broken file's environment/domain are
//     unknown, not unlabeled). Everything else, platform `_` files included,
//     is only counted, so the caller still learns the derivation is
//     incomplete.
func (s *tenantSnapshot) configDerivedMeta(d *Deps, p *rbac.VerifiedPrincipal, now time.Time) ConfigDerivedMeta {
	meta := ConfigDerivedMeta{
		EvaluatedAt:      now,
		ConfigLoadedAt:   s.loadedAt,
		ParseFailedFiles: []string{},
	}
	unrestricted := len(d.RBAC.Get().Groups) == 0 || d.RBAC.PlatformAdminUnrestricted(p)
	if s.derived.loadErr != "" {
		meta.LoadError = scopedLoadError
		if unrestricted {
			meta.LoadError = s.derived.loadErr
		}
	}
	for _, key := range s.derived.parseFailed {
		if unrestricted {
			meta.ParseFailedFiles = append(meta.ParseFailedFiles, key)
			continue
		}
		base := path.Base(key)
		id, ok := confd.TenantIDFromFile(base)
		if ok {
			orgs, _ := d.TenantOrg.OrgsForTenant(id)
			ok = d.RBAC.ScopeAllowedUnknownMetadata(p, id, orgs)
		}
		if !ok {
			meta.ParseFailedHidden++
			continue
		}
		meta.ParseFailedFiles = append(meta.ParseFailedFiles, base)
	}
	return meta
}
