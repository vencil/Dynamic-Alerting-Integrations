"""每支讀 conf.d 的工具都以 yaml.v3 的讀法讀值（ADR-036 步驟 4，#2906）。

判準（#2906 裁決 2026-10-17）：同一棵樹裡，一個值寫成未加引號的 `yes`，與寫成
加引號的 `"yes"`，工具的輸出（結束碼、stdout、stderr、寫出的檔）必須完全相同——
yaml.v3 兩者都讀成字串 "yes"；PyYAML（YAML 1.1）把前者讀成布林 True。另用
`true` 那棵樹確認工具真的讀了這個欄位：它的輸出必須與 `"yes"` 不同，否則前一條
在一支根本不讀這欄位的工具上也成立，什麼都沒證明。

⛔ 欄位不用 `send_resolved`：產生器今天已把字串 yes / no 轉回布林，那個欄位測不出
工具切了沒有。每支工具用它一定會讀、而且「布林」與「字串」會走不同路的欄位
（`_PROBES` 的 `field` 欄）。

母體是 `_confd_population.population_keys`（讀 conf.d 的 scripts/tools 檔，與
enumeration / case-parity / strict-yaml 三道閘門同一份答案），雙向對帳：母體裡的
每個檔，要嘛在 `_PROBES` 有一個探針，要嘛在 `_NOT_PROBED` 寫明為什麼不探；母體
沒有的檔出現在任一表裡也是紅。

還沒切換到 parse-tree 的工具，探針標 `pending`：xfail(strict=True)——它若意外
通過就轉紅，提醒把標記拿掉。最後一支切換 PR（步驟 4d）清空 pending。

⚠️ 這是抽樣，不是完備性證明：一支工具只量一個欄位。它量的是 ADR 真正要的性質，
不論工具怎麼解析（包裝函式、`exec`、別名），讀法錯了就逃不過。`_NOT_PROBED` 的
"pending, no probe yet" 那一類就是還沒量到的工具，數得出來，不是被當成通過。
"""
from __future__ import annotations

import functools
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

from _confd_population import population_keys

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"

_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
_RECEIVER = ("    _routing:\n      receiver:\n        type: webhook\n"
             "        url: https://hook.example/x\n")

# ── the trees: {VALUE} is the value under test, spelled per variant ──────
#: `_routing_enforced.enabled` in the root carrier — a boolean field the
#: route generator reads; `yes` (bool to PyYAML) enables the NOC route,
#: `"yes"` (a string) refuses the tree.
_ENFORCED = {
    "_defaults.yaml": (_DEFAULTS + "_routing_enforced:\n  enabled: {VALUE}\n"
                       "  receiver:\n    type: webhook\n    url: https://noc.example/x\n"),
    "tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_connections: "70"\n' + _RECEIVER,
}
#: A tenant's `_routing_profile` — a string field: a boolean is no reference.
_PROFILE_REF = {
    "_defaults.yaml": _DEFAULTS,
    "_routing_profiles.yaml": ("routing_profiles:\n  team-x:\n    receiver:\n"
                               "      type: webhook\n      url: https://hook.example/p\n"),
    "tenant-a.yaml": ('tenants:\n  tenant-a:\n    mysql_connections: "70"\n'
                      "    _routing_profile: {VALUE}\n"),
}
#: A `_policies` rule's `value` — compared with the tenant's `_metadata.owner`.
_POLICY_VALUE = {
    "_defaults.yaml": (_DEFAULTS + "_policies:\n  - name: owner-is\n    target: _metadata.owner\n"
                       "    operator: equals\n    value: {VALUE}\n    severity: error\n"),
    "tenant-a.yaml": ('tenants:\n  tenant-a:\n    mysql_connections: "70"\n'
                      '    _metadata:\n      owner: "yes"\n'),
}

#: An `_instance_mapping.yaml` entry's `tenant:` — a tenant id, read as text
#: (a boolean `true` is the id "true", not "yes").
_MAPPING_TENANT = {
    "_defaults.yaml": _DEFAULTS,
    "_instance_mapping.yaml": ("instance_tenant_mapping:\n  i1:\n"
                               "    - tenant: {VALUE}\n      filter: 'schema=\"a\"'\n"),
}
#: A root `_defaults.yaml` top-level value — OPA's `input.defaults` carries it.
_OPA_DEFAULT = {
    "_defaults.yaml": _DEFAULTS + "flag: {VALUE}\n",
    "tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_connections: "70"\n',
}
#: A tenant's `_metadata.environment` under a `prod/` directory: a string that
#: disagrees with the path is a finding; a boolean is no environment at all.
_PATH_ENV = {
    "prod/tenant-a.yaml": ("tenants:\n  tenant-a:\n    _metadata:\n"
                           "      environment: {VALUE}\n"),
}

