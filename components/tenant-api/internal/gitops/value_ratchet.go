package gitops

// #3227 (ADR-038 decision 12): the write gate judges a reserved key's value
// with the exporter's own reader, and a value the file already holds does not
// block a write that leaves it as the reader reads it — the value is told to
// the author as a notice, so a tenant whose file predates the check can still
// change anything else.

import (
	"github.com/vencil/tenant-api/internal/confd"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// ratchetValues is validateReplacing's value ratchet: kv judged against the
// file the write replaces (baseRaw, baseErr: readTenantFile's result).
//
// ⛔ STRICT WHENEVER THE BASE IS NOT KNOWN. A missing file (ENOENT — a new
// tenant), a read error, a file parse-tree refuses (confd.Judge) or one that
// does not decode all mean "nothing was there that this write leaves as it
// is", and every value finding stays an error. The opposite reading ("cannot
// tell, so let it through") would let a write introduce a value the exporter
// ignores by pairing it with a broken base. Same direction as eolGuardErrs.
//
// Every tenant of kv is ratcheted, not only the one the write is for: a flat
// file declares several, the body carries all of them, and another tenant's
// section the write does not touch is exactly what the ratchet is for.
func ratchetValues(kv cfg.KeyValidationByTenant, baseRaw []byte, baseErr error) cfg.KeyValidationByTenant {
	if baseErr != nil || len(baseRaw) == 0 {
		return kv
	}
	if confd.Judge(baseRaw) != nil {
		return kv
	}
	var base cfg.ThresholdConfig
	if err := yaml.Unmarshal(baseRaw, &base); err != nil {
		return kv
	}
	return kv.Ratchet(cfg.PriorValues(base.Tenants))
}

// SelfRatchetValues is ratchetValues for a reader of the file as it is on
// disk (GET /api/v1/tenants/{id}): no write is being judged, so a reserved
// value the exporter ignores is reported among the notices — the next write
// that leaves it as it is will not be refused for it — not among the
// validation warnings, which say what blocks a write.
func SelfRatchetValues(kv cfg.KeyValidationByTenant) cfg.KeyValidationByTenant {
	return kv.SelfRatchet()
}
