#!/usr/bin/env python3
"""Guard: every group try-local's dev bypass injects is a platform admin in its _rbac.yaml (#3880).

try-local runs tenant-api with the local-dev auth bypass on
(`TA_DEV_BYPASS_AUTH`, ADR-022 / ADR-027 §3): oauth2-proxy lets every request
through without a session, and the bypass hands tenant-api an identity object
on the socket plane, with the groups listed in `TA_DEV_BYPASS_GROUPS`. Those
groups are then looked up, by exact string, in `try-local/seed/conf.d/_rbac.yaml`.

The two files used to be tied together by a comment listing "three places that
must say demo-admins" — one of which (`smoke.sh` sending the group as a header)
stopped being true when ADR-027 made the API strip client identity headers.
A comment cannot go red. This module is the tie instead.

What the demo needs from the bypass identity is the platform-admin gate
(`rbac.Manager.PlatformAdminUnrestricted`, `internal/rbac/reverse.go`): at
least one rule that matches the group, lists the literal tenant `"*"`, grants
`admin`, and carries no `org-scope` / `environments` / `domains`. Without it
the Tenant Manager lists nothing (`/api/v1/tenants` is RBAC-filtered — the
nightly smoke wants both seed tenants) and the platform-level portal views are
refused. One typo in either file is enough, and nothing else fails until the
nightly runs the whole stack.

⛔ SCOPE — what a green run does NOT say:

* Only legacy name matching is modelled (`name:` equal to the group). A rule
  with a `match:` block is refused here rather than half-evaluated: if the
  demo ever needs claims matching, extend this check against
  `internal/rbac` first.
* `TA_DEV_BYPASS_GROUPS` must be set in the compose file. tenant-api has a
  built-in default for it (`cmd/server/main.go`), and a group that only
  exists as that default is invisible to anyone reading the compose file.
* Whether tenant-api actually loads this `_rbac.yaml` (the volume mount, the
  file name) is the nightly smoke's job, not this module's.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "try-local" / "docker-compose.yaml"
RBAC = REPO_ROOT / "try-local" / "seed" / "conf.d" / "_rbac.yaml"

BYPASS_SWITCH = "TA_DEV_BYPASS_AUTH"
BYPASS_GROUPS = "TA_DEV_BYPASS_GROUPS"
SCOPE_AXES = ("org-scope", "environments", "domains")


def _env(service: dict) -> dict:
    """A compose service's `environment`, in either of its two shapes."""
    env = service.get("environment") or {}
    if isinstance(env, list):
        out = {}
        for item in env:
            key, _, value = str(item).partition("=")
            out[key] = value
        return out
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def bypass_groups(compose: dict) -> "dict[str, list[str]]":
    """Service name -> the groups its dev bypass injects (split like parseForwardedGroups)."""
    found = {}
    for name, service in (compose.get("services") or {}).items():
        env = _env(service or {})
        if env.get(BYPASS_SWITCH, "").lower() != "true":
            continue
        raw = env.get(BYPASS_GROUPS)
        found[name] = None if raw is None else [g.strip() for g in raw.split(",") if g.strip()]
    return found


def platform_admin_problems(rbac: dict, group: str) -> "list[str]":
    """Why no rule in `rbac` makes `group` a platform admin; empty when one does."""
    rules = [r for r in (rbac.get("groups") or []) if (r or {}).get("name") == group]
    if not rules:
        return [f"no rule named {group!r}"]
    problems = []
    for i, rule in enumerate(rules):
        why = []
        if "match" in rule:
            why.append("has a `match:` block (only name matching is modelled here)")
        if "*" not in (rule.get("tenants") or []):
            why.append('tenants does not list the literal "*"')
        if "admin" not in (rule.get("permissions") or []):
            why.append("permissions does not grant admin")
        why += [f"carries `{axis}`" for axis in SCOPE_AXES if rule.get(axis)]
        if not why:
            return []
        problems.append(f"rule #{i} named {group!r}: " + "; ".join(why))
    return problems


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rbac() -> dict:
    return yaml.safe_load(RBAC.read_text(encoding="utf-8"))


