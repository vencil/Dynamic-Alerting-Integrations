/**
 * vibe-security-audit — reusable domain-aware security-audit workflow.
 *
 * Pattern adapted from Cloudflare's open-source `security-audit-skill`
 * (github.com/cloudflare/security-audit-skill, MIT): multi-agent
 * Recon -> Hunt -> adversarial Validate -> Synthesize, "only report
 * exploitable". Wrapped with Vibe's own trust model + attack classes
 * (adopt-then-wrap, per lint-adoption-policy). Orchestrated
 * deterministically via the Workflow tool (harness > model); per-role
 * stance / tools / model live in .claude/agents/vibe-sec-{recon,hunter,
 * validator}.md and are referenced here via agentType.
 *
 * Invoke via the `vibe-security-audit` skill. args MUST be a JSON OBJECT:
 *   { target: "<abs path to component>",   // REQUIRED
 *     componentLabel?: "tenant-api",
 *     attackClasses?: [{ key, title, scope, files }] }  // else Vibe defaults
 */
export const meta = {
  name: 'vibe-security-audit',
  description: 'Domain-aware multi-agent security audit (Recon->Hunt->Validate->Synthesize) for a Vibe component',
  phases: [
    { title: 'Recon', detail: 'map trust boundaries + input surfaces (sonnet)' },
    { title: 'Hunt', detail: 'parallel Vibe-specific attack classes (opus)' },
    { title: 'Validate', detail: 'independent cross-model disprove pass (sonnet)' },
    { title: 'Synthesize', detail: 'coverage-aware verdict + report (sonnet)' },
  ],
}

// --- normalize args: accept an OBJECT or (defensively) a JSON string. The Workflow
// tool wants a JSON object, but passing a stringified value is an easy mistake
// (it left .target undefined in the PoC); parse the string form rather than fail. ---
let A = args
if (typeof A === 'string') {
  try { A = JSON.parse(A) } catch (e) {
    throw new Error('vibe-security-audit: args was a string that is not valid JSON. Pass a JSON object like { target: "C:/.../components/tenant-api" }.')
  }
}
if (!A || typeof A !== 'object' || Array.isArray(A) || !A.target) {
  throw new Error('vibe-security-audit: args must resolve to an OBJECT with a `target` absolute path, e.g. { target: "C:/.../.claude/worktrees/sec-audit/components/tenant-api" }.')
}
const ROOT = A.target
if (!/^(?:[a-zA-Z]:[\\/]|\/)/.test(ROOT)) {
  throw new Error(`vibe-security-audit: target must be an ABSOLUTE path (got "${ROOT}") — a relative path would be interpolated into agent prompts and read the wrong tree, defeating worktree isolation.`)
}
const LABEL = A.componentLabel || 'component'

// --------------------------- schemas ---------------------------
const RECON_SCHEMA = { type: 'object', additionalProperties: false,
  required: ['trust_boundaries', 'input_surfaces', 'auth_model', 'tenant_isolation_mechanism'],
  properties: {
    trust_boundaries: { type: 'array', items: { type: 'string' } },
    input_surfaces: { type: 'array', items: { type: 'string' } },
    auth_model: { type: 'string' },
    tenant_isolation_mechanism: { type: 'string' },
    notes: { type: 'string' },
  } }

const HUNT_SCHEMA = { type: 'object', additionalProperties: false, required: ['attack_class', 'findings'],
  properties: {
    attack_class: { type: 'string' },
    findings: { type: 'array', items: { type: 'object', additionalProperties: false,
      required: ['title', 'severity', 'file', 'attacker', 'action', 'result', 'code_evidence'],
      properties: {
        title: { type: 'string' },
        severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] },
        file: { type: 'string' },
        line: { type: 'integer' },
        attacker: { type: 'string' },
        action: { type: 'string' },
        result: { type: 'string' },
        code_evidence: { type: 'string' },
        exploitability: { type: 'string' },
      } } },
  } }

const VERDICT_SCHEMA = { type: 'object', additionalProperties: false,
  required: ['verdict', 'reasoning', 'domain_aware', 'misread_designed_behavior'],
  properties: {
    verdict: { type: 'string', enum: ['CONFIRMED', 'REJECTED', 'NEEDS_INFO'] },
    reasoning: { type: 'string' },
    code_cite: { type: 'string' },
    domain_aware: { type: 'boolean' },
    misread_designed_behavior: { type: 'boolean' },
  },
  // code_cite is required when a finding is CONFIRMED (the validator prompt already
  // mandates citing the exact file:line on confirm — this is the schema-level backstop).
  if: { properties: { verdict: { const: 'CONFIRMED' } } },
  then: { required: ['code_cite'] },
}

