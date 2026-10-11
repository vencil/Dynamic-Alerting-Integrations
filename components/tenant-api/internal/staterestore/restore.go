// Package staterestore restores tenant-api's state store from a backup
// (#2884): `tenant-api state restore --from FILE [--keys k1,k2] [--dry-run]`.
//
// A backup is what an export already is — the state ConfigMap as
// `kubectl get configmap tenant-api-state -o yaml` (or -o json) prints it, or
// a copy of the single-host state directory. Taking one needs no tool;
// putting one back does, because the AccountID registry must never be
// overwritten by an older copy (account.MergeForRestore says why). So:
//
//   - the registry is always merged, never replaced, and the counter only
//     rises — a conflict between the sources refuses the whole restore
//     before anything is written;
//   - every other record is replaced whole, and only when its key is named
//     in --keys;
//   - --dry-run prints what would change and writes nothing.
package staterestore

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/statestore"
	"gopkg.in/yaml.v3"
)

// Backup maps record keys to their content.
type Backup map[string][]byte

// Load reads a backup from path: "-" for standard input, a directory (a copy
// of the single-host state directory), or a file holding the state
// ConfigMap as YAML or JSON.
func Load(path string, stdin io.Reader) (Backup, error) {
	if path == "-" {
		b, err := io.ReadAll(stdin)
		if err != nil {
			return nil, fmt.Errorf("read backup from stdin: %w", err)
		}
		return parseConfigMap(b, "stdin")
	}
	fi, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("read backup: %w", err)
	}
	if fi.IsDir() {
		return loadDir(path)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read backup: %w", err)
	}
	return parseConfigMap(b, path)
}

func loadDir(dir string) (Backup, error) {
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("read backup directory: %w", err)
	}
	bak := Backup{}
	for _, e := range ents {
		n := e.Name()
		if !e.Type().IsRegular() || strings.HasPrefix(n, ".") {
			continue // the store's own lock and temp files
		}
		b, err := os.ReadFile(filepath.Join(dir, n))
		if err != nil {
			return nil, fmt.Errorf("read backup directory: %w", err)
		}
		bak[n] = b
	}
	return bak, nil
}

// parseConfigMap reads a ConfigMap document (JSON is YAML) and returns its
// data. Anything else — a List, a different kind, no data map — is refused
// rather than read as an empty backup.
func parseConfigMap(b []byte, name string) (Backup, error) {
	var cm struct {
		Kind string            `yaml:"kind"`
		Data map[string]string `yaml:"data"`
	}
	if err := yaml.Unmarshal(b, &cm); err != nil {
		return nil, fmt.Errorf("%s is not a ConfigMap document: %w", name, err)
	}
	if cm.Kind != "ConfigMap" {
		return nil, fmt.Errorf("%s: kind is %q, want ConfigMap (kubectl get configmap tenant-api-state -o yaml)", name, cm.Kind)
	}
	bak := Backup{}
	for k, v := range cm.Data {
		bak[k] = []byte(v)
	}
	return bak, nil
}

