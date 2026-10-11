package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/staterestore"
)

// stateCmdUsage is printed for `tenant-api state` without a known verb.
const stateCmdUsage = `usage: tenant-api state restore --from FILE|DIR|- [--keys k1,k2] [--dry-run]

Restores tenant-api's state store from a backup (#2884). A backup is the
state ConfigMap as "kubectl get configmap tenant-api-state -o yaml" (or
-o json) prints it, or a copy of the --state-dir directory; "-" reads the
ConfigMap document from standard input. On Kubernetes, run it in the
tenant-api pod so it uses the pod's ServiceAccount and settings:

  kubectl exec -i deploy/tenant-api -- tenant-api state restore --from - --dry-run < backup.yaml

The AccountID registry (_account_registry.yaml) is always merged, never
replaced: allocations are the union of the current registry, the backup and
the ids live logs tokens carry, and next_account_id only rises. Sources that
disagree refuse the restore and nothing is written. Other records are
replaced whole, and only those named in --keys.

The store is chosen by the same settings as the server (--state-store,
TA_STATE_STORE, ...). When federation is on (TA_FEDERATION_KEY set), the
federation token store is read for live logs tokens.

Exit codes: 0 restored (or dry run done), 1 refused or failed, 2 usage.`

// runStateCmd runs `tenant-api state <verb>` and returns the exit code.
func runStateCmd(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
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
	sf := registerStateFlags(fs)
	ff := registerFederationStoreFlags(fs)
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	if *from == "" || fs.NArg() > 0 {
		fmt.Fprintln(stderr, stateCmdUsage)
		return 2
	}

	bak, err := staterestore.Load(*from, stdin)
	if err != nil {
		fmt.Fprintf(stderr, "state restore: %v\n", err)
		return 1
	}
	s, err := wireStateStore(*sf, nil)
	if err != nil {
		fmt.Fprintf(stderr, "state restore: %v\n", err)
		return 1
	}
	tokens, err := liveLogsTokens(*ff)
	if err != nil {
		fmt.Fprintf(stderr, "state restore: read live logs tokens (refusing to merge the registry without them): %v\n", err)
		return 1
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	err = staterestore.Run(ctx, s, bak, staterestore.Options{
		Keys:   strings.Split(*keys, ","),
		Tokens: tokens,
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

// liveLogsTokens reads the ids live logs tokens carry from the federation
// token store, or none when federation is off.
func liveLogsTokens(f federationFlags) ([]account.TokenAllocation, error) {
	if f.KeyPath == "" {
		return nil, nil
	}
	client, err := buildInClusterClientset()
	if err != nil {
		return nil, err
	}
	ns := f.Namespace
	if ns == "" {
		if ns, err = inClusterNamespace(); err != nil {
			return nil, err
		}
	}
	rs, err := token.NewConfigMapStore(client, ns, f.ConfigMapName)
	if err != nil {
		return nil, err
	}
	return tokenAllocations(rs)
}

// tokenAllocations converts the live logs records of rs.
func tokenAllocations(rs token.RecordStore) ([]account.TokenAllocation, error) {
	recs, err := token.LiveLogsAccounts(rs, time.Now())
	if err != nil {
		return nil, err
	}
	out := make([]account.TokenAllocation, len(recs))
	for i, r := range recs {
		out[i] = account.TokenAllocation{TenantID: r.TenantID, AccountID: r.AccountID, TokenID: r.TokenID}
	}
	return out, nil
}

// stateSubcommand reports whether argv asks for `tenant-api state ...`.
func stateSubcommand(argv []string) bool { return len(argv) > 1 && argv[1] == "state" }
