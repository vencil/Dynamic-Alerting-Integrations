---
name: Signing scheme request (security team)
about: Request activation of an additional release-artefact signing scheme (GPG / Authenticode / FIPS / etc.) beyond Layer 1 (cosign keyless + SBOM)
title: "[Signing] Request: <scheme name> for <use case>"
labels: ["security", "signing-request", "needs-triage"]
assignees: []
---

<!--
Thank you for raising a signing-scheme request. This template exists
because we deliberately ship Layer 1 (cosign keyless + SBOM in SPDX +
CycloneDX) by default and gate every other scheme on actual customer
need — see `docs/internal/release-signing-runbook.md` for the layered
design rationale.

Filling this template out lets the maintainer evaluate Layer 2 / 3
activation cost and either turn the scheme on or scope an alternative
that meets your actual compliance requirement.

Public Q: which scheme do you actually need verified, and why?
-->

## Required scheme

<!-- Tick the one your security team requires; multi-select OK if you
genuinely need multiple. -->

- [ ] **GPG** detached `.asc` signatures (Layer 2a)
- [ ] **Windows Authenticode** for native `.exe` binaries (Layer 2b)
- [ ] **Cosign + GPG dual-signing** (mixed customer fleet, Layer 2c)
- [ ] **HSM-backed signing keys** (FIPS 140-2 / Common Criteria) (Layer 3)
- [ ] **SLSA Level 2 / Level 3** build provenance attestation (Layer 3)
- [ ] **Reproducible builds** (Layer 3)
- [ ] **In-toto attestation chain** (Layer 3)
- [ ] **Other**: <!-- describe -->

## Compliance driver

<!-- The actual policy / framework / regulation that requires this. The
maintainer can't gauge effort without knowing whether you need a
checkbox-pass for an audit, a working production verification flow, or
a custom certification path. -->

- [ ] FedRAMP (Moderate / High)
- [ ] NIST 800-53 / NIST SSDF
- [ ] DORA (EU financial)
- [ ] PCI DSS
- [ ] HIPAA / 21 CFR Part 11
- [ ] EU Cyber Resilience Act
- [ ] Internal corporate policy (please describe)
- [ ] Other: <!-- describe -->

## Use case

<!-- What you'll do with the verified artefact. Helps separate
"customer just wants to verify cosign chain works in their CI"
(Layer 1, already shipped) from "customer needs HSM-backed keys for an
ATO package" (Layer 3, multi-week effort). -->

- Where does the verification happen? <!-- e.g. CI pipeline / pre-deploy gate / runtime / human review -->
- Network model: <!-- internet-connected / private-network / fully air-gapped -->
- Frequency: <!-- one-time onboarding audit / per-release verification / continuous -->
- Stakeholders: <!-- e.g. Security team / Compliance officer / Customer-of-customer -->

## Existing Layer 1 — what's already shipped

The following are active on every `tools/v*` release from v2.8.0
onward. Confirm before requesting more if your real ask is "Layer 1
isn't enough":

- [x] Cosign keyless signature on every binary archive (linux/darwin/windows × amd64/arm64) — `.sig` + `.cert`
- [x] Cosign keyless signature on Docker image (signed by digest, not tag)
- [x] Cosign keyless signature on the air-gapped Docker image tar
- [x] SBOM in SPDX format (LF standard)
- [x] SBOM in CycloneDX format (OWASP)
- [x] SBOM signed (cosign keyless)
- [x] SHA-256 verification (`SHA256SUMS` file, signed)
- [x] Customer verification helper script (`scripts/tools/dx/verify_release.sh`)
- [x] Documented manual verification command (so customer CI doesn't depend on our script)

→ See [`docs/migration-toolkit-installation.md` §Signature Verification](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/docs/migration-toolkit-installation.md#signature-verification).

## Reason Layer 1 doesn't work for you

<!-- Be concrete. Examples:
  - "Our security team policy mandates FIPS-140-2 hardware-backed keys
    for all software signatures."
  - "Air-gapped environment can't reach Rekor; we need offline-verifiable
    GPG ASC chain rooted in our internal trust store."
  - "Windows-only deployment; SmartScreen warns 'Unrecognized publisher'
    and our compliance team blocks the install."
  - "Auditor explicitly rejects sigstore transparency log model."
-->

## Timeline

<!-- When you actually need this. Helps the maintainer prioritise
versus other work. Honest answers welcome — "not blocking, just
nice-to-have for next quarter" lets us batch with other Layer 2 work. -->

- [ ] Blocking production rollout
- [ ] Required for next compliance audit (date: ____)
- [ ] Strongly preferred but workaround exists
- [ ] Future requirement (FYI)

## Anything else

<!-- Internal CA chain we'd need to plumb in? Hardware token brand /
spec? Specific tool versions? Any context that would let the
maintainer scope the work accurately. -->