#: A tenant file's `_metadata.domain` — the directory migrate_conf_d moves it
#: under: a string is a path, a boolean is unplaceable.
_META_DOMAIN = {
    "tenant-a.yaml": "tenants:\n  tenant-a:\n    _metadata:\n      domain: {VALUE}\n",
}
#: Another file's value naming the tenant being offboarded (`yes`): a string
#: "yes" is a cross-reference, a boolean is not.
_OFFBOARD_REF = {
    "yes.yaml": 'tenants:\n  "yes":\n    mysql_connections: "70"\n',
    "other.yaml": "tenants:\n  other:\n    note: {VALUE}\n",
}

#: A recurring maintenance window's `reason` — printed on the ACTIVE line
#: (`True` for a boolean). The window is "0 0 1 1 *" + 400d, so it is open on
#: any day of any year and starts at the same instant in every run (the line
#: prints it); only two runs straddling a New Year could see different starts.
_MAINT_REASON = {
    "_defaults.yaml": _DEFAULTS,
    "tenant-a.yaml": ('tenants:\n  tenant-a:\n    mysql_connections: "70"\n'
                      "    _state_maintenance:\n      recurring:\n"
                      '        - cron: "0 0 1 1 *"\n          duration: "400d"\n'
                      "          reason: {VALUE}\n"),
}
#: A tenant key with no default — threshold_recommend reports its
#: `current_value` as-is (`"yes"` / `true`). ⛔ Not a key the defaults carry:
#: da-guard logs a timestamped WARN for a non-numeric value of a defaulted
#: metric, and the timestamp would make two runs differ on their own.
_TENANT_KEY = {
    "_defaults.yaml": _DEFAULTS,
    "tenant-a.yaml": ('tenants:\n  tenant-a:\n    mysql_connections: "70"\n'
                      "    custom_widget: {VALUE}\n"),
}

_VARIANTS = {"plain": "yes", "quoted": '"yes"', "bool": "true"}

# tool → (script, argv ({d} = the conf.d, {out} = a scratch dir), tree,
#         files under {out} that are output, why it is still expected to fail
#         — "" once it reads the yaml.v3 way)
_PROBES: dict[str, tuple[str, list[str], dict[str, str], tuple[str, ...], str]] = {
    # ── switched to parse-tree in this step (#2906) ──
    "ops/generate_alertmanager_routes.py": (
        "ops/generate_alertmanager_routes.py",
        ["--config-dir", "{d}", "--validate", "--strict", "--findings-json", "{out}/f.json"],
        _ENFORCED, ("f.json",), ""),
    "ops/validate_config.py": (
        "ops/validate_config.py", ["--config-dir", "{d}", "--json"], _ENFORCED, (), ""),
    "ops/explain_route.py": (
        "ops/explain_route.py", ["--config-dir", "{d}", "--tenant", "tenant-a", "--json"],
        _ENFORCED, (), ""),
    "ops/notification_tester.py": (
        "ops/notification_tester.py", ["--config-dir", "{d}", "--dry-run", "--json"],
        _ENFORCED, (), ""),
    "ops/policy_engine.py": (
        "ops/policy_engine.py", ["--config-dir", "{d}", "--json", "--ci"],
        _POLICY_VALUE, (), ""),
    "lint/check_routing_profiles.py": (
        "lint/check_routing_profiles.py", ["--config-dir", "{d}", "--strict"],
        _PROFILE_REF, (), ""),
    "lint/check_confd_schema.py": (
        "lint/check_confd_schema.py", ["--config-dir", "{d}"], _ENFORCED, (), ""),
    # ── the read-only tools switched in step E1 (#2906) ──
    "ops/generate_tenant_mapping_rules.py": (
        "ops/generate_tenant_mapping_rules.py",
        ["--config-dir", "{d}", "--metrics", "cpu", "--dry-run"], _MAPPING_TENANT, (), ""),
    "ops/policy_opa_bridge.py": (
        "ops/policy_opa_bridge.py", ["--config-dir", "{d}", "--dry-run"], _OPA_DEFAULT, (), ""),
    "lint/check_path_metadata_consistency.py": (
        "lint/check_path_metadata_consistency.py", ["--config-dir", "{d}", "--ci"],
        _PATH_ENV, (), ""),
    # ── the conf.d writers switched in step E2 (#2906) ──
    "dx/migrate_conf_d.py": (
        "dx/migrate_conf_d.py", ["--conf-d", "{d}", "--dry-run"], _META_DOMAIN, (), ""),
    "ops/offboard_tenant.py": (
        "ops/offboard_tenant.py", ["yes", "--config-dir", "{d}"], _OFFBOARD_REF, (), ""),
    # ── already on da-guard (effective / served-values), probed in F1 (#2906) ──
    "ops/maintenance_scheduler.py": (
        "ops/maintenance_scheduler.py", ["--config-dir", "{d}", "--json-output"],
        _MAINT_REASON, (), ""),
    "ops/threshold_recommend.py": (
        "ops/threshold_recommend.py", ["--config-dir", "{d}", "--dry-run", "--json"],
        _TENANT_KEY, (), ""),
}

