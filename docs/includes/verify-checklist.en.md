<!-- Bilingual pair: verify-checklist.md -->

**Standard Verification Checklist:**

- [ ] threshold-exporter `/ready` responds 200
- [ ] Prometheus targets show exporter UP
- [ ] `da-tools diagnose --tenant <NAME>` all items PASS
- [ ] Recording rules correctly produce `tenant:*` metrics
- [ ] Alert rules status normal (no pending/firing false positives)