// ValidKey reports whether key is a record key tenant-api keeps.
func ValidKey(key string) bool {
	switch key {
	case statestore.KeyGroups, statestore.KeyViews, statestore.KeyFederationPolicy, statestore.KeyAccountRegistry:
		return true
	}
	_, ok := statestore.SubsetTenant(key)
	return ok && !strings.ContainsAny(key, `/\`) && !strings.Contains(key, "..")
}

// Options are the restore's inputs besides the store and the backup.
type Options struct {
	// Keys are the records to replace whole from the backup.
	Keys []string
	// Tokens are the ids live logs tokens carry (token.LiveLogsAccounts).
	Tokens []account.TokenAllocation
	DryRun bool
	// Author is recorded in each write's audit event.
	Author string
}

// ErrUsage marks a restore refused for how it was asked (exit 2), as opposed
// to what it found (a conflict, an unreachable store: exit 1).
var ErrUsage = errors.New("usage")

const registryEntity = "account-registry"

// Run restores bak into s, writing a human-readable account to out. The
// registry is merged first: a conflict refuses the restore before any record
// is written.
func Run(ctx context.Context, s statestore.Store, bak Backup, o Options, out io.Writer) error {
	keys, err := checkKeys(bak, o.Keys)
	if err != nil {
		return err
	}
	mode := "restore"
	if o.DryRun {
		mode = "dry run"
	}
	fmt.Fprintf(out, "%s into %s\n", mode, s.Describe())

	// The registry. Judged once against the current record, so a conflict
	// is reported before any write; written through Mutate, which merges
	// again against whatever the record holds by then.
	cur, _, err := s.Get(ctx, statestore.KeyAccountRegistry)
	if err != nil {
		return err
	}
	_, rep, err := account.MergeForRestore(cur, bak[statestore.KeyAccountRegistry], o.Tokens)
	if err != nil {
		return err
	}
	reportRegistry(out, rep)
	if !o.DryRun && rep.Changed() {
		if _, err := s.Mutate(ctx, statestore.KeyAccountRegistry,
			statestore.Meta{Entity: registryEntity, Author: o.Author, Notes: []string{"state restore (merge)"}},
			func(cur []byte) ([]byte, error) {
				reg, _, err := account.MergeForRestore(cur, bak[statestore.KeyAccountRegistry], o.Tokens)
				if err != nil {
					return nil, err
				}
				return reg.Marshal()
			}); err != nil {
			return fmt.Errorf("write %s: %w", statestore.KeyAccountRegistry, err)
		}
	}

	for _, k := range keys {
		before, existed, err := s.Get(ctx, k)
		if err != nil {
			return err
		}
		after := bak[k]
		if existed && bytes.Equal(before, after) {
			fmt.Fprintf(out, "%s: unchanged\n", k)
			continue
		}
		fmt.Fprintf(out, "%s: replace\n", k)
		writeDiff(out, before, after)
		if o.DryRun {
			continue
		}
		if _, err := s.Mutate(ctx, k, statestore.Meta{Entity: entityOf(k), Author: o.Author, Notes: []string{"state restore (replace)"}},
			func([]byte) ([]byte, error) { return after, nil }); err != nil {
			return fmt.Errorf("write %s: %w", k, err)
		}
	}

	var skipped []string
	for k := range bak {
		if k != statestore.KeyAccountRegistry && !contains(keys, k) {
			skipped = append(skipped, k)
		}
	}
	sort.Strings(skipped)
	if len(skipped) > 0 {
		fmt.Fprintf(out, "not restored (not named in --keys): %s\n", strings.Join(skipped, ", "))
	}
	return nil
}

// checkKeys validates --keys against the backup and returns them sorted,
// without duplicates.
func checkKeys(bak Backup, keys []string) ([]string, error) {
	seen := map[string]bool{}
	var out []string
	for _, k := range keys {
		switch {
		case k == "":
			continue
		case k == statestore.KeyAccountRegistry:
			return nil, fmt.Errorf("%w: %s cannot be named in --keys: the registry is always merged, never replaced", ErrUsage, k)
		case !ValidKey(k):
			return nil, fmt.Errorf("%w: %q is not a tenant-api state key", ErrUsage, k)
		}
		if _, ok := bak[k]; !ok {
			return nil, fmt.Errorf("%w: %s is not in the backup", ErrUsage, k)
		}
		if !seen[k] {
			seen[k] = true
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out, nil
}

func reportRegistry(out io.Writer, rep account.RestoreReport) {
	if !rep.Changed() {
		fmt.Fprintf(out, "%s: unchanged (merge)\n", statestore.KeyAccountRegistry)
		return
	}
	fmt.Fprintf(out, "%s: merge — next_account_id %d → %d\n", statestore.KeyAccountRegistry, rep.NextBefore, rep.NextAfter)
	for _, a := range rep.Added {
		fmt.Fprintf(out, "  + %s\n", a)
	}
}

// entityOf names a record for the change notification, as the managers
// writing it do.
func entityOf(key string) string {
	switch key {
	case statestore.KeyGroups:
		return "groups"
	case statestore.KeyViews:
		return "views"
	case statestore.KeyFederationPolicy:
		return "federation-policy"
	}
	if t, ok := statestore.SubsetTenant(key); ok {
		return "federation/" + t
	}
	return key
}

func contains(ss []string, s string) bool {
	for _, x := range ss {
		if x == s {
			return true
		}
	}
	return false
}

// writeDiff prints a line diff of before → after ("-"/"+" lines; unchanged
// lines are left out). The records are small, so a plain LCS table is fine.
func writeDiff(out io.Writer, before, after []byte) {
	a, b := splitLines(before), splitLines(after)
	lcs := make([][]int, len(a)+1)
	for i := range lcs {
		lcs[i] = make([]int, len(b)+1)
	}
	for i := len(a) - 1; i >= 0; i-- {
		for j := len(b) - 1; j >= 0; j-- {
			if a[i] == b[j] {
				lcs[i][j] = lcs[i+1][j+1] + 1
			} else if lcs[i+1][j] >= lcs[i][j+1] {
				lcs[i][j] = lcs[i+1][j]
			} else {
				lcs[i][j] = lcs[i][j+1]
			}
		}
	}
	i, j := 0, 0
	for i < len(a) || j < len(b) {
		switch {
		case i < len(a) && j < len(b) && a[i] == b[j]:
			i, j = i+1, j+1
		case j < len(b) && (i == len(a) || lcs[i][j+1] >= lcs[i+1][j]):
			fmt.Fprintf(out, "  + %s\n", b[j])
			j++
		default:
			fmt.Fprintf(out, "  - %s\n", a[i])
			i++
		}
	}
}

func splitLines(b []byte) []string {
	s := strings.TrimRight(string(b), "\n")
	if s == "" {
		return nil
	}
	return strings.Split(s, "\n")
}