const SYNTH_SCHEMA = { type: 'object', additionalProperties: false,
  required: ['domain_awareness_verdict', 'domain_awareness_evidence', 'coverage_note', 'summary', 'recommendation'],
  properties: {
    domain_awareness_verdict: { type: 'string', enum: ['STRONG', 'PARTIAL', 'WEAK'] },
    domain_awareness_evidence: { type: 'string' },
    coverage_note: { type: 'string' },
    summary: { type: 'string' },
    confirmed_real_findings: { type: 'array', items: { type: 'string' } },
    rejected_noise_examples: { type: 'array', items: { type: 'string' } },
    recommendation: { type: 'string' },
  } }

// --------------------------- domain context ---------------------------
const DOMAIN = 'Vibe = multi-tenant alerting platform; tenant-api is the Go control-plane, threshold-exporter + scripts/tools/dx/custom_alerts are the recipe/rule compiler. Trust model to attack: (1) tenant identity comes from auth context — a tenant MUST NOT read or write another tenant config/alerts/views/effective-rules (cross-tenant isolation). (2) A dev-auth-bypass (ADR-022) and an RBAC "open mode" exist — both MUST be unreachable / fail-CLOSED in production (#991 MED-8). (3) Federation tokens are TTL-scoped, mint-rate-limited, revocable; replaying revoked/expired tokens or minting beyond policy is a breach. (4) Tenants self-serve custom alerts and drive a git write plane; tenant-supplied content must not inject into PromQL/YAML/config/annotation-templates or spoof write-source / bypass gitops admission. VERIFY every claim against the ACTUAL code; if the code contradicts this summary, say so.'

const ANTIP = 'Only report EXPLOITABLE findings with a concrete who/what-input/what-result scenario. REJECT "theoretically/potentially", OWASP-deviation-as-bug, defense-in-depth gaps (hardening notes, not findings), and DESIGNED behavior misread as a bug. Do NOT pad with LOW/theoretical items; an empty result for a hardened area is valid. Severity = likelihood x impact.'

// --------------------------- attack classes (Vibe defaults) ---------------------------
const DEFAULT_CLASSES = [
  { key: 'cross-tenant', title: 'Cross-tenant access & tenant-id authorization bypass',
    scope: 'Can tenant X read/mutate tenant Y data (config, alerts, views, effective rules, diff, search, batch)? tenant-id from body/query/path/header instead of auth context, missing tenant-scope checks, IDOR, batch endpoints skipping per-item authz.',
    files: 'internal/handler/{authz,middleware,me,tenant_get,tenant_put,tenant_effective,tenant_batch,tenant_search,tenant_diff,view}.go, internal/rbac/, internal/groups/, internal/policy/, internal/views/, internal/ws/' },
  { key: 'authbypass', title: 'Auth boundary, dev-bypass & RBAC open-mode containment, IP/header trust',
    scope: 'Can the ADR-022 dev-auth-bypass or RBAC open-mode be reached in a production config (fail-OPEN)? Can caller identity or client IP be spoofed via X-Forwarded-* headers to defeat rate-limit or authz (#991)? default-allow, env/flag reachability, trusting client-supplied identity/IP.',
    files: 'internal/rbac/devbypass.go, internal/rbac/*.go, internal/handler/{authz,middleware,me,rate_limiter}.go, internal/platform/forge_ratelimit.go' },
  { key: 'federation', title: 'Federation token mint / revocation / admission / orphan abuse',
    scope: 'Mint tokens beyond policy or rate-limit, replay a revoked/expired token, bypass federation admission validation, or exploit orphan detection to escalate. Token store/verify, mint_limiter, fedpolicy admission, revocation bookkeeping.',
    files: 'internal/federation/token/*, internal/federation/fedpolicy/*, internal/federation/orphan/*, internal/handler/federation/*' },
  { key: 'injection', title: 'Tenant-supplied content injection & git write-plane abuse',
    scope: 'Tenant custom-alert / write content injecting into PromQL / YAML / config / Go-template ANNOTATIONS (a distinct sink from PromQL selectors), escaping sanitize/body-validator, spoofing write-source, or bypassing gitops admission. Trace tenant input -> merge/extract -> compiler (recipes.py/shape.py) -> admission -> writer -> git, ACROSS component boundaries.',
    files: 'internal/customalerts/*, internal/handler/{sanitize,body_validator,tenant_custom_alerts,write_source,pr_writeflow}.go, internal/gitops/*  AND the cross-component compiler: components/threshold-exporter/app/pkg/config/, scripts/tools/dx/custom_alerts/{recipes,shape,loader}.py' },
]
const CLASSES = (Array.isArray(A.attackClasses) && A.attackClasses.length) ? A.attackClasses : DEFAULT_CLASSES

