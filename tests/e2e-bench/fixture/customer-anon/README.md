# Customer anonymized fixture (gitignored)

This directory holds customer-provided anonymized rule samples for
**B-1 Phase 2 calibration gate** measurements. The actual sample files
are **never** committed (`fixture/customer-anon/conf.d/` is in
`.gitignore`); this README is the only file checked in.

## How customer samples arrive

1. Customer Ops anonymizes their production `conf.d/` tree (replace
   tenant names with hashes, redact secrets in `_metadata` blocks).
2. Sample is delivered out-of-band (encrypted artifact, not via PR).
3. Maintainer extracts to `fixture/customer-anon/conf.d/`:

   ```bash
   tar -xzf customer-sample.tar.gz -C tests/e2e-bench/fixture/customer-anon/
   ```

4. Run e2e harness with `E2E_FIXTURE_KIND=customer-anon`.
5. Compare to most recent `synthetic-v2` baseline; if P95 is within
   ±30%, mark `gate_status: passed` (per design doc §6.5).

## Why not in version control

Even fully anonymized configs may leak structural fingerprints
(domain count, region naming pattern) that a customer considers
sensitive. Out-of-band delivery + local-only fixture is the contract.

## Cross-refs

- `docs/internal/design/phase-b-e2e-harness.md` §6 (customer sample protocol)
- `docs/internal/benchmark-playbook.md` §v2.8.0 Phase 2 e2e (ops cookbook)
