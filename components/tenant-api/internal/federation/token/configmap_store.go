package token

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync/atomic"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/util/retry"
)

// storeSchemaVersion tags the JSON document held in the store
// ConfigMap. A binary that reads a document carrying a *newer* version
// refuses to write it back — otherwise a rolling update would let an
// old replica silently drop fields a newer replica added (ADR-020
// Posture B; Gemini round-5 review).
const storeSchemaVersion = "v1"

// ConfigMap data keys. store.json is tenant-api's source of truth;
// revoked.txt is a derived, gateway-friendly projection.
const (
	cmKeyStore   = "store.json"
	cmKeyRevoked = "revoked.txt"
)

// k8sCallTimeout bounds a single ConfigMap API call.
const k8sCallTimeout = 10 * time.Second

// revokedEntry is one revoked token: the public id plus the expiry of
// the JWT it revokes. ExpiresAt lets the set self-prune — once the JWT
// is past its exp it is invalid by signature-time check anyway.
type revokedEntry struct {
	TokenID   string    `json:"token_id"`
	ExpiresAt time.Time `json:"expires_at"`
}

// storeDoc is the JSON document in the ConfigMap's store.json key.
type storeDoc struct {
	SchemaVersion string         `json:"schema_version"`
	Records       []Record       `json:"records"`
	Revoked       []revokedEntry `json:"revoked"`
}

// newerSchema reports whether the document was written by a binary
// with a schema this one does not recognise. Such a document must not
// be written back.
func (d *storeDoc) newerSchema() bool {
	return d.SchemaVersion != "" && d.SchemaVersion != storeSchemaVersion
}

// configMapStore is a RecordStore backed by a single Kubernetes
// ConfigMap shared across tenant-api replicas (ADR-020 Posture B).
// tenant-api stays stateless — the ConfigMap is the only state.
//
// Layout — two data keys:
//   - store.json  : the full storeDoc (records + revoked set);
//     tenant-api's source of truth, read/written only by tenant-api.
//   - revoked.txt : derived — one token_id per line, non-expired
//     revocations only. The API gateway mounts *this key* as a
//     projected volume and checks every request against it.
//
// The ConfigMap MUST be pre-created by the Helm chart (sub-issue
// IV-2m): RBAC then needs only get+update on this one resourceName and
// never namespace-wide create.
type configMapStore struct {
	client    kubernetes.Interface
	namespace string
	name      string
	// logger, when nil, falls back to slog.Default(). A test injects a
	// buffer-backed logger to assert the revocation event without swapping the
	// global default (test-seam discipline, CLAUDE.md §測試注入 Seam).
	logger *slog.Logger
	// lastContractViolations makes the contract-violation warning
	// EDGE-triggered instead of level-triggered (#1235 / TRK-349).
	//
	// The condition it reports is persistent: a violating entry can only be
	// created by a write that did not come through revoke(), and can only be
	// cleared by a human editing the document. Logging it on every load
	// therefore repeats one unchanging value at the rate of API traffic —
	// and the worst of that is not the UI listing but mutate(), which calls
	// load() inside RetryOnConflict, so a single contended write can emit the
	// same warning several times. Contention peaks exactly when something
	// else is writing this ConfigMap, i.e. when the signal matters most.
	//
	// Edge-triggering keeps every transition — appearance, growth, and the
	// human all-clear — while dropping only the repetitions that carry no
	// information. Deliberately NOT rate-limiting: rate-limiting is for
	// high-frequency CHANGING events; here the value is constant, so a rate
	// limit would just repeat the same number on a different clock.
	//
	// The zero value is the correct start state: a fresh process that finds
	// violations reports them (0 -> n is an edge), and one that finds none
	// stays quiet. Per-process, so each replica reports its own first
	// observation — which is right, that IS a new observation.
	lastContractViolations atomic.Int64
}

