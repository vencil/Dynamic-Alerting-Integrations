package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/staterestore"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/views"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// stateCmdUsage is printed for `tenant-api state` without a known verb.
const stateCmdUsage = `usage: tenant-api state restore --from FILE|DIR|- [--keys k1,k2] [--account-id-margin N] [--dry-run]
       tenant-api state restore --no-backup [--dry-run]
       tenant-api state show [--from FILE|DIR|-]

state show prints every record of the state store as tenant-api reads it,
as one JSON document (schema tenant-api.state-show/v1, ADR-036 §12): each
record the store holds goes through the server's own parse function and is
printed decoded ("record") or with why it cannot be used ("problem": kind
"unreadable" or "rejected"). Without --from it reads the configured store
read-only (a missing state directory or ConfigMap is an error, not an empty
store); with --from it reads a backup, as restore does, and needs no store
settings. It never reads a running server's hot-reload snapshot.

  kubectl exec deploy/tenant-api -c tenant-api -- tenant-api state show > state.json
  docker run -i --rm <tenant-api image> state show --from - < backup.yaml > state.json

Exit codes of state show: 0 printed (records with a problem included), 1 the
store or backup cannot be read, 2 usage.

state restore:

Restores tenant-api's state store from a backup (#2884). A backup is the
state ConfigMap as "kubectl get configmap tenant-api-state -o yaml" (or
-o json) prints it, or a copy of the --state-dir directory; "-" reads the
ConfigMap document from standard input. On Kubernetes, run it in the
tenant-api pod so it uses the pod's ServiceAccount and settings:

  kubectl exec -i deploy/tenant-api -c tenant-api -- tenant-api state restore --from - --dry-run < backup.yaml

The AccountID registry (_account_registry.yaml) is always merged, never
replaced: allocations are the union of the current registry, the backup and
the ids live logs tokens carry, and next_account_id only rises. Sources that
disagree refuse the restore and nothing is written. Other records are
replaced whole, and only those named in --keys.

next_account_id also goes above the AccountID high-water mark and at least
--account-id-margin above the backup's next_account_id (#2928): ids handed
out after the backup may be recorded nowhere that survived. The default is
twice the number of tenants in --config-dir, at least 10000. Running the same
restore again with the same margin changes nothing.

--no-backup merges only the current registry, live logs tokens and the
high-water mark: for a registry that is blank while the mark remains (a
reinstall), which the server refuses to start against. It needs federation
on, where the mark and the tokens live, and takes no --keys or
--account-id-margin.

The store is chosen by the same settings as the server (--state-store,
TA_STATE_STORE, ...). When federation is on (TA_FEDERATION_KEY set), the
federation token store is read for live logs tokens.

Exit codes: 0 restored (or dry run done), 1 refused or failed, 2 usage.`

