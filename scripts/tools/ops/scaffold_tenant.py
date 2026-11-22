#!/usr/bin/env python3
"""
scaffold_tenant.py — Interactive tenant config generator for Dynamic Alerting.

Generates:
  1. <tenant>.yaml         — Tenant threshold overrides (conf.d/ format)
  2. _defaults.yaml        — Platform defaults (optional, if starting fresh)
  3. scaffold-report.txt   — Summary with rule pack & Helm deployment instructions

Usage:
  python3 scripts/tools/ops/scaffold_tenant.py
  python3 scripts/tools/ops/scaffold_tenant.py --tenant db-c --db mariadb,redis -o output/
  python3 scripts/tools/ops/scaffold_tenant.py --non-interactive --tenant db-c --db mariadb
  python3 scripts/tools/ops/scaffold_tenant.py --tenant db-c --db mariadb --namespaces ns1,ns2,ns3
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

_THIS_DIR = Path(__file__).resolve().parent

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, str(_THIS_DIR))  # Docker flat layout
sys.path.insert(0, str(_THIS_DIR.parent))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    detect_cli_lang, ensure_dir_or_die, output_dir_type, write_operator_output_or_die,
    DOCS_INSTALL_URL, DOCS_SITE_BASE,
)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
import _lib_input  # noqa: E402  (#3445: --from-onboard is read by the lib)
from _lib_prompt import prompt_line  # noqa: E402  (#3822: an unreadable stdin is rc 2, named)
from _lib_validation import is_valid_tenant_id, tenant_id_rule  # noqa: E402  (ADR-035)
import _lib_confedit  # noqa: E402  (ADR-036 §2: the conf.d files go to da-guard write)
import _lib_write_check  # noqa: E402  (the draft conf.d's name and layout check, #4014)

# #1641: every path this tool writes descends from -o/--output-dir, so every
# writer names that flag; an unusable path is rc=2 + one line, not a
# traceback at rc=1 (which reads as "invalid input").
_OUTPUT_FLAG = "-o/--output-dir"
from _registry_lib import (  # noqa: E402
    counterexample_sentence,
    shipped_dimensions_for_packs,
)
from _registry_lib import counterexample_for_key as _registry_counterexample_for_key  # noqa: E402

# Language detection for bilingual help
_LANG = detect_cli_lang()


# #3822: what an interactive run that cannot read stdin is told to do instead.
_NO_PROMPT_REMEDY = {
    'zh': '不經提示執行：--non-interactive --tenant <name> --db <type>[,<type>...]'
          '，或 --from-onboard <file>。',
    'en': 'To run without prompts: --non-interactive --tenant <name> '
          '--db <type>[,<type>...], or --from-onboard <file>.',
}


def _ask(prompt: str) -> str:
    """One interactive answer (#3822): `prompt_line`, naming --non-interactive."""
    lang = 'zh' if _LANG == 'zh' else 'en'
    return prompt_line(prompt, remedy=_NO_PROMPT_REMEDY[lang], lang=lang)

#: `--silent-mode` choices: the silencing targets plus the documented off word.
#: The exporter also takes the other off aliases (disabled / off / false);
#: tests/ops/test_scaffold_tenant.py keeps these within the tenant schema's
#: silentMode enum, which is pinned to the exporter's words (#3227).
SILENT_MODE_CHOICES = ("warning", "critical", "all", "disable")
#: The interactive menu's silent-mode picks (menu number -> target); each is
#: written as the scalar value or as the structured form's `target`.
INTERACTIVE_SILENT_TARGETS = {"2": "warning", "3": "critical", "4": "all"}

# Bilingual help strings
_HELP = {
    'description': {
        'zh': '動態告警互動式租戶配置生成器',
        'en': 'Interactive tenant config generator for Dynamic Alerting'
    },
    'tenant': {
        'zh': '租戶命名空間名稱 (例如 db-c)',
        'en': 'Tenant namespace name (e.g., db-c)'
    },
    'db': {
        'zh': '逗號分隔的資料庫類型 (mariadb,redis,mongodb,elasticsearch)',
        'en': 'Comma-separated DB types (mariadb,redis,mongodb,elasticsearch)'
    },
    'output_dir': {
        'zh': '輸出目錄 (預設: scaffold_output)',
        'en': 'Output directory (default: scaffold_output)'
    },
    'catalog': {
        'zh': '顯示支援的 exporter 清單',
        'en': 'Display list of supported exporters'
    },
    'non_interactive': {
        'zh': '跳過互動提示 (需要 --tenant 和 --db)',
        'en': 'Skip interactive prompts (requires --tenant and --db)'
    },
    'namespaces': {
        'zh': '逗號分隔的 K8s 命名空間名稱 (用於 N:1 租戶映射，例如 ns1,ns2,ns3)',
        'en': 'Comma-separated K8s namespace names for N:1 tenant mapping (e.g., ns1,ns2,ns3)'
    },
    'silent_mode': {
        'zh': '靜默模式: 告警觸發 (TSDB 記錄) 但通知被抑制',
        'en': 'Silent mode: alerts fire (TSDB records) but notifications suppressed'
    },
    'severity_dedup': {
        'zh': '嚴重度去重: 在 critical 觸發時抑制 warning 通知 (預設: enable)',
        'en': 'Severity dedup: suppress warning notification when critical fires (default: enable)'
    },
    'routing_receiver': {
        'zh': '告警路由接收器地址 (URL 或電子郵件清單，取決於類型)',
        'en': 'Alert routing receiver address (URL or email list depending on type)'
    },
    'routing_receiver_type': {
        'zh': '接收器類型 (預設: webhook)',
        'en': 'Receiver type (default: webhook)'
    },
    'routing_smarthost': {
        'zh': '電子郵件接收器的 SMTP smarthost (例如 smtp.example.com:587)',
        'en': 'SMTP smarthost for email receiver (e.g., smtp.example.com:587)'
    },
    'routing_group_by': {
        'zh': '逗號分隔的分組標籤 (例如 alertname,severity)',
        'en': 'Comma-separated group_by labels (e.g., alertname,severity)'
    },
    'routing_group_wait': {
        'zh': '分組等待持續時間 (例如 30s，範圍: 5s-5m)',
        'en': 'Group wait duration (e.g., 30s, range: 5s-5m)'
    },
    'routing_group_interval': {
        'zh': '分組間隔持續時間 (例如 5m，範圍: 5s-5m)',
        'en': 'Group interval duration (e.g., 5m, range: 5s-5m)'
    },
    'routing_repeat_interval': {
        'zh': '重複間隔持續時間 (例如 4h，範圍: 1m-72h)',
        'en': 'Repeat interval duration (e.g., 4h, range: 1m-72h)'
    },
    'profile': {
        'zh': '租戶設定檔名稱 (例如 standard-mariadb-prod)。設定時，租戶 YAML 將參考此設定檔，並僅包含明確覆寫',
        'en': 'Tenant profile name (e.g., standard-mariadb-prod). When set, tenant YAML will reference the named profile and only contain explicit overrides.'
    },
    'routing_profile': {
        'zh': '路由設定檔名稱 (ADR-007)，例如 team-sre-apac。設定時，租戶 YAML 將包含 _routing_profile 引用',
        'en': 'Routing profile name (ADR-007), e.g., team-sre-apac. When set, tenant YAML will include _routing_profile reference'
    },
    'topology': {
        'zh': '租戶映射拓撲 (ADR-006): 1:1 (預設), N:1 (需要 --namespaces), 1:N (產生 mapping hint)',
        'en': 'Tenant mapping topology (ADR-006): 1:1 (default), N:1 (requires --namespaces), 1:N (generates mapping hint)'
    },
    'mapping_instance': {
        'zh': '1:N 拓撲的來源實例名稱 (例如 oracle-prod-01)',
        'en': 'Source instance name for 1:N topology (e.g., oracle-prod-01)'
    },
    'mapping_filter': {
        'zh': '1:N 拓撲的 PromQL filter (例如 schema=~"app_a_.*")',
        'en': 'PromQL filter for 1:N topology (e.g., schema=~"app_a_.*")'
    },
    'from_onboard': {
        'zh': 'onboard-hints.json 的路徑 (自動從上船結果生成)',
        'en': 'Path to onboard-hints.json (auto-scaffold from onboard results)'
    },
    'epilog': {
        'zh': '''範例:
  %(prog)s                                                      # 互動模式
  %(prog)s --catalog                                            # 顯示支援的 exporter 清單
  %(prog)s --tenant db-c --db mariadb,redis                     # 非互動模式
  %(prog)s --tenant db-c --db mariadb -o out/                   # 指定輸出目錄
  %(prog)s --tenant db-c --db mariadb --namespaces ns1,ns2,ns3  # 含 N:1 租戶映射''',
        'en': '''Examples:
  %(prog)s                                                      # Interactive mode
  %(prog)s --catalog                                            # Display supported exporters
  %(prog)s --tenant db-c --db mariadb,redis                     # Non-interactive mode
  %(prog)s --tenant db-c --db mariadb -o out/                   # Specify output directory
  %(prog)s --tenant db-c --db mariadb --namespaces ns1,ns2,ns3  # With N:1 tenant mapping'''
    }
}

def _h(key: str) -> str:
    """Get help text in detected language."""
    return _HELP[key].get(_LANG, _HELP[key]['en'])

# ============================================================
# Rule Pack catalog — metric keys, defaults, descriptions
# ============================================================
from _rule_packs_generated import THRESHOLD_PACKS as _THRESHOLD_PACKS  # noqa: E402
from _rule_packs_generated import BUILTIN_DEFAULT_KEYS as _BUILTIN_DEFAULT_KEYS  # noqa: E402

# Scaffold-only per-pack data: what is not threshold identity (state-filter
# definitions, a dimensional example for the generated tenant YAML). The
# threshold half lives in rule-packs/threshold-registry.yaml, the SoT since
# D-A PR-3, and reaches this module generated (`_rule_packs_generated`).
_SCAFFOLD_PACK_EXTRAS = {'kubernetes': {'state_filters': {'container_crashloop': {'reasons': ['CrashLoopBackOff'],
                                                          'severity': 'critical',
                                                          'desc': 'Detect CrashLoopBackOff '
                                                                  'containers'},
                                  'container_imagepull': {'reasons': ['ImagePullBackOff',
                                                                      'InvalidImageName'],
                                                          'severity': 'warning',
                                                          'desc': 'Detect ImagePullBackOff '
                                                                  'containers'},
                                  'maintenance': {'reasons': [],
                                                  'severity': 'info',
                                                  'default_state': 'disable',
                                                  'desc': 'Maintenance mode (suppresses all '
                                                          'alerts)'},
                                  'unset_thresholds': {'reasons': [],
                                                       'severity': 'info',
                                                       'default_state': 'disable',
                                                       'desc': 'Name the threshold keys that serve '
                                                               'no threshold (#1200 D-A A3)'}}},
 'postgresql': {'dimensional_example': {'pg_connections{datname="orders"}': '70'}},
 'redis': {'dimensional_example': {'redis_connected_clients{instance="redis-orders:6379"}': '500',
                                   'redis_evicted_keys_rate{instance="redis-cache:6379"}': '500'}},
 'mongodb': {'dimensional_example': {'mongodb_replication_lag{rs="rs-orders"}': '30'}},
 'elasticsearch': {'dimensional_example': {'es_search_latency_ms{index="logs-prod"}': '2000'}},
 'oracle': {'dimensional_example': {'oracle_tablespace_used_percent{tablespace=~"USERS|DATA.*"}': '90',
                                    'oracle_tablespace_used_percent{tablespace="SYSTEM"}': '95:critical'}},
 'db2': {'dimensional_example': {'db2_tablespace_used_percent{tablespace=~"USERSPACE.*"}': '80'}},
 'clickhouse': {'dimensional_example': {'clickhouse_max_part_count{database=~"prod_.*"}': '200'}},
 'kafka': {'dimensional_example': {'kafka_consumer_lag{group="order-processing"}': '5000',
                                   'kafka_consumer_lag{group="analytics"}': '50000'}},
 'rabbitmq': {'dimensional_example': {'rabbitmq_queue_messages{queue="payment-events"}': '50000',
                                      'rabbitmq_queue_messages{queue="bulk-processing"}': '500000'}}}

# RULE_PACKS: the registry's threshold half (display / exporter / default_on /
# rule_pack_file / defaults / optional_overrides) plus the extras above. Same
# shape and API as the hand-written dict it replaces; edit thresholds in the
# registry, never here.
RULE_PACKS = {
    _name: {**_pack, **_SCAFFOLD_PACK_EXTRAS.get(_name, {})}
    for _name, _pack in _THRESHOLD_PACKS.items()
}
_unknown_extras = set(_SCAFFOLD_PACK_EXTRAS) - set(RULE_PACKS)
if _unknown_extras:  # an extras entry for a pack the registry does not have
    raise ImportError(
        f"_SCAFFOLD_PACK_EXTRAS names packs not in the threshold registry: "
        f"{sorted(_unknown_extras)}")

# 飽和類指標（metric_class == "saturation"）的 _critical 教育註解——純顯示、
# 不擋不驗。判準：有界資源用量 / backlog（容量行動）＋存在配對症狀指標。
# 詳見 docs/alerting-design-fundamentals.md〈告警該響之前〉。
SATURATION_CRITICAL_COMMENT = "# 飽和類指標的 critical 層：飽和=容量訊號，建議先確認配對症狀告警（如慢查詢），或 warning＋容量規劃"

_SATURATION_CRITICAL_RE = re.compile(r"([A-Za-z0-9_]+)_critical")


def saturation_default_keys() -> set[str]:
    """回傳 RULE_PACKS defaults 中標記為 saturation 的 base key set。"""
    return {
        key
        for pack in RULE_PACKS.values()
        for key, info in pack.get("defaults", {}).items()
        if info.get("metric_class") == "saturation"
    }


def saturation_critical_head(at: tuple, value: object) -> str | None:
    """飽和類 `<base>_critical` 鍵上方的教育註解：`_lib_confedit.data_value` 的
    *head*（鍵路徑, 值）→ 註解文字，其他鍵 None。

    - base key 以 RULE_PACKS defaults 的 ``metric_class == "saturation"`` set 判定
    - 任何深度的 mapping 鍵都算（租戶檔、`_profiles.yaml`）；序列項目不算
    - 註解由 da-guard 寫在該鍵上方、同縮排（ADR-036 §2：寫 conf.d 的只有 Go）
    """
    if not at or at[-1].startswith("["):
        return None
    m = _SATURATION_CRITICAL_RE.fullmatch(at[-1])
    if m and m.group(1) in saturation_default_keys():
        return SATURATION_CRITICAL_COMMENT[len("# "):]
    return None


def prompt_choice(question: str, options: list[tuple[str, str]], default: str | None = None) -> str:
    """Interactive single-choice prompt."""
    print(f"\n{question}")
    for i, (key, label) in enumerate(options, 1):
        marker = " (default)" if key == default else ""
        print(f"  {i}. {label}{marker}")
    while True:
        raw = _ask(f"選擇 [1-{len(options)}]: ").strip()
        if not raw and default:
            return default
        try:
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        except ValueError:
            pass
        print("  無效選擇，請重試。")


def prompt_multi(question: str, options: list[tuple[str, str]]) -> list[str]:
    """Interactive multi-choice prompt. Returns list of selected keys."""
    print(f"\n{question}")
    for i, (key, label) in enumerate(options, 1):
        print(f"  {i}. {label}")
    print(f"  輸入數字 (逗號分隔，例如 1,3)，或 'all' 全選:")
    while True:
        raw = _ask("選擇: ").strip()
        if raw.lower() == "all":
            return [k for k, _ in options]
        try:
            indices = [int(x.strip()) for x in raw.split(",")]
            if all(1 <= i <= len(options) for i in indices):
                return [options[i - 1][0] for i in indices]
        except ValueError:
            pass
        print("  無效選擇，請重試。")


def _counterexample_sentence(ce: dict) -> str:
    """The prompt framing of a measured counter-example.

    ⛔ The FACTS come from one place — `_registry_lib.counterexample_sentence`,
    the same composer the two `_defaults.yaml` writers use, over the same
    direction table the rule-pack headers and the portal render from. This
    module used to keep its own copy of the direction→wording map while its
    docstring claimed the clause was "shared by every surface"; the claim was
    false and the copy was one edit away from saying the opposite of the header
    next to it (blind review, #1344).
    """
    return counterexample_sentence(ce)


def counterexample_prompt_lines(info: dict[str, object]) -> list[str]:
    """The ⚠ line shown above a prompt whose value has a measured counter-example.

    Derived from the registry field (`value_counterexample`), never authored at
    the prompt — the rule-pack headers render the same data from the same
    source, so the two cannot disagree. Returns [] when nothing was measured,
    which reads as "no counter-example", never as "validated".
    """
    ce = info.get("value_counterexample")
    if not ce:
        return []
    return [f"  ⚠ {_counterexample_sentence(ce)}"]


def counterexample_for_key(key: str, rule_packs: dict | None = None) -> dict | None:
    """The measured counter-example for `key`, or None. Looks across both tiers.

    Re-exported from `_registry_lib`, which owns it because `init_project`
    writes the same `_defaults.yaml` and needs the same annotation. Kept
    spelled here so existing callers and tests import one obvious name.
    """
    return _registry_counterexample_for_key(
        key, RULE_PACKS if rule_packs is None else rule_packs)


def prompt_value(metric: str, info: dict[str, object], current: str | None = None,
                 *, platform_value: bool = False) -> str | None:
    """Prompt for a threshold value. Returns string or None to skip.

    #3924: pressing Enter writes nothing — the registry number is never copied
    into the tenant's file by default. ``platform_value`` marks a key the
    readers build in with the platform's number (``arming: default``):
    omitting it serves that number and follows the platform's later
    calibration, so Enter keeps it. Every other key (``arming: operator``, and
    every ``<base>_critical``) has no platform value: the registry number is
    SHOWN as a reference — the only starting point an operator has — and Enter
    skips the key, which serves nothing for it.

    A tenant's OWN prior value (``current``) is still the Enter-default: that
    one is not a platform assertion.

    ⛔ Any measured counter-example is printed FIRST, for both kinds of key.
    The text is derived from ``value_counterexample`` rather than restated
    here, so this docstring cannot drift away from the registry.
    """
    for line in counterexample_prompt_lines(info):
        print(line)
    if current:
        prompt = f"  {metric} [{info['desc']}] ({current} {info['unit']}): "
    elif platform_value:
        prompt = (f"  {metric} [{info['desc']}] (平台值 {info['value']} "
                  f"{info['unit']}；留空沿用平台值): ")
    else:
        prompt = (f"  {metric} [{info['desc']}] (無平台值，參考值 "
                  f"{info['value']} {info['unit']}；須依自身 baseline 校準，"
                  "留空跳過): ")
    raw = _ask(prompt).strip()
    if not raw:
        return current or None
    if raw.lower() in ("skip", "disable"):
        return raw if raw.lower() == "disable" else None
    return raw


def generate_defaults(selected_dbs: list[str]) -> dict:
    """Generate _defaults.yaml content.

    #3924: no threshold key of the registry is written, neither as a value
    under ``defaults:`` nor as a name under ``optional_overrides:``. Every
    registry key is in the readers' built-in layer (#3832, #4163): one the
    platform ships armed is valued with the platform's number, and every other
    one is valued off, so a tenant, subtree or profile that gives it a value is
    served without the root declaring it. A copy here would pin the day's
    number (#3832 decision 7) or arm, for every tenant, a value only the
    deployment can calibrate. Which keys of the selected packs have no platform
    value, and how many tenants set them: ``da-tools validate-config``
    (platform_defaults).

    Sections:

      * ``dimensions``     — the labels a dimensional key may carry and still
        be served (#3475); this file is the only declaration a GitOps
        deployment has.
      * ``state_filters``  — kubernetes state monitoring.
    """
    k8s = RULE_PACKS["kubernetes"]
    state_filters = {
        k: {kk: vv for kk, vv in v.items() if kk != "desc"}
        for k, v in k8s["state_filters"].items()
    }
    out: dict = {}
    dimensions = shipped_dimensions_for_packs(["kubernetes", *selected_dbs], RULE_PACKS)
    if dimensions:
        out["dimensions"] = dimensions
    out["state_filters"] = state_filters
    return out


def prompt_tenant_overrides(selected_dbs: list[str]) -> dict:
    """互動式收集 tenant 覆寫（原 generate_tenant 內的 _ask()/print() 邏輯）。

    逐項詢問並收集：metric 閾值覆寫 → 維護模式 → 靜音模式 → 嚴重度去重 →
    告警路由。prompt 提示字串、預設值與詢問次序與抽出前逐字相同（互動 UX 為契約），
    **例外**是閾值：#3924 起按 Enter 不寫任何值——平台給值的鍵沿用平台值，
    其餘的鍵跳過（見 ``prompt_value``）。
    僅在 ``generate_tenant(interactive=True)`` 路徑被呼叫。

    Args:
        selected_dbs: DB 型別 key 清單（決定 metric 覆寫 prompt 的數量與順序）。

    Returns:
        dict — 收集到的覆寫（metric key → 值字串，外加視答案而定的
        ``_state_maintenance`` / ``_silent_mode`` / ``_severity_dedup`` /
        ``_routing``）。未觸發任何覆寫時回傳空 dict。
    """
    tenant_config = {}

    for db in selected_dbs:
        pack = RULE_PACKS.get(db)
        if not pack:
            continue

        # Every threshold key of the pack, each asked once (#3924: no key is
        # declared in `_defaults.yaml` any more — every one is built in, so a
        # `<base>_critical` always has its base and is asked too).
        for tier in ("defaults", "optional_overrides"):
            for key, info in pack.get(tier, {}).items():
                val = prompt_value(key, info,
                                   platform_value=key in _BUILTIN_DEFAULT_KEYS)
                if val and val != "skip":
                    tenant_config[key] = val

    # Always add maintenance state control (disabled by default)
    # v1.7.0: supports structured format with expires for auto-deactivation
    enable_maint = _ask("\n  啟用維護模式? (y/N): ").strip().lower()
    if enable_maint == "y":
        expires_str = _ask("  設定到期時間 (ISO 8601, 如 2026-04-01T00:00:00Z, 空白=無期限): ").strip()
        if expires_str:
            reason_str = _ask("  原因 (選填): ").strip()
            maint_obj = {"target": "enable", "expires": expires_str}
            if reason_str:
                maint_obj["reason"] = reason_str
            tenant_config["_state_maintenance"] = maint_obj
        else:
            tenant_config["_state_maintenance"] = "enable"

    # Silent mode: alerts fire (TSDB records) but notifications suppressed
    # v1.7.0: supports structured format with expires for auto-deactivation
    print("\n  靜音模式 (Silent Mode):")
    print("    1. Normal — 不靜音 (預設)")
    print("    2. Warning — 只靜音 warning 通知")
    print("    3. Critical — 只靜音 critical 通知")
    print("    4. All — 靜音所有通知")
    silent_choice = _ask("  選擇 [1-4] (預設 1): ").strip()
    silent_map = INTERACTIVE_SILENT_TARGETS
    if silent_choice in silent_map:
        expires_str = _ask("  設定到期時間 (ISO 8601, 如 2026-04-01T00:00:00Z, 空白=無期限): ").strip()
        if expires_str:
            reason_str = _ask("  原因 (選填): ").strip()
            silent_obj = {"target": silent_map[silent_choice], "expires": expires_str}
            if reason_str:
                silent_obj["reason"] = reason_str
            tenant_config["_silent_mode"] = silent_obj
        else:
            tenant_config["_silent_mode"] = silent_map[silent_choice]

    # Severity dedup: control warning↔critical notification deduplication
    print("\n  嚴重度去重 (Severity Dedup):")
    print("    1. Enable — critical 觸發時壓制 warning 通知 (預設)")
    print("    2. Disable — warning 和 critical 通知都發送")
    dedup_choice = _ask("  選擇 [1-2] (預設 1): ").strip()
    if dedup_choice == "2":
        tenant_config["_severity_dedup"] = "disable"

    # Alert routing: tenant-managed notification destination
    print("\n  告警路由 (Alert Routing):")
    print("    設定通知目的地，空白跳過使用平台預設")
    print("    支援類型: webhook | email | slack | teams | rocketchat | pagerduty")
    receiver_type = _ask("  Receiver type (預設 webhook): ").strip().lower() or "webhook"
    receiver_obj = None
    if receiver_type == "webhook":
        url = _ask("  Webhook URL: ").strip()
        if url:
            receiver_obj = {"type": "webhook", "url": url}
    elif receiver_type == "email":
        to = _ask("  Email to (逗號分隔): ").strip()
        smarthost = _ask("  SMTP smarthost (例如 smtp.example.com:587): ").strip()
        if to and smarthost:
            receiver_obj = {"type": "email", "to": [t.strip() for t in to.split(",")],
                            "smarthost": smarthost}
    elif receiver_type == "slack":
        api_url = _ask("  Slack API URL: ").strip()
        if api_url:
            receiver_obj = {"type": "slack", "api_url": api_url}
            channel = _ask("  Channel (例如 #alerts，選填): ").strip()
            if channel:
                receiver_obj["channel"] = channel
    elif receiver_type == "teams":
        webhook_url = _ask("  Teams Webhook URL: ").strip()
        if webhook_url:
            receiver_obj = {"type": "teams", "webhook_url": webhook_url}
    elif receiver_type == "rocketchat":
        url = _ask("  Rocket.Chat Webhook URL: ").strip()
        if url:
            receiver_obj = {"type": "rocketchat", "url": url}
    elif receiver_type == "pagerduty":
        service_key = _ask("  PagerDuty Service Key: ").strip()
        if service_key:
            receiver_obj = {"type": "pagerduty", "service_key": service_key}
    else:
        print(f"  WARN: unknown type '{receiver_type}', skipping routing")

    if receiver_obj:
        routing = {"receiver": receiver_obj}

        group_by = _ask("  Group by labels (逗號分隔，預設 alertname,tenant): ").strip()
        routing["group_by"] = ([g.strip() for g in group_by.split(",")]
                               if group_by else ["alertname", "tenant"])
        routing["group_wait"] = _ask("  Group wait (預設 30s，範圍 5s-5m): ").strip() or "30s"
        routing["group_interval"] = _ask("  Group interval (預設 5m，範圍 5s-5m): ").strip() or "5m"
        routing["repeat_interval"] = _ask("  Repeat interval (預設 4h，範圍 1m-72h): ").strip() or "4h"

        tenant_config["_routing"] = routing

    return tenant_config


def generate_tenant(tenant_name: str, selected_dbs: list[str], interactive: bool = False) -> dict:
    """Generate tenant YAML content.

    純組裝：``interactive=True`` 時委派 ``prompt_tenant_overrides`` 逐項收集覆寫，
    否則為空覆寫（non-interactive 一律繼承 _defaults.yaml，不寫 metric 覆寫）。
    本函式本身不再直接呼叫 _ask()/print()——互動 I/O 全在 prompt_tenant_overrides。
    """
    tenant_config = prompt_tenant_overrides(selected_dbs) if interactive else {}
    return {"tenants": {tenant_name: tenant_config}} if tenant_config else {"tenants": {tenant_name: {}}}


def build_receiver_from_args(receiver_type: str, receiver_value: str, smarthost: str | None = None) -> dict:
    """從 CLI 參數建立 receiver dict。

    將 receiver_type + receiver_value 組合為結構化的 receiver 物件，
    供 generate_alertmanager_routes.py 消費。

    Args:
        receiver_type: 接收器類型（webhook, email, slack, teams, rocketchat, pagerduty）。
        receiver_value: 接收器地址（URL、email 清單、service key 等）。
        smarthost: 僅 email 類型需要的 SMTP smarthost。

    Returns:
        dict — 結構化 receiver 物件，如 ``{"type": "webhook", "url": "..."}}``。
    """
    receiver_obj = {"type": receiver_type}
    if receiver_type == "webhook":
        receiver_obj["url"] = receiver_value
    elif receiver_type == "email":
        receiver_obj["to"] = [t.strip() for t in receiver_value.split(",")]
        receiver_obj["smarthost"] = smarthost or "localhost:25"
    elif receiver_type == "slack":
        receiver_obj["api_url"] = receiver_value
    elif receiver_type == "teams":
        receiver_obj["webhook_url"] = receiver_value
    elif receiver_type == "rocketchat":
        receiver_obj["url"] = receiver_value
    elif receiver_type == "pagerduty":
        receiver_obj["service_key"] = receiver_value
    return receiver_obj


def generate_report(tenant_name: str, selected_dbs: list[str], output_dir: str, namespaces: str | None = None) -> str:
    """Generate scaffold report with deployment instructions."""
    lines = [
        f"# Scaffold Report — {tenant_name}",
        f"# Generated by scaffold_tenant.py",
        "",
        "## 生成檔案",
        f"  - {output_dir}/{CONFD_DIR}/{tenant_name}.yaml (tenant 閾值設定)",
        f"  - {output_dir}/{CONFD_DIR}/_defaults.yaml (平台預設值)",
    ]

    if namespaces:
        lines.append(f"  - {output_dir}/{relabel_snippet_name(tenant_name)} (Prometheus relabel snippet)")

    lines.extend([
        "",
        "## Rule Packs (已預載於平台)",
        "  所有核心 Rule Packs (包含自我監控) 已透過 Projected Volume 預載於平台中。",
        "  未部署 exporter 的 pack 不會產生 metrics，alert 不會誤觸發。",
        "",
    ])

    for db in selected_dbs:
        pack = RULE_PACKS.get(db)
        if pack:
            lines.append(f"  ✅ {pack['display']} — 已預載")

    # 飽和類指標教育段 — 僅當所選 pack 含 metric_class == saturation 的鍵時
    saturation_keys = sorted({
        key
        for db in selected_dbs
        for key, info in (RULE_PACKS.get(db) or {}).get("defaults", {}).items()
        if info.get("metric_class") == "saturation"
    })
    if saturation_keys:
        lines.extend([
            "",
            "## 飽和類指標的 critical 層（教育提示）",
            "  飽和=容量訊號，建議先確認配對症狀告警（如慢查詢），或 warning＋容量規劃。",
            f"  詳見〈告警該響之前〉：{DOCS_SITE_BASE}"
            "alerting-design-fundamentals/",
            "  本租戶可用的飽和類 _critical 鍵：",
        ])
        lines.extend(f"    - {key}_critical" for key in saturation_keys)

    # Helm deployment command (no rule pack overlays needed)
    lines.extend(["", "## 部署指令", ""])

    lines.append("```bash")
    lines.append("# 部署/更新 threshold-exporter (Rule Packs 已內建，無需額外 -f)")
    lines.append("# ⛔ 下面兩個路徑指的是本專案的原始碼樹，不是你的 repo。"
                 "從 OCI registry 安裝的寫法見")
    lines.append(f"#    {DOCS_SITE_BASE}scenarios/gitops-ci-integration/")
    lines.append("helm upgrade --install threshold-exporter ./helm/threshold-exporter \\")
    lines.append("  -n monitoring \\")
    lines.append("  -f environments/local/threshold-exporter.yaml")
    lines.append("```")

    # Prometheus relabel_configs (if namespaces provided)
    if namespaces:
        lines.extend([
            "",
            "## Prometheus N:1 Tenant Mapping",
            "",
            f"Relabel configs have been generated for namespace mapping: {namespaces}",
            "",
            "```bash",
            "# Add to your Prometheus scrape_configs[] section:",
            f"cat {output_dir}/{relabel_snippet_name(tenant_name)}",
            "```",
            "",
            "Then apply to scrape_configs in your Prometheus configuration.",
        ])

    # ConfigMap patching
    lines.extend([
        "",
        "## 掛載 Tenant Config",
        "",
        "```bash",
        "# 方法 1: 複製到你的 conf.d/（GitOps 流程走這條）",
        f"cp {output_dir}/{CONFD_DIR}/{tenant_name}.yaml conf.d/",
        "",
        "# 方法 2: 直接改叢集裡的 ConfigMap（見下方 ⛔ 的先決條件）",
        f"da-tools patch-config {tenant_name} <metric_key> <value>",
        "```",
        "",
        "## 驗證",
        "",
        "```bash",
        "# 純讀設定，不需要叢集：",
        f"da-tools diagnose {tenant_name} --config-dir conf.d/",
        "",
        "# 下面要連得到叢集／Prometheus（裸 diagnose 會去讀 ConfigMap）：",
        f"da-tools diagnose {tenant_name}",
    ])

    # `check-alert` needs an alert name, and the only one this report ever
    # named was MariaDB's — unconditionally, including for tenants that never
    # selected the mariadb pack. Naming an alert the tenant does not have is
    # the same failure mode as naming a file they do not have (#1447), so it
    # is emitted only when that pack is in play.
    if "mariadb" in (selected_dbs or []):
        lines.append(
            f"da-tools check-alert MariaDBHighConnections {tenant_name}")
    lines.extend([
        "```",
        "",
        f"（`da-tools` 的取得方式見 {DOCS_INSTALL_URL}）",
        "⛔ 需要叢集的那幾個要 kubectl 與叢集存取權，而 da-tools 映像本身不含 "
        "kubectl——在映像裡跑會是 `FileNotFoundError: 'kubectl'`。",
    ])

    return "\n".join(lines)


def _die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)