// NewConfigMapStore returns a RecordStore backed by the named ConfigMap
// in namespace. It verifies the ConfigMap exists at construction — a
// NotFound is fatal: the Helm chart is responsible for pre-creating it,
// and tenant-api deliberately lacks the RBAC to create one.
func NewConfigMapStore(client kubernetes.Interface, namespace, name string) (RecordStore, error) {
	ctx, cancel := context.WithTimeout(context.Background(), k8sCallTimeout)
	defer cancel()
	if _, err := client.CoreV1().ConfigMaps(namespace).Get(ctx, name, metav1.GetOptions{}); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, fmt.Errorf("federation: store ConfigMap %s/%s not found — the Helm chart must pre-create it", namespace, name)
		}
		return nil, fmt.Errorf("federation: open ConfigMap store: %w", err)
	}
	return &configMapStore{client: client, namespace: namespace, name: name}, nil
}

// log returns the store's logger, defaulting to slog.Default() in production
// (where main.configureLogger has installed the JSON handler on stderr).
func (s *configMapStore) log() *slog.Logger {
	if s.logger != nil {
		return s.logger
	}
	return slog.Default()
}

// load fetches and parses the store ConfigMap. The raw *ConfigMap is
// returned so a caller can write it back with its resourceVersion.
func (s *configMapStore) load(ctx context.Context) (*corev1.ConfigMap, *storeDoc, error) {
	cm, err := s.client.CoreV1().ConfigMaps(s.namespace).Get(ctx, s.name, metav1.GetOptions{})
	if err != nil {
		return nil, nil, err
	}
	doc, err := parseStoreDoc(cm.Data[cmKeyStore])
	if err != nil {
		return nil, nil, err
	}
	// Surfaced, never acted on — see countInvalidRevokedIDs. Count only: the
	// entry itself is never logged (ADR-028 D3; the content is the sensitive
	// part). Edge-triggered — see lastContractViolations for why, including
	// why a rate limit would be the wrong instrument here.
	n := int64(countInvalidRevokedIDs(doc))
	if prev := s.lastContractViolations.Swap(n); prev != n {
		switch {
		case n > 0:
			s.log().Warn("federation: store holds revoked entries that violate the token-id contract",
				"event", "federation_revoked_entry_contract_violation",
				"count", n, "previous_count", prev)
		default:
			// The all-clear. Only an edge-triggered signal can report this;
			// a level-triggered one just stops, which is indistinguishable
			// from the process having died.
			s.log().Warn("federation: store no longer holds revoked entries that violate the token-id contract",
				"event", "federation_revoked_entry_contract_violation_cleared",
				"count", 0, "previous_count", prev)
		}
	}
	return cm, doc, nil
}

// countInvalidRevokedIDs reports how many entries in the loaded document
// carry a token_id that violates tokenIDPattern (#1235 / TRK-349).
//
// ⛔⛔ IT COUNTS. IT DOES NOT FILTER — and no caller may make it filter.
// revokedText() regenerates revoked.txt verbatim from doc.Revoked on EVERY
// write, so dropping a non-conforming entry here would remove that token
// from the revoked set the moment anything else touched the store: the
// load path would finish, on the attacker's behalf, exactly the un-revoke
// that ADR-028 exists to detect. A bad entry that stays in the document
// keeps being written out, keeps being visible to the reconciler, and is
// refused by the gateway's whole-file check — all three of which are
// safe. Removing it is the only unsafe option.
//
// The write-side guard (revoke) is what stops such an entry appearing in
// the first place; this is the read-side detector for one that predates
// the guard or was planted directly into the ConfigMap.
func countInvalidRevokedIDs(doc *storeDoc) int {
	n := 0
	for _, e := range doc.Revoked {
		if !validTokenID(e.TokenID) {
			n++
		}
	}
	return n
}

// parseStoreDoc decodes the store.json value. An empty value (a
// freshly Helm-created ConfigMap) yields an empty document.
func parseStoreDoc(raw string) (*storeDoc, error) {
	doc := &storeDoc{SchemaVersion: storeSchemaVersion}
	if strings.TrimSpace(raw) == "" {
		return doc, nil
	}
	if err := json.Unmarshal([]byte(raw), doc); err != nil {
		return nil, fmt.Errorf("parse %s: %w", cmKeyStore, err)
	}
	if doc.SchemaVersion == "" {
		doc.SchemaVersion = storeSchemaVersion
	}
	return doc, nil
}

