---
title: "RBAC Setup Wizard — YAML generator + validator"
purpose: |
  Two pure functions for the RBAC wizard: emit a `groups:` list `_rbac.yaml`
  from a list of group definitions, and lint the same input for common
  configuration mistakes.

  The emitted shape mirrors the tenant-api strict RBAC parser
  (components/tenant-api/internal/rbac/rbac.go — RBACConfig / GroupRule /
  MatchBlock) and docs/schemas/rbac.schema.json: a top-level `groups:` list of
  rules, each with `name` / `tenants` / `permissions` (a LIST) / flat
  `environments` / `domains`, plus the optional claims-aware `match:` block and
  the `org-scope:` axis (ADR-027 / LD-6 P3/P4). The pre-P7d output (a top-level
  `_rbac:` MAP with a `description:` key, a singular `permission:` scalar and a
  nested `filters:` block) was rejected wholesale by that parser — this
  generator is the drift fix (P7d).

  Emission rules that keep the output load-valid AND legacy-compatible:
    * No claims configured  -> NO `match:` block; `name` is the IdP-group
      matcher (byte-for-byte the legacy shape a today's deployment runs).
    * Claims configured     -> `match: { groups: [<name>], claims: {...} }`.
      `name` is copied into `match.groups[0]` so it stays the matcher AND
      doubles as the audit label. `match` is NEVER groups-less (a claims-only
      match block widens the rule to "anyone bearing the claim" — rbac.go:541)
      and NEVER empty/bare (a load error — detectNullMatchBlocks).
    * A single selected permission LEVEL is emitted as a one-element list
      (`permissions: [admin]`); admin ⊇ write ⊇ read is resolved by the server
      (permCovers, rbac.go:1076), so no cumulative [read, write, admin].
    * The per-group free-text `description` is a UI-only hint and is NOT
      emitted (GroupRule has no such field; the strict parser rejects it).
    * "prefix" tenant mode appends a trailing `*` if the operator omitted it —
      a bare `acme-` is an EXACT match in Go (tenantMatches), not a prefix.

  Public API:
    rbacGenerateYaml(groups)     emit the `groups:` YAML block
    rbacValidate(groups)         return [{level, msg}]

  Closure deps: validate uses window.__t for messages.
---

// YAML double-quoted flow scalar: safe for values carrying `*`, `-`, spaces or
// quotes (tenant patterns, env/domain, claim keys/values, group names in a flow
// list). `*` MUST be quoted in flow context (a bare leading `*` is a YAML
// alias). Escapes backslash and double-quote; single-line inputs cannot carry a
// newline.
function yq(s) {
  return '"' + String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
}

// YAML plain scalars that resolve to a NON-string type — decoding one into a Go
// string field either errors or (for the null family) silently yields "". A
// `name:`/`org-scope:` equal to one of these MUST be quoted, or `name: null`
// decodes to an empty Name (a silently dead rule that matches nobody).
const YAML_RESERVED_RE = /^(~|null|Null|NULL|true|True|TRUE|false|False|FALSE|yes|Yes|YES|no|No|NO|on|On|ON|off|Off|OFF)$/;

// Bare block scalar ONLY for a safe identifier YAML cannot re-interpret; quoted
// otherwise. Used for the `name:` / `org-scope:` block values. `:` is excluded
// from the bare set — a trailing colon (`platform:`) makes YAML read a mapping,
// rejecting the whole file at load — and the reserved null/bool family is quoted
// so a string field never decodes to null/bool. (Values with `*`, spaces or
// quotes already fail the charset and fall through to yq.)
function yblock(s) {
  const v = String(s == null ? '' : s);
  if (/^[A-Za-z0-9_.-]+$/.test(v) && !YAML_RESERVED_RE.test(v)) {
    return v;
  }
  return yq(v);
}

// The active claim conditions for a group: a claim contributes only when it has
// a key AND at least one value. Keeps a half-typed row from emitting an empty
// value list (a load error) or a keyless entry.
function activeClaims(group) {
  return (group.claims || []).filter((c) => c && c.key && Array.isArray(c.values) && c.values.length > 0);
}

// The tenant pattern list a group emits, mirroring the three tenant modes. In
// "prefix" mode a trailing `*` is appended when missing so the emitted pattern
// is a real prefix (Go treats `acme-` as an exact id, `acme-*` as a prefix).
// Returns [] when the mode yields nothing (empty prefix / no specific tenants);
// the caller then omits the `tenants:` key and validate flags it.
function tenantPatterns(group) {
  if (group.tenantMode === 'all') return ['*'];
  if (group.tenantMode === 'prefix') {
    const p = (group.tenantPrefix || '').trim();
    if (!p) return [];
    return [p.endsWith('*') ? p : p + '*'];
  }
  if (group.tenantMode === 'specific') {
    return (group.specificTenants || []).map((tenant) => String(tenant).trim()).filter(Boolean);
  }
  return [];
}

function rbacGenerateYaml(groups) {
  let yaml = 'groups:\n';
  for (const group of groups) {
    if (!group.name) continue;
    yaml += `  - name: ${yblock(group.name)}\n`;

    // Claims-aware match block (optional). groups:[name] keeps `name` as the
    // matcher; claims AND-across, values OR-within.
    const claims = activeClaims(group);
    if (claims.length > 0) {
      yaml += `    match:\n`;
      yaml += `      groups: [${yq(group.name)}]\n`;
      yaml += `      claims:\n`;
      for (const c of claims) {
        yaml += `        ${yq(c.key)}: [${c.values.map(yq).join(', ')}]\n`;
      }
    }

    const tenants = tenantPatterns(group);
    if (tenants.length > 0) {
      yaml += `    tenants: [${tenants.map(yq).join(', ')}]\n`;
    }

    // Single selected level → one-element list (admin ⊇ write ⊇ read on the
    // server). Omitted when unset; validate flags the missing permission.
    if (group.permission) {
      yaml += `    permissions: [${group.permission}]\n`;
    }

    // Flat environments / domains (NOT nested under `filters:`); emitted only
    // when non-empty (empty = no restriction).
    if (group.environments && group.environments.length > 0) {
      yaml += `    environments: [${group.environments.map(yq).join(', ')}]\n`;
    }
    if (group.domains && group.domains.length > 0) {
      yaml += `    domains: [${group.domains.map(yq).join(', ')}]\n`;
    }

    // org-scope axis (optional). The key must be declared in the deployment's
    // --identity-claim-headers or the WHOLE file fails to load (validate warns).
    if (group.orgScope) {
      yaml += `    org-scope: ${yblock(group.orgScope)}\n`;
    }
  }
  return yaml;
}

// Tenant pattern grammar, mirrored from validTenantPattern (rbac.go:1057):
//   "*"                      full wildcard
//   no "*"                   exact id (non-blank)
//   exactly one trailing "*" prefix ("acme-*")
//   two or more "*"          INVALID ("**", "*a*", "a**")
function validTenantPattern(pat) {
  if (pat === '*') return true;
  const stars = (pat.match(/\*/g) || []).length;
  if (stars === 0) return pat.trim() !== '';
  if (stars === 1) return pat.endsWith('*') && pat.length > 1;
  return false;
}

// Claim/org-scope keys must match this shape or they can never be declared in
// --identity-claim-headers (principal.go claimKeyRe) — the rule would be dead.
const CLAIM_KEY_RE = /^[A-Za-z0-9_.-]+$/;

function rbacValidate(groups) {
  const t = window.__t || ((zh, en) => en);
  const warnings = [];
  for (const group of groups) {
    if (!group.name) {
      warnings.push({ level: 'error', msg: () => t('群組名稱不能為空', 'Group name cannot be empty') });
    } else if (!group.name.trim()) {
      // A whitespace-only name is truthy (so it isn't skipped) but matches no
      // IdP group — a silent dead rule (or, with claims, a load rejection).
      warnings.push({ level: 'error', msg: () => t('群組名稱不能只有空白字元', 'Group name cannot be only whitespace') });
    }
    if (!group.permission) {
      warnings.push({ level: 'error', msg: () => t('未設定權限', 'Permission not set') });
    }

    // A group that yields no tenant pattern grants access to nothing (a silent
    // dead rule); flag both mistake shapes (empty prefix / no specific tenants).
    const tenants = tenantPatterns(group);
    if (tenants.length === 0) {
      warnings.push({ level: 'error', msg: () => t(`群組 "${group.name}" 未指定任何租戶（規則不會授予任何存取）`, `Group "${group.name}" grants no tenants (the rule authorizes nothing)`) });
    }
    for (const pat of tenants) {
      if (!validTenantPattern(pat)) {
        warnings.push({ level: 'error', msg: () => t(`租戶樣式 "${pat}" 不合法（允許："*"、單一結尾 "*" 的前綴、或精確 id）`, `Invalid tenant pattern "${pat}" (allowed: "*", a single trailing "*" prefix, or an exact id)`) });
      }
    }

    if (group.tenantMode === 'all' && group.permission === 'admin') {
      warnings.push({
        level: 'warning',
        msg: () => t(`群組 "${group.name}" 有管理員權限且可訪問所有租戶 - 非常寬鬆，請確認`, `Group "${group.name}" has admin on all tenants - very broad, please confirm`),
      });
    }

    // A half-typed claim row (a key with no committed values, or values with no
    // key) is SILENTLY DROPPED by activeClaims — and a claim the operator meant
    // to add but that vanishes leaves the rule WIDER than intended (no match
    // block). Surface it rather than dropping in silence.
    for (const c of group.claims || []) {
      const hasKey = !!(c && c.key && String(c.key).trim());
      const hasVals = !!(c && Array.isArray(c.values) && c.values.length > 0);
      if (hasKey && !hasVals) {
        warnings.push({ level: 'warning', msg: () => t(`宣告 "${c.key}" 未填任何值，將被忽略（該規則不會套用此條件）`, `Claim "${c.key}" has no values and will be dropped (the rule will not apply this condition)`) });
      } else if (!hasKey && hasVals) {
        warnings.push({ level: 'warning', msg: () => t('有一列宣告值未填鍵，將被忽略', 'A claim row has values but no key and will be dropped') });
      }
    }

    // Claims axis: keys must be declarable, values non-blank. Whether a key is
    // actually declared in the deployment is UNKNOWABLE offline → warn below.
    const claims = activeClaims(group);
    for (const c of claims) {
      if (!CLAIM_KEY_RE.test(c.key)) {
        warnings.push({ level: 'error', msg: () => t(`宣告鍵 "${c.key}" 含不允許的字元（僅限英數、底線、點、連字號）`, `Claim key "${c.key}" has disallowed characters (only letters, digits, _ . -)`) });
      }
      for (const v of c.values) {
        if (!String(v).trim()) {
          warnings.push({ level: 'error', msg: () => t(`群組 "${group.name}" 的宣告 "${c.key}" 含空值`, `Group "${group.name}" claim "${c.key}" has an empty value`) });
        }
      }
    }

    if (group.orgScope && !CLAIM_KEY_RE.test(group.orgScope)) {
      warnings.push({ level: 'error', msg: () => t(`org-scope 鍵 "${group.orgScope}" 含不允許的字元`, `org-scope key "${group.orgScope}" has disallowed characters`) });
    }

    // The one failure an offline generator cannot detect: a claim / org-scope
    // key the deployment did not declare fails the WHOLE file at load.
    if (claims.length > 0 || group.orgScope) {
      warnings.push({
        level: 'warning',
        msg: () => t(
          `群組 "${group.name}" 使用宣告軸（claims/org-scope）：這些鍵必須已在部署的 --identity-claim-headers 宣告，否則整份 _rbac.yaml 會載入失敗。載入前請用租戶管理員的稽核（dry-run）驗證。`,
          `Group "${group.name}" uses an identity axis (claims/org-scope): these keys MUST be declared in the deployment's --identity-claim-headers or the ENTIRE _rbac.yaml fails to load. Verify with the admin audit (dry-run) before deploying.`,
        ),
      });
    }
  }
  return warnings;
}

export { rbacGenerateYaml, rbacValidate };