#: #4014: the conf.d files of a run (`<tenant>.yaml`, `_defaults.yaml`,
#: `_instance_mapping.yaml`) go to this tool's draft conf.d under its own
#: dot-directory (`_lib_write_check.DRAFT_ROOTS`, where why it is
#: dot-prefixed and one per tool is said, once): they are a draft to copy
#: into conf.d (the report says so), and `-o` at a served conf.d used to
#: replace its root `_defaults.yaml` whole — every other tenant's keys
#: undeclared — and serve the new tenant.
CONFD_DIR = _lib_write_check.draft_confd_rel("scaffold_tenant")


def _write_confd_files(out: Path, files: list) -> None:
    """Every conf.d file of the run through ONE `da-guard write` batch (ADR-036
    §2: only Go writes conf.d; #3206 step 10c-3) into the draft conf.d under
    *out* (`CONFD_DIR`, #4014): each as typed values (key
    order as generated) with its comments, every file replacing what is at
    its path (as this tool always has: a fresh path is a create). Go writes
    the text and judges the whole tree once and writes all of them or none;
    `_lib_confedit.write_all_or_exit` names every refused file and its
    reason (a write stopped part-way: the files left) and exits 2. A tree
    that is already not servable and a value with no type the contract
    carries end the run with rc 2 before the batch, nothing written."""
    import _lib_parse_tree
    entries = []
    for f in files:
        try:
            entries.append(f.entry(replace=True))
        except ValueError as exc:
            _die(f"{f.rel}: {exc} — nothing was written")
    conf_dir = out / CONFD_DIR
    try:
        # A tree that is already broken is not written into (#3206).
        _lib_write_check.require_output_tree(out)
        # #4014: a draft holding an earlier run's tenant is another tenant
        # in it — refused before anything is written or made.
        refusal = _lib_write_check.refuse_draft_layout(
            out, "scaffold_tenant", [e["path"] for e in entries],
            why="an earlier run's tenant file is another tenant in the draft, #4014")
    except _lib_parse_tree.ParseTreeError as exc:
        _die(f"cannot judge {out} through `da-guard parse-tree` (ADR-036 §2): "
             f"{exc}; nothing was written")
    if refusal:
        _die(f"{refusal} — nothing was written")
    # da-guard's --config-dir: made only when the batch is sent, and removed
    # again (left empty) when it is not written — a failed run leaves no
    # output directory behind (#3206).
    _lib_confedit.write_all_or_exit(
        conf_dir, entries, tool="scaffold_tenant",
        make_root=lambda: ensure_dir_or_die(conf_dir, flag=_OUTPUT_FLAG))