# population file → why it has no probe. Three kinds, so the table reads as
# what is measured and what is not:
_LIB = "library module: no CLI of its own; its callers' probes measure it"
_NAMES = "reads conf.d names / paths / bytes, no value of a file"
_GO = "reads conf.d values only through da-guard (served-values / effective / parse-tree)"
_PENDING = ("pending (4c): not on parse-tree yet, or not established here whether it "
            "reads conf.d values at all — no probe written yet")
#: Step E1 (#2906): tools whose conf.d reads are da-guard's but which take no
#: conf.d argument (fixed repo-relative trees), or read no value a probe can
#: tell apart (key names only): `_GO`, and their own tests in
#: tests/shared/test_readonly_tools_read_parse_tree.py measure the yaml.v3
#: reading, the closure, the unreadable file and the missing da-guard.
_GO_FIXED = (_GO + "; fixed repo-relative conf.d trees, no conf.d argument to probe — "
             "tests/shared/test_readonly_tools_read_parse_tree.py")
_GO_KEYS = (_GO + "; Python reads only key names / truthiness from parse-tree, no value a "
            "yes/\"yes\"/true probe tells apart — "
            "tests/shared/test_readonly_tools_read_parse_tree.py")
#: Step E2 (#2906): the conf.d writers. Every decision is parse-tree's, PyYAML
#: only produces the text of a write, and parse-tree reads that text back
#: before it is written (ADR-036 §2). They take no conf.d argument a probe can
#: drive without a cluster or an API (patch_config: a ConfigMap via kubectl;
#: threshold_govern: tenant-api; da_assembler: a CR), or write what they
#: generate rather than read a value (init_project, scaffold_tenant), or read
#: a value whose yes / "yes" / true readings end the same (deprecate_rule:
#: every non-number `defaults:` value is a dropped carrier) — their own rows
#: are in tests/shared/test_writers_confirm_parse_tree.py.
_GO_WRITER = (_GO + "; a conf.d writer confirmed by parse-tree before it writes — "
              "tests/shared/test_writers_confirm_parse_tree.py")
#: A conf.d file read from `git show` blobs (parse-tree's file mode), one
#: fixed name at two revisions — no conf.d tree or argument a probe can drive.
_GO_BLOB = (_GO + "; reads git blobs of one fixed-name file (`git show`) in parse-tree's "
            "file mode, no conf.d argument to probe — "
            "tests/shared/test_readonly_tools_read_parse_tree.py")
_OWNER_2115 = ("--config-dir reads served-values only; the single-file `--tenant-config` "
               "branch stays on PyYAML by owner ruling #2115 (decision 4: not switched), "
               "and no run mixes the two")
#: Admitted to the population only by the K3 text net (`conf.d` / `config_dir`
#: in a docstring, comment, help or output string); what the tool reads is
#: named after the `;` in its row.
_PROSE_ONLY = ("only mentions a conf.d in prose (docstring / comment / help / output text) "
               "— the K3 text net; reads no tenant conf.d")