// --------------------------- Phase 1: Recon ---------------------------
phase('Recon')
log(`Recon: mapping ${LABEL} at ${ROOT} (sonnet)`)
const recon = await agent(
  `[Recon] Target: ${ROOT}. ${DOMAIN}\nMap: trust boundaries, ALL input surfaces (routes/handlers + auth level), the auth/identity model, and the concrete tenant-isolation mechanism. Cite file:line. Flag unverified external trust deps. Note unread subtrees (coverage honesty). Output the structured map.`,
  { agentType: 'vibe-sec-recon', model: 'sonnet', phase: 'Recon', schema: RECON_SCHEMA }
)
log(`Recon done: ${(recon && recon.input_surfaces ? recon.input_surfaces.length : 0)} input surfaces, ${(recon && recon.trust_boundaries ? recon.trust_boundaries.length : 0)} trust boundaries`)
const reconBrief = JSON.stringify(recon || {}).slice(0, 6000)

// --------------------------- Phase 2+3: Hunt (opus) -> Validate (sonnet, cross-model) ---------------------------
phase('Hunt')
log(`Hunt: ${CLASSES.length} attack classes in parallel (opus)`)
const huntCounts = []
const perClass = await pipeline(
  CLASSES,
  async (cls) => {
    const h = await agent(
      `[Hunter — only exploitable, concrete who/what/result + file:line, no padding, trace CROSS-COMPONENT sinks] Attack class: ${cls.title}. Scope: ${cls.scope}. Start files (relative to ${ROOT} unless an explicit components/... or scripts/... path is given): ${cls.files}. RECON MAP: ${reconBrief}. ${DOMAIN} ${ANTIP} Return MAX 3 strongest findings, or an EMPTY findings array if none are real.`,
      { agentType: 'vibe-sec-hunter', model: 'opus', label: `hunt:${cls.key}`, phase: 'Hunt', schema: HUNT_SCHEMA }
    )
    const n = (h && h.findings ? h.findings : []).length
    huntCounts.push({ class: cls.key, findings: n, hunted: !!h })
    return h
  },
  (h, cls) => {
    const fs = (h && h.findings ? h.findings : []).slice(0, 3)
    if (!fs.length) return []
    return parallel(fs.map(f => () =>
      agent(
        `[Validator — DISPROVE this] Finding (class ${cls.key}): ${JSON.stringify(f)}. Read the ACTUAL source under ${ROOT} (and any cross-component path the finding cites) at every step. ${DOMAIN} Apply exploitation/impact/mitigation/designed-behavior tests. CONFIRM only if you cannot disprove (cite exact code). Default to skepticism. Set domain_aware + misread_designed_behavior.`,
        { agentType: 'vibe-sec-validator', model: 'sonnet', label: `val:${cls.key}`, phase: 'Validate', schema: VERDICT_SCHEMA }
      ).then(v => ({ class: cls.key, finding: f, verdict: v }))
    ))
  }
)

const all = perClass.flat().filter(Boolean)
const confirmed = all.filter(x => x.verdict && x.verdict.verdict === 'CONFIRMED')
log(`Validate: ${all.length} findings, ${confirmed.length} confirmed; coverage ${JSON.stringify(huntCounts)}`)

// --------------------------- Phase 4: Synthesize (coverage-aware) ---------------------------
phase('Synthesize')
const compact = all.map(x => ({
  class: x.class,
  title: x.finding && x.finding.title,
  severity: x.finding && x.finding.severity,
  file: x.finding && x.finding.file,
  verdict: x.verdict && x.verdict.verdict,
  domain_aware: x.verdict && x.verdict.domain_aware,
  misread: x.verdict && x.verdict.misread_designed_behavior,
  why: x.verdict && x.verdict.reasoning,
}))
const synth = await agent(
  `[Synthesis] Judge this security audit of ${LABEL}. COVERAGE (every attack class that ran, including empty ones): ${JSON.stringify(huntCounts)}. FINDINGS+VERDICTS: ${JSON.stringify(compact)}. ${DOMAIN}\nAn EMPTY class after genuine reading means "no NEW exploitable bug in the code that EXISTS" — credit it in coverage_note, but DISTINGUISH a genuinely hardened area from a MID-REMEDIATION / known-open boundary where "empty" must NOT be read as "resolved" (say which, and name the pending fix). Do NOT treat an empty class as un-audited either. Assess domain_awareness_verdict (STRONG/PARTIAL/WEAK) with concrete evidence, summarize confirmed real issues vs rejected noise, and recommend next steps (what to fix, what to re-audit).`,
  { model: 'sonnet', phase: 'Synthesize', schema: SYNTH_SCHEMA }
)

return { target: ROOT, label: LABEL, recon, coverage: huntCounts, findings: compact, confirmed_count: confirmed.length, synthesis: synth }