_DEFAULTS_HEADER = (
    "# _defaults.yaml — Platform-managed global settings\n"
    "# Generated by scaffold_tenant.py\n"
    "#\n"
    "# 規則包的閾值都內建在讀取端（exporter、da-guard、tenant-api），所以\n"
    "# defaults: 不列它們：平台出貨即啟用的鍵用平台值，其餘的鍵沒有平台值\n"
    "# （依部署校準），由租戶、子目錄或 profile 給值才送。在 defaults: 的寫法：\n"
    "#   不寫             → 平台值（跟著平台之後的校準），或不送\n"
    "#   <鍵>: <數字>     → 每個租戶都用這個數字（釘住）\n"
    "#   <鍵>: \"disable\"  → 每個租戶都關閉；子目錄、profile 或租戶給了值就開回\n"
    "# 有哪些鍵、平台值與設了值的租戶數：da-tools validate-config\n"
    "# --config-dir <這個目錄> 的 platform_defaults 列。"
)


def defaults_confd_file(defaults_data: dict) -> "_lib_confedit.NewFile":
    """`_defaults.yaml` of a run, with its header (no threshold key is in
    it, #3924, so there is no counter-example to annotate)."""
    return _lib_confedit.NewFile("_defaults.yaml", defaults_data,
                                 _lib_confedit.comment_block(_DEFAULTS_HEADER))