def test_the_bypass_is_on_somewhere(compose) -> None:
    # Without this an empty result would pass every check below.
    assert bypass_groups(compose), f"no service in {COMPOSE.name} sets {BYPASS_SWITCH}=true"


def test_every_bypass_service_names_its_groups(compose) -> None:
    missing = [s for s, groups in bypass_groups(compose).items() if not groups]
    assert not missing, (
        f"{', '.join(missing)}: {BYPASS_SWITCH} is on but {BYPASS_GROUPS} is unset or empty in "
        f"{COMPOSE.relative_to(REPO_ROOT)} — set it, so the groups _rbac.yaml must cover are "
        "visible in the compose file rather than in tenant-api's built-in default"
    )


def test_every_bypass_group_is_a_platform_admin(compose, rbac) -> None:
    failures = [
        f"{service}: {problem}"
        for service, groups in bypass_groups(compose).items()
        for group in groups or []
        for problem in platform_admin_problems(rbac, group)
    ]
    assert not failures, (
        f"{RBAC.relative_to(REPO_ROOT)} does not make every dev-bypass group a platform admin "
        "(tenants [\"*\"], admin, no scope axes):\n  " + "\n  ".join(failures)
    )


# --- the check bites: each broken copy is paired with the real file passing ---

_GOOD = {"groups": [{"name": "g", "tenants": ["*"], "permissions": ["read", "write", "admin"]}]}


def _broken(**changes) -> dict:
    doc = copy.deepcopy(_GOOD)
    doc["groups"][0].update(changes)
    return doc


def test_a_correct_rule_passes() -> None:
    assert platform_admin_problems(_GOOD, "g") == []


@pytest.mark.parametrize("doc, needle", [
    (_broken(name="g-typo"), "no rule named"),
    (_broken(tenants=["db-demo", "cache-demo"]), '"*"'),
    (_broken(tenants=["*-demo"]), '"*"'),
    (_broken(permissions=["read", "write"]), "admin"),
    (_broken(environments=["production"]), "environments"),
    (_broken(domains=["finance"]), "domains"),
    (_broken(**{"org-scope": "acme"}), "org-scope"),
    (_broken(match={"groups": ["g"]}), "match"),
], ids=["typo", "listed-tenants", "prefix-pattern", "no-admin",
        "environments", "domains", "org-scope", "match-block"])
def test_a_broken_rule_is_named(doc, needle) -> None:
    problems = platform_admin_problems(doc, "g")
    assert problems and needle in problems[0], problems


def test_one_good_rule_among_bad_ones_is_enough() -> None:
    # PlatformUnrestricted passes on ANY qualifying rule, so this must too.
    doc = {"groups": [_broken(permissions=["read"])["groups"][0], _GOOD["groups"][0]]}
    assert platform_admin_problems(doc, "g") == []


@pytest.mark.parametrize("env, want", [
    ({"TA_DEV_BYPASS_AUTH": "true", "TA_DEV_BYPASS_GROUPS": " a, b ,,"}, ["a", "b"]),
    (["TA_DEV_BYPASS_AUTH=true", "TA_DEV_BYPASS_GROUPS=a"], ["a"]),
    ({"TA_DEV_BYPASS_AUTH": "true"}, None),
], ids=["mapping-split", "list-form", "unset"])
def test_bypass_groups_reads_both_compose_shapes(env, want) -> None:
    assert bypass_groups({"services": {"s": {"environment": env}}}) == {"s": want}


def test_a_service_without_the_bypass_is_ignored() -> None:
    env = {"TA_DEV_BYPASS_GROUPS": "a"}
    assert bypass_groups({"services": {"s": {"environment": env}}}) == {}
