# Rule-Pack Alerting-Quality Reference Validation (ADR-030)

Re-runnable **reference fixtures** for measuring how well Vibe's shipped rule-packs
detect manufactured faults — the [ADR-030](../../../docs/adr/030-decision-layer-migration-validation.md)
"manufacture, don't observe" catch-rate harness applied to Oracle, DB2, and Linux-on-K8s.

## ⚠️ What these are (and are NOT)

- **Vendor-doc REFERENCE libraries** — fault/benign waveform signatures authored from
  public vendor documentation + DBA/SRE domain knowledge. **Public, committable, reusable.**
- **NOT customer engagement waveforms.** Real customer fault libraries never enter this repo
  — they load from an external path and go through `waveform_score.py --redact` (air-gap
  self-service, [#1079](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1079)).
  This directory is the *public-knowledge* counterpart, distinct from `tests/dx/fixtures/waveform/`
  toy self-test seeds.
- **Blind-authored (anti-circularity).** Every value comes from real fault behaviour, never
  reverse-engineered from a rule threshold. `oracle-reference-n2` / `db2-reference-n2` are a
  **second independent author** (different model) — findings that reproduce across authors are
  robust. `negative-*` are all `must_detect:false` benign signatures (the precision probe).

## Files

| file | role |
|---|---|
| `oracle-reference.yaml`, `db2-reference.yaml` | fault libraries (author 1) |
| `oracle-reference-n2.yaml`, `db2-reference-n2.yaml` | fault libraries (author 2, independent) |
| `k8s-linux-reference.yaml` | Linux-on-K8s fault library (container/node) |
| `negative-oracle.yaml`, `negative-db2.yaml` | benign libraries (precision probe) |
| `candidate-{oracle,db2,k8s}.rules.yaml` | direct-predicate form of the shipped alerting logic (see header of each) |
| `tolerances.yaml` | ⚠️ **illustrative** detection-time ceilings — NOT customer-MTTA-derived |

## Re-run (regression baseline)

Needs a dev-container `vmsingle` (`:8428`) + `vmalert-tool`/`vmalert` (see the VM install
notes in the ADR-030 harness). Per library:

```sh
# validate → inject → score (--rules-delay-s 30 is required for for:-alert ALERTS visibility)
python3 scripts/tools/dx/waveform_compile.py --check <lib>.yaml
python3 scripts/tools/dx/inject_waveform.py --vm-url http://localhost:8428 \
    --vmalert /tmp/vm/vmalert-prod --rules candidate-<engine>.rules.yaml \
    --rules-delay-s 30 --seed 1 --out /tmp/<lib>-inject.json <lib>.yaml
python3 scripts/tools/dx/waveform_score.py /tmp/<lib>-inject.json --tolerances tolerances.yaml
```

## Result summary (first run, 2026-07-19)

| metric | value |
|---|---|
| Recall (Oracle+DB2, author 1) | 51/67 = **76.1%** |
| Precision (Oracle+DB2) | ≈ **71.8%** (20 benign cases false-fired) |
| F1 | ≈ **73.9%** |
| Recall — Linux-on-K8s | 23/35 = **65.7%** |
| Recall — author 2 (n=2) | Oracle 100% · DB2 57.9% |

⚠️ **Thresholds used are the rule-pack header's *documented example* values, not shipped
active defaults** (Oracle/DB2 `_defaults.yaml` carries none — see findings). Precision is
threshold-sensitive: every over-fire is a busy-but-benign pattern exceeding a low example
threshold.

## Findings → tracked issues

- [#1174](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1174) — Oracle/DB2 coverage gaps (hard-parse, lock-wait orphan)
- [#1175](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1175) — ⭐ Oracle/DB2 threshold alerts ship dormant (no `_defaults` values)
- [#1176](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1176) — documented thresholds over-fire on busy workloads + deadlock/scale calibration
- [#1177](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1177) — Linux-on-K8s coverage gaps (oomkill-restart, staleness, flapping)

Full report: [#948](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/948) (ADR-030 RFC SSOT).
