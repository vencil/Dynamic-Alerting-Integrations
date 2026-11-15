package config

// Key checks (ADR-038 §11): the two checks on a tenant's keys as written
// that the CI readers print and no longer judge themselves — a dimensional
// key's `version` label (ADR-024 OQ-6) and a key in a retired spelling
// (#1231). One record per finding (KeyCheck), from which both of its
// renderings come:
//
//   - Message: the whole sentence the exporter's config-load log and the
//     tenant-api write path carry in KeyValidation.Errors / Notices. It
//     quotes the key and the label as written.
//   - Reason: a constant sentence per kind, carrying nothing the author
//     wrote. da-guard hands it to the route generator, which names the key
//     itself from the record's structured fields (#3703: the generator's
//     output carries no key or label value as written — Python's
//     shown_key decides how a key is shown, not Go).

import (
	"fmt"
	"strings"
)

// KeyCheckKind is a KeyCheck's kind: one of KeyCheckKinds.
type KeyCheckKind string

// Key check kinds.
const (
	// KeyCheckVersionRegex: a regex matcher on the `version` label.
	KeyCheckVersionRegex KeyCheckKind = "version_label_regex"
	// KeyCheckVersionEmpty: an empty `version` label.
	KeyCheckVersionEmpty KeyCheckKind = "version_label_empty"
	// KeyCheckVersionDefault: the literal `version="default"`.
	KeyCheckVersionDefault KeyCheckKind = "version_label_default"
	// KeyCheckVersionCharset: a `version` label outside versionLabelPattern.
	KeyCheckVersionCharset KeyCheckKind = "version_label_charset"
	// KeyCheckRenamedShadowed: a retired spelling whose replacement the same
	// map also sets; the retired entry is ignored.
	KeyCheckRenamedShadowed KeyCheckKind = "renamed_shadowed"
	// KeyCheckRenamedCriticalDimensional: a retired `_critical` spelling with
	// labels; a `_critical` tier takes no dimensions, so neither spelling is
	// served (#3556).
	KeyCheckRenamedCriticalDimensional KeyCheckKind = "renamed_critical_dimensional"
	// KeyCheckRenamed: a retired spelling, still resolved during the
	// transition window.
	KeyCheckRenamed KeyCheckKind = "renamed"
)

// KeyCheckKinds is every KeyCheckKind, in declaration order.
func KeyCheckKinds() []KeyCheckKind {
	return []KeyCheckKind{
		KeyCheckVersionRegex, KeyCheckVersionEmpty, KeyCheckVersionDefault, KeyCheckVersionCharset,
		KeyCheckRenamedShadowed, KeyCheckRenamedCriticalDimensional, KeyCheckRenamed,
	}
}

// IsRenamed reports whether k is about a retired spelling (an advisory,
// KeyValidation.Notices) rather than a `version` label (KeyValidation.Errors).
func (k KeyCheckKind) IsRenamed() bool {
	switch k {
	case KeyCheckRenamedShadowed, KeyCheckRenamedCriticalDimensional, KeyCheckRenamed:
		return true
	}
	return false
}

// keyCheckReasons is each kind's Reason: compile-time constants, with no
// key, label value or tenant — a rule's own constants only (the charset
// pattern, the ADR and issue numbers). KeyCheck.Reason panics on a kind
// missing here; TestKeyCheckEveryKind holds the two lists together.
var keyCheckReasons = map[KeyCheckKind]string{
	KeyCheckVersionRegex: "a regex matcher on the `version` label: ADR-024 Phase 1 expects an exact " +
		`version="..." selector`,
	KeyCheckVersionEmpty: "an empty `version` label: ADR-024 OQ-6 forbids it — it collides with the " +
		"unversioned baseline",
	KeyCheckVersionDefault: "the literal `version` label \"default\" is reserved for the normalize-layer " +
		"fallback (ADR-024 OQ-6)",
	KeyCheckVersionCharset: "the `version` label violates " + versionLabelPattern +
		" (ADR-024 OQ-6; pilot-calibratable)",
	KeyCheckRenamedShadowed: "a retired spelling (#1231), ignored because its replacement is also " +
		"set: remove it",
	KeyCheckRenamedCriticalDimensional: "a retired spelling (#1231), but a `_critical` tier takes no " +
		"dimensions and neither spelling is served (#3556): write the labels on the base key with " +
		"the value \"N:critical\", declared for the base under the root `dimensions:`",
	KeyCheckRenamed: "a retired spelling (#1231): the old name still resolves during the 2-release " +
		"transition window; update it to the replacement",
}