// mutate applies apply to the store document under RetryOnConflict —
// the standard client-go optimistic-concurrency loop (exponential
// backoff, retries only on a ResourceVersion conflict). It prunes
// expired entries on every write and regenerates revoked.txt.
//
// apply runs against freshly-loaded state on every retry attempt and
// may return an error to abort the write (e.g. a per-tenant cap that is
// only known once the current records are in hand). A non-conflict
// error from apply propagates out unretried — so a check inside apply
// is an atomic compare-and-swap, not a TOCTOU.
func (s *configMapStore) mutate(apply func(*storeDoc) error) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		ctx, cancel := context.WithTimeout(context.Background(), k8sCallTimeout)
		defer cancel()

		cm, doc, err := s.load(ctx)
		if err != nil {
			return err
		}
		if doc.newerSchema() {
			return fmt.Errorf("federation: store schema %q is newer than this binary supports (%q); refusing to write",
				doc.SchemaVersion, storeSchemaVersion)
		}

		now := time.Now()
		pruneDoc(doc, now)
		if err := apply(doc); err != nil {
			return err
		}
		doc.SchemaVersion = storeSchemaVersion

		// Compact, not indented: store.json is machine-read/written
		// state, and a ConfigMap has a hard ~1MiB ceiling — indentation
		// would burn 20-30% of that budget for readability nobody needs.
		raw, err := json.Marshal(doc)
		if err != nil {
			return err
		}
		if cm.Data == nil {
			cm.Data = map[string]string{}
		}
		cm.Data[cmKeyStore] = string(raw)
		cm.Data[cmKeyRevoked] = revokedText(doc.Revoked, now)

		_, err = s.client.CoreV1().ConfigMaps(s.namespace).Update(ctx, cm, metav1.UpdateOptions{})
		return err
	})
}

// put inserts (or idempotently replaces) a Record. The per-tenant cap
// is enforced HERE, inside the RetryOnConflict closure, against the
// freshly-loaded document — never as a list()-then-put() in the caller,
// which across tenant-api replicas is a TOCTOU race that lets the cap
// be overrun. pruneDoc has already dropped expired records, so every
// doc.Records entry counted below is live.
func (s *configMapStore) put(r Record) error {
	return s.mutate(func(doc *storeDoc) error {
		live := 0
		for i := range doc.Records {
			if doc.Records[i].TokenID == r.TokenID {
				doc.Records[i] = r // same token id — idempotent replace
				return nil
			}
			if doc.Records[i].TenantID == r.TenantID {
				live++
			}
		}
		if live >= maxTokensPerTenant {
			return ErrTokenLimitReached
		}
		doc.Records = append(doc.Records, r)
		return nil
	})
}

func (s *configMapStore) get(tokenID string) (Record, bool, error) {
	ctx, cancel := context.WithTimeout(context.Background(), k8sCallTimeout)
	defer cancel()
	_, doc, err := s.load(ctx)
	if err != nil {
		return Record{}, false, err
	}
	for _, r := range doc.Records {
		if r.TokenID == tokenID {
			return r, true, nil
		}
	}
	return Record{}, false, nil
}

func (s *configMapStore) list(tenantID string, now time.Time) ([]Record, error) {
	ctx, cancel := context.WithTimeout(context.Background(), k8sCallTimeout)
	defer cancel()
	_, doc, err := s.load(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]Record, 0)
	for _, r := range doc.Records {
		if r.TenantID == tenantID && !r.expired(now) {
			out = append(out, r)
		}
	}
	sortRecordsByIssuedAt(out)
	return out, nil
}

// listAll returns every non-expired Record across all tenants, oldest
// first. Used by the OrphanDetector (#521).
func (s *configMapStore) listAll(now time.Time) ([]Record, error) {
	ctx, cancel := context.WithTimeout(context.Background(), k8sCallTimeout)
	defer cancel()
	_, doc, err := s.load(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]Record, 0)
	for _, r := range doc.Records {
		if !r.expired(now) {
			out = append(out, r)
		}
	}
	sortRecordsByIssuedAt(out)
	return out, nil
}