def tenant_confd_file(tenant_name: str, tenant_data: dict) -> "_lib_confedit.NewFile":
    """`<tenant>.yaml`: the header, and the saturation note above each
    saturation `<base>_critical`."""
    try:
        _lib_confedit.comment_field("the tenant id", tenant_name, line=True)
    except ValueError as exc:
        _die(f"{exc} — nothing was written")
    # #3924: one rule for every threshold key — each is built in (#3832,
    # #4163), with the platform's number or off — so the header needs no key
    # list and no per-population sentence (it used to append the declared
    # keys and a `<base>_critical` paragraph read off `_defaults.yaml`).
    head = _lib_confedit.comment_block("\n".join([
        f"# {tenant_name}.yaml — Tenant-managed thresholds",
        "# Generated by scaffold_tenant.py",
        "# 三態: 數值=Custom, 省略=平台值（沒有平台值的鍵省略就是不送）, \"disable\"=停用",
        "# 沒有平台值的鍵（依部署校準）與 <鍵>_critical 都在這裡給值；有哪些、平台值多少：",
        "# da-tools validate-config 的 platform_defaults 列。",
    ]))
    # 飽和類 _critical 鍵上方插入教育註解（純顯示；API 全檔重寫不保留）。
    return _lib_confedit.NewFile(f"{tenant_name}.yaml", tenant_data, head,
                                 node_head=saturation_critical_head)