#: blast_radius reads two `describe-tenant --all --output` JSON files, never a
#: conf.d: their values are da-guard effective's, through dx/describe_tenant.py
#: (its own `_GO` row).
_GO_VIA_DESCRIBE = (_GO + "; indirectly — it reads `describe-tenant --all` JSON "
                    "(dx/describe_tenant.py), never a conf.d file")
_NOT_PROBED: dict[str, str] = {
    "_lib_confd.py": _LIB, "_lib_constants.py": _LIB, "_lib_io.py": _LIB,
    "_lib_key_verdict.py": _LIB, "_lib_parse_cache.py": _LIB, "_lib_parse_tree.py": _LIB,
    "_lib_python.py": _LIB, "_lib_tenant_uniqueness.py": _LIB, "_lib_tenant_values.py": _LIB,
    "_lib_yaml_keys.py": _LIB, "_lib_write_check.py": _LIB, "dx/custom_alerts/__init__.py": _LIB,
    "dx/custom_alerts/loader.py": _LIB, "dx/custom_alerts/entries.py": _GO, "lint/_lint_helpers.py": _LIB, "lint/_rule_tree.py": _LIB,
    "ops/_grar_merge.py": _LIB, "ops/_grar_parse.py": _LIB, "ops/_grar_render.py": _LIB,
    "ops/_grar_validate.py": _LIB, "ops/_gsvc_child.py": _LIB, "ops/_gsvc_proto.py": _LIB,
    "ops/_observed_map_lib.py": _LIB, "ops/_registry_lib.py": _LIB, "ops/_render_diff.py": _LIB,
    "ops/generator_service.py": ("the generator container: judges with the generator in-process; "
                                 "its CLI-parity tests (tests/ops/test_generator_service.py) "
                                 "compare it with generate_alertmanager_routes"),
    "ops/guard_dispatch.py": _GO,
    "dx/generate_platform_data.py": _GO, "dx/generate_tenant_metadata.py": _GO,
    "dx/sync_schema.py": _NAMES, "lint/check_cli_contract.py": _NAMES,
    "lint/check_cli_default_drift.py": _NAMES, "lint/check_doc_datools_cmds.py": _NAMES,
    "lint/check_frontmatter_versions.py": _NAMES, "lint/check_structure.py": _NAMES,
    "lint/check_k8s_manifests.py": "reads Kubernetes manifests, not a tenant conf.d",
    "lint/check_md_yaml_drift.py": "reads YAML fences / CRDs quoted in docs, not a tenant conf.d",
    "ops/config_diff.py": ("renders both trees through da-guard and the route generator "
                           "(subprocess): its routing half is the generator's probe"),
    "ops/migration_audit.py": ("reads with both readers on purpose (PyYAML vs parse-tree) "
                               "to list where they differ (ADR-036 §8)"),
    "dx/describe_tenant.py": _GO,
    "dx/generate_tenant_fixture.py": _PENDING, "dx/run_chaos_soak.py": _NAMES,
    "lint/check_account_registry_monotonic.py": _GO_BLOB,
    "validate_all.py": _PROSE_ONLY + ("; runs other tools as subprocesses — the "
                                       "conf.d readers among them have their own rows"),
    "ops/byo_check.py": _PROSE_ONLY + "; reads Alertmanager's /api/v2/status config",
    "ops/blast_radius.py": _GO_VIA_DESCRIBE,
    "ops/generate_rule_pack_split.py": _PROSE_ONLY + "; reads rule packs",
    "ops/cutover_tenant.py": _PROSE_ONLY + "; reads a readiness JSON and drives kubectl",
    "ops/baseline_discovery.py": _PROSE_ONLY + "; reads Prometheus",
    "ops/blind_spot_discovery.py": _GO,
    "dx/compile_custom_alerts.py": _GO,
    "dx/tenant_verify.py": _GO, 
    "lint/check_retire_drift.py": _GO_KEYS,
    "lint/check_scrape_reachability.py": (_PROSE_ONLY + "; reads Kubernetes manifests and the "
                                          "Prometheus ConfigMap"),
    "lint/check_threshold_observed_map.py": (_PROSE_ONLY + "; reads rule packs and "
                                             "metric_observed_map.yaml"),
    "lint/check_threshold_reachability.py": _GO_FIXED,
    "lint/check_threshold_registry.py": (
        _GO_WRITER + "; --regen writes the two fixed conf.d `_defaults.yaml`, its check "
        "mode reads every surface as bytes (a text compare), no value"),
    "lint/check_threshold_unit_sanity.py": _GO_FIXED,
    "ops/analyze_rule_pack_gaps.py": _OWNER_2115, "ops/assemble_config_dir.py": _GO,
    "ops/backtest_threshold.py": _GO, "ops/batch_diagnose.py": _NAMES,
    "ops/config_history.py": _NAMES, "ops/da_assembler.py": _GO_WRITER,
    "ops/deprecate_rule.py": _GO_WRITER, "ops/diagnose.py": _GO,
    "ops/drift_detect.py": _NAMES, "ops/explain_keys.py": _GO,
    "ops/gitops_check.py": _GO_KEYS,
    "ops/init_project.py": _GO_WRITER, "ops/migrate_rule.py": _PENDING,
    "ops/migrate_to_operator.py": _GO_KEYS,
    "ops/onboard_platform.py": _PENDING, "ops/operator_generate.py": _GO_KEYS,
    "ops/patch_config.py": _GO_WRITER,
    "ops/scaffold_tenant.py": _GO_WRITER, "ops/threshold_govern.py": _GO_WRITER,
}