// KeyCheck is one key check on one tenant key.
type KeyCheck struct {
	Kind   KeyCheckKind
	Tenant string
	// Key is the key as the tenant's config writes it.
	Key string
	// Head is Key's metric name: Key before its first `{` (splitKeyHead).
	Head string
	// ReplacementHead, for a renamed kind only, is the metric name of the
	// spelling that replaces Key (the alias table's canonical name); "" for
	// a `version` label kind.
	ReplacementHead string

	// judged is the key in the spelling the check judged it in — the
	// canonical one (canonicalKeyFor) — and label the `version` label's
	// value or regex pattern. Both are what the author wrote; only Message
	// carries them.
	judged string
	label  string
}

// newKeyCheck is a KeyCheck on key (as written), judged in its canonical
// spelling canon; label is the `version` label's value or pattern, "" for a
// renamed kind.
func newKeyCheck(kind KeyCheckKind, tenant, key, canon, label string) KeyCheck {
	head, _ := splitKeyHead(key)
	c := KeyCheck{Kind: kind, Tenant: tenant, Key: key, Head: head, judged: canon, label: label}
	if kind.IsRenamed() {
		c.ReplacementHead, _ = splitKeyHead(canon)
	}
	return c
}

// Reason is the kind's constant sentence (keyCheckReasons).
func (c KeyCheck) Reason() string {
	r, ok := keyCheckReasons[c.Kind]
	if !ok {
		panic(fmt.Sprintf("config: key check kind %q has no reason", c.Kind))
	}
	return r
}

// Message is the whole sentence KeyValidation.Errors (a `version` label
// kind) or Notices (a renamed kind) carries for c: the key and the label as
// written, quoted.
func (c KeyCheck) Message() string {
	switch c.Kind {
	case KeyCheckVersionRegex:
		return fmt.Sprintf(
			"WARN: tenant=%s: regex version matcher %q in key %q — ADR-024 "+
				"Phase 1 expects an exact version=\"...\" selector", c.Tenant, c.label, c.judged)
	case KeyCheckVersionEmpty:
		return fmt.Sprintf(
			"WARN: tenant=%s: empty version label in key %q (ADR-024 OQ-6 "+
				"forbids empty — it collides with the unversioned baseline)", c.Tenant, c.judged)
	case KeyCheckVersionDefault:
		return fmt.Sprintf(
			"WARN: tenant=%s: literal version=\"default\" in key %q is reserved "+
				"for the normalize-layer fallback (ADR-024 OQ-6)", c.Tenant, c.judged)
	case KeyCheckVersionCharset:
		return fmt.Sprintf(
			"WARN: tenant=%s: version %q in key %q violates %s "+
				"(ADR-024 OQ-6; pilot-calibratable)", c.Tenant, c.label, c.judged, versionLabelPattern)
	case KeyCheckRenamedShadowed:
		return fmt.Sprintf(
			"NOTICE: tenant=%s: deprecated key %q is ignored because its replacement %q is also set — remove %q (#1231 rename)",
			c.Tenant, c.Key, c.judged, c.Key)
	case KeyCheckRenamedCriticalDimensional:
		head, labels := splitKeyHead(c.judged)
		tier := strings.TrimSuffix(head, criticalSuffix)
		return fmt.Sprintf(
			"NOTICE: tenant=%s: key %q was renamed to %q (#1231), but a `_critical` tier takes no dimensions and neither spelling is served (#3556); write it as %s: \"N:critical\", its labels declared for %s under the root `dimensions:`",
			c.Tenant, c.Key, c.judged, tier+labels, tier)
	case KeyCheckRenamed:
		return fmt.Sprintf(
			"NOTICE: tenant=%s: key %q was renamed to %q (#1231) — the old name still resolves during the 2-release transition window; please update this override to %q",
			c.Tenant, c.Key, c.judged, c.judged)
	}
	panic(fmt.Sprintf("config: key check kind %q has no message", c.Kind))
}