def _plan_outputs(output_dir: str, tenant_name: str, defaults_data: dict, tenant_data: dict,
                  report: str, relabel_snippet: str | None = None,
                  mapping_hint: dict | None = None, *, with_defaults: bool = True,
                  ) -> tuple[list, list]:
    """`(conf.d files [_lib_confedit.NewFile], every other write [(path,
    text)])` of one tenant's outputs; nothing is written here (nor the
    output directory created: the conf.d batch does that). *with_defaults* False leaves `_defaults.yaml` to
    the caller (`--from-onboard` writes one for all its tenants)."""
    out = Path(output_dir)

    confd = []
    if with_defaults:
        confd.append(defaults_confd_file(defaults_data))
    confd.append(tenant_confd_file(tenant_name, tenant_data))
    # v2.1.0 ADR-006: the _instance_mapping hint (1:N topology)
    if mapping_hint:
        confd.append(_lib_confedit.NewFile("_instance_mapping.yaml", mapping_hint, "\n".join([
            "_instance_mapping.yaml — 1:N Tenant Mapping (ADR-006)",
            "Generated by scaffold_tenant.py --topology=1:N",
            "Merge into your config-dir's _instance_mapping.yaml",
            "Then run: generate_tenant_mapping_rules.py --config-dir <dir>",
        ])))

    # Every other file, in the order it is written (after the conf.d batch).
    writes = []
    # relabel_configs (N:1 topology) — a Prometheus snippet, not a conf.d file.
    # Named so no conf.d reader takes it for one (#3671: a `.yaml` here was a
    # tenant file whose top-level `relabel_configs:` no reader reads).
    if relabel_snippet:
        writes.append((str(out / relabel_snippet_name(tenant_name)), relabel_snippet))
    writes.append((str(out / "scaffold-report.txt"), report))
    return confd, writes