// runStateCmd runs `tenant-api state <verb>` (restore, show) and returns the
// exit code.
func runStateCmd(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) > 0 && args[0] == "show" {
		return runStateShow(args[1:], stdin, stdout, stderr)
	}
	if len(args) == 0 || args[0] != "restore" {
		fmt.Fprintln(stderr, stateCmdUsage)
		return 2
	}
	fs := flag.NewFlagSet("tenant-api state restore", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { fmt.Fprintln(stderr, stateCmdUsage) }
	from := fs.String("from", "", "Backup to restore: a ConfigMap YAML/JSON file, a state directory, or - for stdin")
	keys := fs.String("keys", "", "Comma-separated record keys to replace whole from the backup")
	dryRun := fs.Bool("dry-run", false, "Print what would change; write nothing")
	author := fs.String("author", "state-restore", "Name recorded in each write's audit event")
	noBackup := fs.Bool("no-backup", false, "Merge without a backup: the current registry, live logs tokens and the high-water mark")
	margin := fs.Int64("account-id-margin", -1, "Ids to add to the backup's next_account_id (default: twice the tenants in --config-dir, at least 10000)")
	sf := registerStateFlags(fs)
	ff := registerFederationStoreFlags(fs)
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	if !checkTAEnv(os.Environ(), stderr) {
		return 2
	}
	if (*from == "") == !*noBackup || fs.NArg() > 0 {
		fmt.Fprintln(stderr, stateCmdUsage)
		return 2
	}
	if *noBackup && *keys != "" {
		fmt.Fprintln(stderr, "state restore: --keys needs a backup to replace records from; --no-backup has none")
		return 2
	}
	if *noBackup && *margin >= 0 {
		fmt.Fprintln(stderr, "state restore: --account-id-margin is added to the backup's next_account_id; --no-backup has none")
		return 2
	}
	if *noBackup && ff.KeyPath == "" {
		// Without federation there is no high-water mark and no live tokens,
		// so the merge has nothing to add to the current registry.
		fmt.Fprintln(stderr, "state restore: --no-backup merges the AccountID high-water mark and live logs tokens, which exist only with federation on (TA_FEDERATION_KEY)")
		return 2
	}
	if *margin < -1 || *margin > int64(^uint32(0)) {
		fmt.Fprintf(stderr, "state restore: --account-id-margin %d is out of range\n", *margin)
		return 2
	}

	bak := staterestore.Backup{}
	if !*noBackup {
		var err error
		if bak, err = staterestore.Load(*from, stdin); err != nil {
			fmt.Fprintf(stderr, "state restore: %v\n", err)
			return 1
		}
	}
	lim := account.RestoreLimits{}
	switch {
	case *margin >= 0:
		lim.Margin = uint32(*margin)
	case len(bytes.TrimSpace(bak[statestore.KeyAccountRegistry])) == 0:
		// No backup registry: the margin would not be applied, so its
		// default is not worth reading conf.d for.
	default:
		tenants, err := account.ListTenantIDs(sf.ConfigDir)
		if err != nil {
			fmt.Fprintf(stderr, "state restore: count tenants in %s for the default --account-id-margin (or pass it): %v\n", sf.ConfigDir, err)
			return 1
		}
		lim.Margin = account.DefaultRestoreMargin(len(tenants))
	}
	s, err := wireStateStore(*sf, nil)
	if err != nil {
		fmt.Fprintf(stderr, "state restore: %v\n", err)
		return 1
	}
	tokens, mark, err := federationRestoreInputs(*ff)
	lim.HighWater = mark.Max
	if err != nil {
		fmt.Fprintf(stderr, "state restore: read live logs tokens and the AccountID high-water mark (refusing to merge the registry without them): %v\n", err)
		return 1
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	err = staterestore.Run(ctx, s, bak, staterestore.Options{
		Keys:   strings.Split(*keys, ","),
		Tokens: tokens,
		Limits: lim,
		DryRun: *dryRun,
		Author: *author,
	}, stdout)
	switch {
	case err == nil:
		return 0
	case errors.Is(err, staterestore.ErrUsage):
		fmt.Fprintf(stderr, "state restore: %v\n", err)
		return 2
	default:
		fmt.Fprintf(stderr, "state restore: %v\n", err)
		return 1
	}
}

// federationRestoreInputs reads, from the federation token store, the ids
// live logs tokens carry and the AccountID high-water mark (#2928) — or
// neither when federation is off.
func federationRestoreInputs(f federationFlags) ([]account.TokenAllocation, account.Mark, error) {
	if f.KeyPath == "" {
		return nil, account.Mark{}, nil
	}
	client, err := buildInClusterClientset()
	if err != nil {
		return nil, account.Mark{}, err
	}
	ns := f.Namespace
	if ns == "" {
		if ns, err = inClusterNamespace(); err != nil {
			return nil, account.Mark{}, err
		}
	}
	rs, err := token.NewConfigMapStore(client, ns, f.ConfigMapName)
	if err != nil {
		return nil, account.Mark{}, err
	}
	tokens, err := tokenAllocations(rs)
	if err != nil {
		return nil, account.Mark{}, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	mark, err := token.NewHighWater(client, ns, f.ConfigMapName).Get(ctx)
	if err != nil {
		return nil, account.Mark{}, err
	}
	return tokens, mark, nil
}

// tokenAllocations converts the live logs records of rs.
func tokenAllocations(rs token.RecordStore) ([]account.TokenAllocation, error) {
	recs, err := token.LiveLogsAccounts(rs, time.Now())
	if err != nil {
		return nil, err
	}
	return toTokenAllocations(recs), nil
}

// toTokenAllocations converts live logs token records to the registry's
// view of them.
func toTokenAllocations(recs []token.Record) []account.TokenAllocation {
	out := make([]account.TokenAllocation, len(recs))
	for i, r := range recs {
		out[i] = account.TokenAllocation{TenantID: r.TenantID, AccountID: r.AccountID, TokenID: r.TokenID}
	}
	return out
}

// stateSubcommand reports whether argv asks for `tenant-api state ...`.
func stateSubcommand(argv []string) bool { return len(argv) > 1 && argv[1] == "state" }

// stateShowSchema names the JSON document `tenant-api state show` prints.
const stateShowSchema = "tenant-api.state-show/v1"

// Problem kinds of a record or config that cannot be used (ADR-036 §12).
const (
	// problemUnreadable: parse-tree judged the bytes unreadable (confd.Judge
	// returned a *cfg.UnreadableFileError).
	problemUnreadable = "unreadable"
	// problemRejected: the decode or the record's own checks refused it.
	problemRejected = "rejected"
)

// problem says why a record or config cannot be used.
type problem struct {
	Kind    string `json:"kind"`
	Message string `json:"message"`
}

// problemOf classifies a parse function's error.
func problemOf(err error) *problem {
	if _, ok := cfg.UnreadableKindOf(err); ok {
		return &problem{Kind: problemUnreadable, Message: err.Error()}
	}
	return &problem{Kind: problemRejected, Message: err.Error()}
}

// shownRecord is one entry of state show's records: exactly one of Record
// and Problem is non-nil.
type shownRecord struct {
	Key     string   `json:"key"`
	Record  any      `json:"record"`
	Problem *problem `json:"problem"`
}

type stateShowDoc struct {
	Schema           string        `json:"schema"`
	TenantAPIVersion string        `json:"tenant_api_version"`
	Records          []shownRecord `json:"records"`
}

// parseStateRecord passes one record to the parse function the server reads
// it with. ok is false for a key that is not a state record.
func parseStateRecord(key string, data []byte) (rec any, ok bool, err error) {
	switch key {
	case statestore.KeyGroups:
		rec, err = groups.ParseConfig(data)
	case statestore.KeyViews:
		rec, err = views.ParseConfig(data)
	case statestore.KeyFederationPolicy:
		rec, err = fedpolicy.ParsePolicyConfig(data)
	case statestore.KeyAccountRegistry:
		rec, err = account.Parse(data)
	default:
		if !staterestore.ValidKey(key) {
			return nil, false, nil
		}
		rec, err = fedpolicy.ParseSubset(data)
	}
	return rec, true, err
}

// showRecords judges every state record of snap, sorted by key. Keys that
// are not state records are left out, as are records the snapshot lacks.
func showRecords(snap map[string][]byte) []shownRecord {
	keys := make([]string, 0, len(snap))
	for k := range snap {
		if staterestore.ValidKey(k) {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	out := make([]shownRecord, 0, len(keys))
	for _, k := range keys {
		rec, ok, err := parseStateRecord(k, snap[k])
		switch {
		case !ok:
			continue
		case err != nil:
			out = append(out, shownRecord{Key: k, Problem: problemOf(err)})
		default:
			out = append(out, shownRecord{Key: k, Record: rec})
		}
	}
	return out
}

// runStateShow runs `tenant-api state show [--from FILE|DIR|-]`.
func runStateShow(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("tenant-api state show", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { fmt.Fprintln(stderr, stateCmdUsage) }
	from := fs.String("from", "", "Backup to read instead of the store: a ConfigMap YAML/JSON file, a state directory, or - for stdin")
	sf := registerStateFlags(fs)
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if !checkTAEnv(os.Environ(), stderr) {
		return 2
	}
	if fs.NArg() > 0 {
		fmt.Fprintln(stderr, stateCmdUsage)
		return 2
	}
	var snap map[string][]byte
	if *from != "" {
		// A backup needs no store settings: none of sf is read.
		bak, err := staterestore.Load(*from, stdin)
		if err != nil {
			fmt.Fprintf(stderr, "state show: %v\n", err)
			return 1
		}
		snap = bak
	} else {
		s, err := readStateSnapshot(*sf)
		if err != nil {
			fmt.Fprintf(stderr, "state show: %v\n", err)
			return 1
		}
		snap = s
	}
	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(stateShowDoc{
		Schema:           stateShowSchema,
		TenantAPIVersion: tenantAPIVersion(),
		Records:          showRecords(snap),
	}); err != nil {
		fmt.Fprintf(stderr, "state show: write output: %v\n", err)
		return 1
	}
	return 0
}
