package config

// retired_threshold_keys.go — threshold keys retired from the contract (#3829).
//
// A retired threshold key is one no rule pack reads any more, recorded in the
// threshold registry's `retired_keys:` section (rule-packs/threshold-registry.yaml)
// with the key that replaces it (or none), the issue that retired it and a
// hint. The table is GENERATED from that section into
// retired_threshold_keys_gen.go by `check_threshold_registry.py --regen`;
// `--ci` fails while it is stale. This file is the lookup over it.
//
// ⛔ WHY A TABLE AND NOT A READER CHECK. The exporter serves a row for any
// numeric key a `defaults:` declares, so a retired key the deployment's own
// root `_defaults.yaml` still writes is "read" by the exporter, and only the
// advisory `no_alert` row said nothing alerts on it (ADR-038 decision 10:
// the deployment's own rules may read such a key, which the platform cannot
// see). The registry is what knows the key was retired, so the shared key
// verdict refuses it from this table, in every layer, whatever reads it.
//
// Lookup contract: the key's head (the part before `{`), exactly or with a
// `_critical` suffix — the two shapes aliasHead maps for a deprecated alias,
// so closing an alias window (moving `mysql_cpu` here) also retires
// `mysql_cpu_critical`, which the alias canonicalized while it was open. A
// derived `<old>_critical` names `<replaced_by>_critical` as its replacement.
// An entry written for the `_critical` key itself wins over the derived one.
// ⛔ Never a prefix match: `<old>_x` is not retired.

import (
	"fmt"
	"strconv"
	"strings"
)

// retiredThresholdKey is one entry of the registry's `retired_keys:`.
type retiredThresholdKey struct {
	key        string
	replacedBy string // "" when nothing replaces it
	tracking   int
	hint       string
}

// retiredThresholdKeysFixture is empty in every shipped binary. A test build
// of da-guard sets it with `-ldflags -X` (tests/_go_build.py ldflags_x) to
// add entries the committed registry does not have, as
// `<key>:<replaced_by>:<tracking>[,…]` (replaced_by may be empty) — so the
// tools that judge through da-guard can be measured on a retired key while
// the registry has none. ⛔ A test seam, read once at init; the generated
// table is the contract.
var retiredThresholdKeysFixture string

func init() {
	retiredThresholdKeys = append(retiredThresholdKeys, parseRetiredFixture(retiredThresholdKeysFixture)...)
}

// parseRetiredFixture reads retiredThresholdKeysFixture's format; a malformed
// entry panics: only a test build sets it, and a fixture that silently adds
// nothing would make that test pass for the wrong reason.
func parseRetiredFixture(s string) []retiredThresholdKey {
	if s == "" {
		return nil
	}
	var out []retiredThresholdKey
	for _, e := range strings.Split(s, ",") {
		f := strings.Split(e, ":")
		if len(f) != 3 || f[0] == "" {
			panic(fmt.Sprintf("retiredThresholdKeysFixture: %q is not <key>:<replaced_by>:<tracking>", e))
		}
		n, err := strconv.Atoi(f[2])
		if err != nil || n < 1 {
			panic(fmt.Sprintf("retiredThresholdKeysFixture: %q: tracking is not an issue number", e))
		}
		out = append(out, retiredThresholdKey{key: f[0], replacedBy: f[1], tracking: n,
			hint: "test fixture entry"})
	}
	return out
}

// retiredThresholdIn is table's entry for key's head, if any: the head's
// own entry, else — for a `<base>_critical` head — base's entry carried to
// the critical tier (the file comment's lookup contract).
func retiredThresholdIn(table []retiredThresholdKey, key string) (retiredThresholdKey, bool) {
	head, _ := splitKeyHead(key)
	if r, ok := retiredExact(table, head); ok {
		return r, true
	}
	if base, found := strings.CutSuffix(head, criticalSuffix); found {
		if r, ok := retiredExact(table, base); ok {
			r.key = head
			if r.replacedBy != "" {
				r.replacedBy += criticalSuffix
			}
			return r, true
		}
	}
	return retiredThresholdKey{}, false
}

func retiredExact(table []retiredThresholdKey, head string) (retiredThresholdKey, bool) {
	for _, r := range table {
		if r.key == head {
			return r, true
		}
	}
	return retiredThresholdKey{}, false
}

// text is the hint printed for a retired threshold key: it names the key
// that replaces it (or says none does), the issue, and the registry's hint.
func (r retiredThresholdKey) text() string {
	if r.replacedBy != "" {
		return fmt.Sprintf("retired threshold key (#%d): write `%s` instead; %s", r.tracking, r.replacedBy, r.hint)
	}
	return fmt.Sprintf("retired threshold key (#%d) with no replacement: remove it; %s", r.tracking, r.hint)
}

// RetiredThresholdHint is the hint for key when its head is a retired
// threshold key (the registry's `retired_keys:`), else "". A `_` key is
// never one: retired reserved keys are RetiredHint's.
func RetiredThresholdHint(key string) string {
	if r, ok := retiredThresholdIn(retiredThresholdKeys, key); ok {
		return r.text()
	}
	return ""
}

// retiredThresholdKeyNames is every retired threshold key of table.
func retiredThresholdKeyNames(table []retiredThresholdKey) []string {
	out := make([]string, 0, len(table))
	for _, r := range table {
		out = append(out, r.key)
	}
	return out
}