def relabel_snippet_name(tenant_name: str) -> str:
    """The N:1 topology's Prometheus relabel snippet, written beside the
    conf.d files: a name no conf.d reader scans (not a config extension)."""
    return f"relabel_configs-{tenant_name}.txt"


def _refuse_config_named_extras(writes: list) -> None:
    """Every file written beside the conf.d batch must be one no conf.d
    reader reads — da-guard's own name verdict (`config_extension`), the
    rule the exporter's walk applies — or it lands in the tree as a tenant
    file outside the batch's judgement (#3671). Nothing is written."""
    import _lib_parse_tree
    names = [Path(p).name for p, _ in writes]
    try:
        verdict = _lib_parse_tree.classify_names(names)
    except _lib_parse_tree.ParseTreeError as exc:
        _die(f"cannot judge the output names through `da-guard parse-tree`: {exc}; nothing was written")
    read = sorted(n for n, e in verdict.items() if e.config_extension)
    if read:
        _die(f"{', '.join(read)} would be read as conf.d config but is not written through "
             "`da-guard write` (#3671) — nothing was written")


def _perform_writes(out: Path, confd: list, writes: list) -> None:
    """The conf.d batch (all or nothing), then every other file."""
    _refuse_config_named_extras(writes)
    _write_confd_files(out, confd)
    for f in confd:
        print(f"  📄 {out / CONFD_DIR / f.rel}")
    for path, content in writes:
        write_operator_output_or_die(path, content, flag=_OUTPUT_FLAG)
        print(f"  📄 {path}")


def write_outputs(output_dir: str, tenant_name: str, defaults_data: dict, tenant_data: dict, report: str, relabel_snippet: str | None = None, mapping_hint: dict | None = None) -> None:
    """Write all output files: the conf.d ones through one `da-guard write`
    batch (ADR-036 §2, #3206 10c-3: Go writes the text, judges the tree once,
    writes all or none; otherwise rc 2 and nothing written), then the rest."""
    _refuse_invalid_tenant_id(tenant_name)
    confd, writes = _plan_outputs(output_dir, tenant_name, defaults_data, tenant_data,
                                  report, relabel_snippet, mapping_hint)
    _perform_writes(Path(output_dir), confd, writes)