// revoke removes the bookkeeping Record and adds the token to the
// revoked set. It records the revocation even when the Record is
// already gone (it may have been pruned while the JWT is still live).
//
// On a newly-added revocation it emits a structured `federation_token_revoked`
// event to the log (ADR-028 D1): an off-store, append-only tamper-evidence
// anchor that the revocation reconciler later checks against the live set to
// detect an un-revoke. The event is emitted AFTER mutate() commits — never
// inside the RetryOnConflict closure, which may run several times — and only
// when the token was not already revoked (an idempotent re-revoke emits nothing).
func (s *configMapStore) revoke(tokenID string, expiresAt time.Time) (bool, error) {
	// Write-side charset guard (#1235 / TRK-349). revoked.txt is consumed
	// by two independent readers whose agreement depends on every line
	// being unambiguous, so a non-conforming id must never enter the store
	// through the legitimate path. This is a hard error, not a
	// normalisation: silently rewriting the id is what produced two
	// disagreeing views in the first place.
	// The offending id is NOT echoed into the error (it can reach a log).
	if !validTokenID(tokenID) {
		return false, ErrInvalidTokenID
	}
	found := false
	newlyRevoked := false
	err := s.mutate(func(doc *storeDoc) error {
		newlyRevoked = false // reset per attempt — RetryOnConflict may re-run this closure
		kept := doc.Records[:0]
		for _, r := range doc.Records {
			if r.TokenID == tokenID {
				found = true
				continue
			}
			kept = append(kept, r)
		}
		doc.Records = kept

		for _, e := range doc.Revoked {
			if e.TokenID == tokenID {
				return nil // already revoked
			}
		}
		doc.Revoked = append(doc.Revoked, revokedEntry{TokenID: tokenID, ExpiresAt: expiresAt})
		newlyRevoked = true
		return nil
	})
	if err != nil {
		return false, err
	}
	if newlyRevoked {
		// Emitted AFTER the ConfigMap commit — a deliberate ordering with a
		// documented, accepted dual-write gap: if the pod hard-dies (OOMKill /
		// node crash) in the nanoseconds between the commit and this call, the
		// revocation persists but its event is lost, leaving that one token
		// without a tamper-evidence anchor (a later targeted un-revoke of it
		// would go undetected). An Outbox pattern would close the gap but is
		// absurd over-engineering for a 4h-TTL revocation, so the risk (crash in
		// the gap AND a precisely-targeted un-revoke) is accepted (ADR-028).
		//
		// ADR-028 D3 (PII minimization): opaque token_id + expires_at only —
		// NO tenant identifier, so the audit sink never becomes a store of
		// customer identifiers. The reconciler correlates on token_id and, if a
		// human needs the tenant at IR time, resolves it from the store.
		s.log().Info("federation token revoked",
			"event", "federation_token_revoked",
			"token_id", tokenID,
			"expires_at", expiresAt.UTC().Format(time.RFC3339))
	}
	return found, nil
}

// pruneDoc drops expired records and expired revoked entries. An
// expired revoked entry is safe to drop — the JWT it named is already
// rejected by the verifier's exp check.
func pruneDoc(doc *storeDoc, now time.Time) {
	recs := doc.Records[:0]
	for _, r := range doc.Records {
		if !r.expired(now) {
			recs = append(recs, r)
		}
	}
	doc.Records = recs

	rev := doc.Revoked[:0]
	for _, e := range doc.Revoked {
		if now.Before(e.ExpiresAt) {
			rev = append(rev, e)
		}
	}
	doc.Revoked = rev
}

// revokedText renders the gateway-facing revoked.txt: one token_id per
// line, non-expired entries only. The format is deliberately neutral —
// a plain id list any consumer can parse — not tailored to one gateway
// (e.g. Nginx `map` `<id> 1;` syntax). The gateway-specific encoding is
// settled when the gateway is built (sub-issue IV-2b), not guessed here.
//
// VERBATIM regeneration is load-bearing (#1235): every non-expired entry in
// the document is written out unchanged on every write. That is why the
// load path counts contract-violating entries instead of removing them —
// removing one here would silently delete it from the revoked set. See
// countInvalidRevokedIDs.
func revokedText(revoked []revokedEntry, now time.Time) string {
	var b strings.Builder
	for _, e := range revoked {
		if now.Before(e.ExpiresAt) {
			b.WriteString(e.TokenID)
			b.WriteByte('\n')
		}
	}
	return b.String()
}