def _population() -> set[str]:
    return {p.relative_to(TOOLS).as_posix() for p in TOOLS.rglob("*.py") if population_keys(p)}


def test_every_reader_is_probed_or_says_why_not():
    pop = _population()
    probed, excused = set(_PROBES), set(_NOT_PROBED)
    assert not probed & excused, f"in both tables: {sorted(probed & excused)}"
    listed = probed | excused
    assert pop == listed, (
        f"conf.d readers with no row (probe it, or say why not): {sorted(pop - listed)}\n"
        f"rows for files that are not conf.d readers (delete them): {sorted(listed - pop)}")


@functools.lru_cache(maxsize=None)
def _outcome(tool: str, variant: str) -> tuple:
    script, argv, tree, outputs, _pending = _PROBES[tool]
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "conf.d")
        out = os.path.join(tmp, "out")
        os.makedirs(d)
        os.makedirs(out)
        for name, text in tree.items():
            pathlib.Path(d, name).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(d, name).write_text(text.replace("{VALUE}", _VARIANTS[variant]),
                                             encoding="utf-8")
        # The tool runs in `tmp`: a relative $DA_GUARD_BINARY is made absolute
        # against this process's directory first (#2906 review).
        env = dict(os.environ)
        if env.get("DA_GUARD_BINARY", "").strip():
            env["DA_GUARD_BINARY"] = os.path.abspath(env["DA_GUARD_BINARY"])
        r = subprocess.run(
            [sys.executable, str(TOOLS / script)] + [a.format(d=d, out=out) for a in argv],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=tmp, env=env, timeout=180)
        files = tuple(pathlib.Path(out, f).read_text(encoding="utf-8", errors="replace")
                      if pathlib.Path(out, f).exists() else None for f in outputs)

    def norm(s: str) -> str:
        return s.replace(tmp, "<TMP>")
    return r.returncode, norm(r.stdout), norm(r.stderr), tuple(
        norm(f) if f is not None else None for f in files)


def _params():
    for tool, spec in sorted(_PROBES.items()):
        marks = [pytest.mark.xfail(strict=True, reason=spec[4])] if spec[4] else []
        yield pytest.param(tool, marks=marks, id=tool)


@pytest.mark.parametrize("tool", list(_params()))
def test_unquoted_yes_reads_as_the_string_yes(tool):
    plain, quoted = _outcome(tool, "plain"), _outcome(tool, "quoted")
    assert plain == quoted, (
        f"{tool}: an unquoted `yes` and a quoted \"yes\" give different output — the "
        f"tool reads the unquoted word as PyYAML's boolean, not as yaml.v3's string.\n"
        f"  yes:   rc {plain[0]}\n{plain[1][-1500:]}{plain[2][-1500:]}\n"
        f"  \"yes\": rc {quoted[0]}\n{quoted[1][-1500:]}{quoted[2][-1500:]}")


@pytest.mark.parametrize("tool", sorted(_PROBES))
def test_the_probe_reads_the_field(tool):
    """Anti-vacuity: `true` must come out differently from `"yes"`."""
    assert _outcome(tool, "bool") != _outcome(tool, "quoted"), (
        f"{tool}: `true` and \"yes\" give the same output — the probe's field is not one "
        f"this tool reads, so the yes / \"yes\" row proves nothing; pick another field")