def print_catalog() -> None:
    """Print the supported exporter catalog."""
    print("\n╔══════════════════════════════════════════════════════════════╗")
    print("║          Dynamic Alerting — Supported Exporters            ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    for key, pack in RULE_PACKS.items():
        metrics = ", ".join(pack.get("defaults", {}).keys())
        print(f"║ [預載] {pack['display']:<45}║")
        print(f"║   Exporter: {pack['exporter']:<44}║")
        print(f"║   Metrics:  {metrics:<44}║")
        print(f"║   Rule Pack: {pack['rule_pack_file']:<43}║")
        print("╠══════════════════════════════════════════════════════════════╣")
    print("╚══════════════════════════════════════════════════════════════╝")


def generate_relabel_snippet(tenant: str, namespaces: str | list[str], tenant_label: str = "tenant") -> str:
    """
    Generate Prometheus relabel_configs snippet for N:1 tenant mapping.

    Args:
        tenant: Tenant name
        namespaces: List of K8s namespace names (or comma-separated string)
        tenant_label: Label name to set (default: "tenant")

    Returns:
        YAML string with relabel_configs section
    """
    # Parse namespaces if string
    if isinstance(namespaces, str):
        ns_list = [ns.strip() for ns in namespaces.split(",") if ns.strip()]
    else:
        ns_list = list(namespaces)

    if not ns_list:
        return ""

    # Build regex from namespace list
    ns_regex = "|".join(ns_list)

    relabel_config = {
        "relabel_configs": [
            {
                "source_labels": ["__meta_kubernetes_namespace"],
                "action": "keep",
                "regex": ns_regex,
            },
            {
                "source_labels": ["__meta_kubernetes_namespace"],
                "regex": ns_regex,
                "target_label": tenant_label,
                "replacement": tenant,
            },
        ]
    }

    # Generate YAML with comment header
    yaml_lines = [
        "# Prometheus relabel_configs for N:1 tenant mapping",
        "# Add to scrape_configs[].relabel_configs",
        "",
    ]
    yaml_lines.append(yaml.dump(relabel_config, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip())

    return "\n".join(yaml_lines)


def run_interactive(output_dir: str) -> None:
    """Full interactive mode."""
    print("=" * 60)
    print("  scaffold_tenant.py — 互動式 Tenant Config 產生器")
    print("=" * 60)

    # Step 1: Tenant name
    tenant_name = _ask("\n📛 Tenant namespace (例如 db-c): ").strip()
    if not tenant_name:
        print("錯誤: Tenant name 不可為空")
        sys.exit(EXIT_CALLER_ERROR)
    _refuse_invalid_tenant_id(tenant_name)

    # Step 2: Select DB types
    db_options = [(k, v["display"]) for k, v in RULE_PACKS.items() if k != "kubernetes"]
    selected_dbs = prompt_multi(
        "📦 選擇要監控的 DB 類型:",
        db_options,
    )
    # Always include kubernetes
    selected_dbs = ["kubernetes"] + selected_dbs

    print(f"\n已選擇: {', '.join(selected_dbs)}")

    # Step 3: Configure thresholds
    customize = _ask("\n🔧 自訂閾值? (y=逐項設定 / N=使用預設值): ").strip().lower()
    interactive_thresholds = customize == "y"

    # Step 4: N:1 tenant mapping (optional)
    namespaces = None
    ns_input = _ask("\n📍 K8s 命名空間 (逗號分隔，例如 ns1,ns2,ns3，空白跳過): ").strip()
    if ns_input:
        namespaces = ns_input

    # Step 5: Profile assignment (optional, v1.12.0)
    profile_name = None
    profile_input = _ask("\n🏷️  使用 Tenant Profile? (輸入 profile 名稱，空白跳過): ").strip()
    if profile_input:
        profile_name = profile_input

    # Generate
    print("\n⚙️  正在生成...")
    defaults_data = generate_defaults(selected_dbs)
    tenant_data = generate_tenant(tenant_name, selected_dbs, interactive=interactive_thresholds)

    # Add _profile if specified (v1.12.0)
    if profile_name:
        tenant_data["tenants"][tenant_name]["_profile"] = profile_name

    # Add _namespaces metadata if provided
    if namespaces:
        tenant_data["tenants"][tenant_name]["_namespaces"] = [ns.strip() for ns in namespaces.split(",")]

    relabel_snippet = generate_relabel_snippet(tenant_name, namespaces) if namespaces else None
    report = generate_report(tenant_name, selected_dbs, output_dir, namespaces=namespaces)

    # Write
    print(f"\n📁 輸出至 {output_dir}/")
    write_outputs(output_dir, tenant_name, defaults_data, tenant_data, report, relabel_snippet=relabel_snippet)

    # Summary
    print("\n" + "=" * 60)
    print("✅ Tenant config 生成完畢！")
    print("=" * 60)

    print("\n  所有核心 Rule Packs (包含自我監控) 已透過 Projected Volume 預載於平台，無需額外掛載。")
    print(f"\n詳見 {output_dir}/scaffold-report.txt")


def _refuse_invalid_tenant_id(*tenant_names: object) -> None:
    """ADR-035: the id becomes a conf.d key and a file name — refuse what the
    tenant-id rule refuses (rc 2, the caller's input), before writing ANY
    file: every invalid id is named, and nothing is written for the valid
    ones either (a partial conf.d is not what the caller asked for)."""
    invalid = [t for t in tenant_names if not is_valid_tenant_id(t)]
    if not invalid:
        return
    for t in invalid:
        print(f"錯誤: 不合法的 tenant id {t!r}（{tenant_id_rule()[1]}）",
              file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)


def run_non_interactive(args: argparse.Namespace) -> None:
    """Non-interactive mode with CLI args."""
    tenant_name = args.tenant
    selected_dbs = ["kubernetes"] + [db.strip() for db in args.db.split(",")]
    output_dir = args.output_dir

    # Validate DB choices
    for db in selected_dbs:
        if db not in RULE_PACKS:
            print(f"錯誤: 不支援的 DB 類型 '{db}'")
            print(f"支援的類型: {', '.join(RULE_PACKS.keys())}")
            sys.exit(EXIT_CALLER_ERROR)

    print(f"⚙️  生成 {tenant_name} config (DBs: {', '.join(selected_dbs)})...")
    defaults_data = generate_defaults(selected_dbs)
    tenant_data = generate_tenant(tenant_name, selected_dbs, interactive=False)

    # Apply --profile if specified (v1.12.0)
    profile_name = getattr(args, "profile", None)
    if profile_name:
        tenant_data["tenants"][tenant_name]["_profile"] = profile_name

    # Apply --silent-mode if specified
    silent_mode = getattr(args, "silent_mode", None)
    if silent_mode and silent_mode != "disable":
        tenant_data["tenants"][tenant_name]["_silent_mode"] = silent_mode

    # Apply --severity-dedup if specified (only write if not default)
    severity_dedup = getattr(args, "severity_dedup", "enable")
    if severity_dedup == "disable":
        tenant_data["tenants"][tenant_name]["_severity_dedup"] = "disable"

    # Apply --routing-receiver if specified
    # Platform defaults are applied for timing params when not explicitly provided,
    # ensuring generated config is complete for generate_alertmanager_routes.py
    routing_receiver = getattr(args, "routing_receiver", None)
    if routing_receiver:
        receiver_type = getattr(args, "routing_receiver_type", "webhook")
        smarthost = getattr(args, "routing_smarthost", None)
        receiver_obj = build_receiver_from_args(receiver_type, routing_receiver, smarthost)
        routing = {"receiver": receiver_obj}

        # group_by: use explicit value or platform default
        routing_group_by = getattr(args, "routing_group_by", None)
        routing["group_by"] = (
            [g.strip() for g in routing_group_by.split(",")]
            if routing_group_by
            else ["alertname", "tenant"]
        )

        # Timing params: use explicit value or platform default
        routing["group_wait"] = getattr(args, "routing_group_wait", None) or "30s"
        routing["group_interval"] = getattr(args, "routing_group_interval", None) or "5m"
        routing["repeat_interval"] = getattr(args, "routing_repeat_interval", None) or "4h"

        tenant_data["tenants"][tenant_name]["_routing"] = routing

    # v2.1.0 ADR-007: Apply --routing-profile if specified
    routing_profile = getattr(args, "routing_profile", None)
    if routing_profile:
        tenant_data["tenants"][tenant_name]["_routing_profile"] = routing_profile

    # Apply --namespaces if specified (N:1 topology)
    namespaces = getattr(args, "namespaces", None)
    relabel_snippet = None
    if namespaces:
        ns_list = [ns.strip() for ns in namespaces.split(",")]
        tenant_data["tenants"][tenant_name]["_namespaces"] = ns_list
        relabel_snippet = generate_relabel_snippet(tenant_name, namespaces)

    # v2.1.0 ADR-006: Generate 1:N mapping hint if --topology=1:N
    topology = getattr(args, "topology", "1:1")
    mapping_hint = None
    if topology == "1:N":
        inst = getattr(args, "mapping_instance", None)
        filt = getattr(args, "mapping_filter", None)
        if inst and filt:
            mapping_hint = {
                "instance_tenant_mapping": {
                    inst: [{"tenant": tenant_name, "filter": filt}]
                }
            }
        else:
            print("  WARN: --topology=1:N requires --mapping-instance and "
                  "--mapping-filter", file=sys.stderr)
    elif topology == "N:1" and not namespaces:
        print("  WARN: --topology=N:1 requires --namespaces", file=sys.stderr)

    report = generate_report(tenant_name, selected_dbs, output_dir, namespaces=namespaces)

    print(f"\n📁 輸出至 {output_dir}/")
    write_outputs(output_dir, tenant_name, defaults_data, tenant_data, report,
                  relabel_snippet=relabel_snippet, mapping_hint=mapping_hint)

    print("\n✅ 完成 (所有核心 Rule Packs (包含自我監控) 已透過 Projected Volume 預載於平台，無需額外掛載)")


#: --from-onboard: onboard-hints.json, a JSON object (#3445).
ONBOARD_HINTS_DOCUMENT = _lib_input.Document("json", dict)


def run_from_onboard(args: argparse.Namespace) -> None:
    """Auto-scaffold tenants from onboard-hints.json.

    Reads hints produced by onboard_platform.py and generates config
    for each tenant with pre-filled DB types and routing hints.
    """
    # #3415 / #3445: the path and the content judged by the lib — missing,
    # not a file, not openable, not UTF-8, not JSON, not an object: rc 2,
    # the flag and the path named (a top level that was no object was an
    # AttributeError traceback).
    hints = _lib_input.document(args, "from_onboard", lang=_LANG)

    tenants = hints.get("tenants", [])
    if not tenants:
        # #452: readable-but-empty hints = unusable input = caller error,
        # consistent with the "cannot read hints" sibling above.
        print("No tenants found in onboard hints.", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # ADR-035: judged before the first file is written — and before the
    # de-duplication below, which hashes each id (a list there is named
    # here, not a TypeError).
    _refuse_invalid_tenant_id(*tenants)
    # A tenant named twice is scaffolded once (in its first place): two
    # `<t>.yaml` in one batch are a caller error to da-guard write (#3206).
    tenants = list(dict.fromkeys(tenants))

    output_dir = args.output_dir
    print(f"Auto-scaffolding {len(tenants)} tenant(s) from onboard hints...")

    # One `_defaults.yaml` for the run, of every pack any tenant selected
    # (#3206 10c-3). Each tenant used to plan its own, and the last one
    # written won: every other tenant's pack defaults (and declared keys) were
    # gone from the file its tenant file points at.
    per_tenant: list = []
    union_dbs: list = []
    for tenant_name in tenants:
        # Resolve DB types from hints, fallback to kubernetes only
        db_list = hints.get("db_types", {}).get(tenant_name, [])
        selected_dbs = ["kubernetes"] + [db for db in db_list if db in RULE_PACKS]
        per_tenant.append((tenant_name, selected_dbs))
        union_dbs += [db for db in selected_dbs if db not in union_dbs]
    defaults_data = generate_defaults(union_dbs)

    all_confd: list = [defaults_confd_file(defaults_data)]
    all_writes: list = []
    for tenant_name, selected_dbs in per_tenant:
        print(f"\n  {tenant_name}: DBs={', '.join(selected_dbs)}")
        tenant_data = generate_tenant(tenant_name, selected_dbs, interactive=False)

        # Apply routing hints if available
        routing_hint = hints.get("routing_hints", {}).get(tenant_name, {})
        if routing_hint and routing_hint.get("receiver_type"):
            # Routing hints provide structure but receivers need actual URLs
            # which onboard can extract from Alertmanager config
            routing = {}
            if routing_hint.get("group_wait"):
                routing["group_wait"] = routing_hint["group_wait"]
            if routing_hint.get("group_interval"):
                routing["group_interval"] = routing_hint["group_interval"]
            if routing_hint.get("repeat_interval"):
                routing["repeat_interval"] = routing_hint["repeat_interval"]
            if routing:
                tenant_data["tenants"][tenant_name].setdefault("_routing", {}).update(routing)

        report = generate_report(tenant_name, selected_dbs, output_dir)
        confd, writes = _plan_outputs(output_dir, tenant_name, defaults_data,
                                      tenant_data, report, with_defaults=False)
        all_confd += confd
        all_writes += writes

    # ADR-036 §2 (#2906, #3206 10c-3): every tenant's conf.d files go in one
    # `da-guard write` batch — all or nothing.
    _perform_writes(Path(output_dir), all_confd, all_writes)
    print(f"\n  Scaffolded {len(tenants)} tenants to {output_dir}/")


def main() -> None:
    """CLI entry point: Interactive tenant config generator for Dynamic Alerting."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=_h('description'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_h('epilog'),
    )
    parser.add_argument("--tenant", help=_h('tenant'))
    parser.add_argument("--db", help=_h('db'))
    parser.add_argument("-o", "--output-dir", default="scaffold_output", type=output_dir_type(),
                        help=_h('output_dir'))
    parser.add_argument("--catalog", action="store_true", help=_h('catalog'))
    parser.add_argument("--non-interactive", action="store_true", help=_h('non_interactive'))
    parser.add_argument("--namespaces", help=_h('namespaces'))
    parser.add_argument("--routing-profile", help=_h('routing_profile'))
    parser.add_argument("--topology", choices=["1:1", "N:1", "1:N"], default="1:1",
                        help=_h('topology'))
    parser.add_argument("--mapping-instance", help=_h('mapping_instance'))
    parser.add_argument("--mapping-filter", help=_h('mapping_filter'))
    parser.add_argument("--silent-mode", choices=list(SILENT_MODE_CHOICES),
                        help=_h('silent_mode'))
    parser.add_argument("--severity-dedup", choices=["enable", "disable"], default="enable",
                        help=_h('severity_dedup'))
    parser.add_argument("--routing-receiver", help=_h('routing_receiver'))
    parser.add_argument("--routing-receiver-type", default="webhook",
                        choices=["webhook", "email", "slack", "teams", "rocketchat", "pagerduty"],
                        help=_h('routing_receiver_type'))
    parser.add_argument("--routing-smarthost", help=_h('routing_smarthost'))
    parser.add_argument("--routing-group-by", help=_h('routing_group_by'))
    parser.add_argument("--routing-group-wait", help=_h('routing_group_wait'))
    parser.add_argument("--routing-group-interval", help=_h('routing_group_interval'))
    parser.add_argument("--routing-repeat-interval", help=_h('routing_repeat_interval'))
    parser.add_argument("--profile", help=_h('profile'))
    _lib_input.add_input(parser, "--from-onboard", decode=ONBOARD_HINTS_DOCUMENT,
                         help=_h('from_onboard'))

    args = parser.parse_args()
    if args.tenant is not None:
        _refuse_invalid_tenant_id(args.tenant)

    if args.catalog:
        print_catalog()
        sys.exit(EXIT_OK)

    if args.from_onboard is not None:  # '' is given: refused, named (#3797)
        run_from_onboard(args)
    elif args.non_interactive or (args.tenant and args.db):
        if not args.tenant or not args.db:
            print("錯誤: --non-interactive 模式需要 --tenant 和 --db 參數")
            sys.exit(EXIT_CALLER_ERROR)
        run_non_interactive(args)
    else:
        run_interactive(args.output_dir)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
