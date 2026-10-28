#!/usr/bin/env python3
"""offboard_tenant.py — 安全的 Tenant 下架工具。

執行 Tenant 下架前的 Pre-check，確認無外部依賴後安全移除。

用法:
  # 預檢模式 (預設): 只檢查不刪除
  python3 offboard_tenant.py db-a

  # 執行下架
  python3 offboard_tenant.py db-a --execute

  # 指定 conf.d 目錄
  python3 offboard_tenant.py db-a --config-dir /path/to/conf.d --execute

  # 一併檢查 tenant-api state store 的引用（群組、views、federation 子集）
  kubectl -n tenant-api exec deploy/tenant-api -c tenant-api -- tenant-api state show > state.json
  python3 offboard_tenant.py db-a --state state.json --execute

Pre-check 項目:
  1. 確認 tenant config 檔案存在
  2. 掃描所有其他 tenant 是否有引用此 tenant
  3. 列出此 tenant 的所有已設定指標
  4. 檢查是否有 custom_ 前綴的規則引用此 tenant
  5. tenant-api state store 的引用（--state）：群組成員、views 的 filters、
     federation 子集；AccountID 配發只列為資訊（永不回收，ADR-021）。
     工具不寫 state store，只印出要呼叫的 tenant-api API。--state 收
     `tenant-api state show` 印出的 JSON（ADR-036 §12：紀錄由 tenant-api 自己的
     解析函式讀；舊的 ConfigMap／state 目錄輸入一律拒收並附取得 JSON 的指令）。
     沒給 --state，或用到的紀錄（groups、views、該租戶的 subset）帶 problem
     （tenant-api 無法使用）時標為「未判定」並點名，--execute 需另加
     --skip-state-check 才執行；帶 problem 的紀錄絕不當成空。

讀取（ADR-036 §2，#2906）：哪些檔、每個檔的內容一律取自 `da-guard parse-tree`
（yaml.v3 的讀法），不經 PyYAML。要刪的檔是 parse-tree 選到的租戶檔裡（任何深度，exporter 遞迴讀整棵樹），`tenants:`
宣告了這個 tenant 的每一個（檔名帶著它、卻沒宣告它的檔不算）（#3206：同一租戶由兩個
檔宣告時兩個都刪，否則租戶仍被服務），而且每一個都恰好只宣告這個 tenant——另宣告了
別的租戶時 Pre-check 不通過（刪檔會連帶下架別的租戶），一個都不刪。parse-tree 讀不了的
檔一律具名、擋下 --execute；沒有可用的 da-guard（`$DA_GUARD_BINARY`→`$PATH`）
結束碼 2。

刪除（ADR-036 §2，#3206 步驟 10）：只有 Go 寫 conf.d。--execute 不自己刪檔，而是把
每個要刪的檔各作一個刪除項目 `{path, delete: true}`，一批送給 `da-guard write`：它把
每個檔當成搬移的來源判（一般檔、不是 symlink、有讀取端、parse-tree 讀得了），並以拿掉
它們之後的整棵樹判一次（樹不能變壞：`worsens_tree`）。被拒時每個理由具名印在 stderr、
結束碼 2，什麼都不刪。Pre-check（有沒有 --execute 都一樣）的判定包含同一批的 `--dry-run`：
被拒同樣結束碼 2，「✅ Pre-check 通過」只在它也通過後才印。

<tenant> 先經 ADR-035 的租戶 id 規則驗證：不合法時結束碼 2、點名規則，什麼都不讀不刪。
"""

import sys
import os
import re
import argparse
from pathlib import Path

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_VIOLATION  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_io import exit_on_yaml_file_error  # noqa: E402  (#1654 unreadable YAML → rc 2, named)
import _lib_parse_cache  # noqa: E402  (ADR-036 §2, #2906: parse-tree's selection and content)
import _lib_parse_tree  # noqa: E402
from _lib_validation import is_valid_tenant_id, tenant_id_rule  # noqa: E402  (ADR-035)
import _lib_confedit  # noqa: E402  (ADR-036 §2, #3206: only Go writes conf.d — the delete too)
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402

#: The tool name on every line `_lib_confedit.write_all_or_exit` prints.
_TOOL = "offboard_tenant"
from _lib_confd import LEGACY_STATE_FILES  # noqa: E402  (#2883)
from _lib_tenant_state import (  # noqa: E402  (#2883 groups / views / subsets live in tenant-api's state store)
    KEY_ACCOUNT_REGISTRY,
    KEY_GROUPS,
    KEY_VIEWS,
    StateInputError,
    load_state,
    subset_key,
)


def _reader(config_dir):
    """The run's parse-tree reader of *config_dir* (one document per run)."""
    return _lib_parse_cache.reader_for(config_dir)


def _selected(config_dir):
    """Every config file parse-tree selects, at ANY depth (root-relative entry
    paths), sorted, each with its entry (Go's `role`, `class`). #3206: the
    exporter reads the whole tree, so does this tool — a tenant declared in
    `team-eu/tenant-a.yaml` is served, and offboarding must find it. The
    entries of the document the content is read from — one snapshot, no
    second walk."""
    reader = _reader(config_dir)
    selected = reader.tree().selected
    return sorted((e.path, e) for e in reader.listing().entries
                  if e.config_file and e.path in selected)


def find_config_file(tenant, config_dir):
    """tenant 的設定檔（`<config_dir>/<rel>`，見 `find_config_rel`），或 None。"""
    rel = find_config_rel(tenant, config_dir)
    return None if rel is None else str(Path(config_dir) / rel)


def find_config_rel(tenant, config_dir):
    """tenant 的設定檔相對 *config_dir* 的路徑（parse-tree 的 entry path：正規形式，
    沒有 `./`、`//`——`da-guard write` 收的就是這個形式），或 None。

    tenant 的設定檔：parse-tree 選到的檔裡（任何深度），檔名主幹等於 tenant 的第一個。

    #1588: the carrier is found by comparing the id its NAME carries, never
    by pasting an extension onto the id (`alpha.YAML` is alpha's), and
    exactly — not case-folded (`Alpha` is another tenant). ADR-036 §2: which
    files there are, and the id each name carries (`tenant_named`), are
    parse-tree's answer (a directory named `<tenant>.yaml/` refuses the run,
    #3206)."""
    base = Path(config_dir)
    if not base.is_dir():
        return None
    for rel, e in _selected(config_dir):
        # Go's `role`, not `is_file()` (#3206): a file parse-tree cannot read
        # refuses the run before this line is reached.
        if e.tenant_named == tenant and e.config_file:
            return rel
    return None


def find_carriers(tenant, config_dir):
    """Every file that carries *tenant* — the files `--execute` deletes, in
    one batch: the tenant files at any depth (Go's entry class `tenant`) whose
    `tenants:`, as parse-tree reads it, declares it. A file whose NAME
    carries the id but which declares no such tenant (a leftover
    `_federation/<tenant>.yaml` subset record, read as a tenant file) serves
    nothing of it: not a carrier, at most a cross-reference. Root-relative entry paths (parse-tree's clean
    form, what `da-guard write` takes), sorted.

    #3206: a tenant declared in two tenant files (`tenant-a.yaml` and
    `tenant-a.yml`, or `other.yaml`) is served from both; deleting one of
    them leaves the tenant served. Which files those are is the index's
    answer (class, the declared keys), never a name compared
    here. A platform file (`_platform.yaml`) that mentions the tenant is not
    a carrier the exporter counts twice: it stays a cross-reference; nor is a
    subdirectory's `_defaults.yaml` (class `defaults`)."""
    base = Path(config_dir)
    if not base.is_dir():
        return []
    out = []
    for rel, e in _selected(config_dir):
        if e.cls != "tenant" or rel in LEGACY_STATE_FILES:
            continue
        if tenant in (declared_by(str(base / rel), config_dir) or ()):
            out.append(rel)
    return out


def declared_by(path, config_dir):
    """The tenants the file at *path* declares, as parse-tree reads it (its
    `tenants:` mapping's keys), or None when parse-tree cannot read it."""
    try:
        data = _reader(config_dir).read(path)
    except _lib_parse_tree.FileUnreadable:
        return None
    tenants = data.get("tenants")
    return sorted(tenants) if isinstance(tenants, dict) else []


def load_all_configs(config_dir):
    """載入 conf.d 的所有設定檔（parse-tree 選到的檔，任何深度，ADR-036 §2；鍵是
    root-relative 的 entry path）。

    Each entry is {"path", "data" (yaml.v3's values), "nodes" (parse-tree's
    nodes: every key and scalar as its source text), "tenant_named" (the
    tenant id the file NAME carries, Go's `Entry.tenant_named`; None for a
    `_` file)}.

    ⛔ #3206: the reader's tree read is the run's fail-closed exit
    (`_lib_parse_cache.ConfdReader.tree`): a tree with a path Go names — a
    directory the walk could not list, a FIFO, a dangling link, a file the
    exporter cannot decode — ends the run there, every such path named,
    exit 2, before the pre-check reports and before `--execute` deletes
    anything. Every file selected here is therefore one parse-tree read.
    """
    configs = {}
    base = Path(config_dir)
    # #3206: the whole tree, as the exporter reads it (it used to be flat,
    # with `warn_nested` naming the files a hierarchical conf.d hid).
    reader = _reader(config_dir)
    for filename, entry in _selected(config_dir):
        if filename in LEGACY_STATE_FILES:
            # #2883: tenant-api keeps these records in its state store since
            # #2856; it imports this copy at startup only while the store has
            # no such record. check_state_references judges it against --state.
            print(f"  ⚠️  略過 {safe_label(filename)}: tenant-api 只在 state store 沒有這筆紀錄時"
                  "於開機匯入它（引用改由 --state 判定）")
            continue
        nodes = reader.node(filename)
        configs[filename] = {"path": str(base / filename),
                             "data": _lib_parse_tree.to_python(nodes),
                             "nodes": nodes, "tenant_named": entry.tenant_named}
    return configs


def _texts(nodes, *, values=False):
    """Every mapping key and scalar source text under parse-tree *nodes*
    (*values*: a structure of Python values instead — each scalar's str)."""
    out, stack = [], [nodes]
    while stack:
        n = stack.pop()
        if not values and _lib_parse_tree.is_scalar(n):
            out.append(n["text"])
        elif isinstance(n, dict):
            out.extend(str(k) for k in n)
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)
        elif values and n is not None:
            out.append(str(n))
    return out


def check_cross_references(tenant, configs, carriers=None):
    """檢查其他設定檔中是否有引用此 tenant（configs 取自 `load_all_configs`）。

    租戶自己的檔是 *carriers*（`find_carriers`：下架時一併刪除的檔）；沒給時是檔名
    帶著這個租戶 id 的那一個——Go 的 `tenant_named`（#3206），不是以 `<tenant>.`
    開頭的任何檔：`db-a.bak.yaml` 帶的是 `db-a.bak`，它對 `db-a` 的引用要列出來。"""
    references = []
    for filename, info in configs.items():
        if (filename in carriers) if carriers is not None else info["tenant_named"] == tenant:
            continue  # 跳過自己
        # 搜尋檔案內容中是否出現 tenant 名稱：每個 key 與純量的原文（parse-tree，
        # 所以 `[010]` 這種以值點名租戶的清單也找得到，#2216）。
        texts = _texts(info["nodes"])
        if any(tenant in t for t in texts):
            references.append(filename)
    return references


def get_tenant_metrics(tenant, configs, carriers=None):
    """取得 tenant 的所有已設定指標：*carriers*（`find_carriers`；沒給時是檔名帶著
    這個租戶 id 的檔，Go 的 `tenant_named`，#3206）裡 `tenants.<tenant>` 的內容，
    依檔名順序合在一起。"""
    metrics = {}
    for filename, info in sorted(configs.items()):
        if (filename in carriers) if carriers is not None else info["tenant_named"] == tenant:
            tenants = info["data"].get("tenants", {})
            block = tenants.get(tenant, {}) if isinstance(tenants, dict) else {}
            if isinstance(block, dict):
                metrics.update(block)
    return metrics


def _legacy_refs(tenant, state, config_dir):
    """conf.d 殘留、而 state store 沒有對應紀錄的舊檔中對 tenant 的引用。

    tenant-api 每次開機在 state store 沒有該筆紀錄時，會從 conf.d 匯入
    `_groups.yaml`／`_views.yaml`／`_federation_policy.yaml` 與
    `_federation/<id>.yaml`（statemigrate.Import），所以這些引用在下次
    重啟後就會出現在 state store。
    """
    refs = []
    if config_dir is None:
        return refs
    base = Path(config_dir)
    reader = _reader(config_dir)
    tree = reader.tree()

    def config_file(rel):
        # #3206 m6: whether *rel* is a config file is the entry's role in the
        # reader's one listing, never `is_file()` (False for a file whose
        # stat is refused, which is then a file read and failing loudly).
        e = reader.entry(rel)
        return e is not None and e.config_file
    for name in LEGACY_STATE_FILES:
        path = base / name
        if name == "_account_registry.yaml" or state.has(name) or not config_file(name):
            continue
        if name not in tree.selected:
            continue
        if any(tenant in t for t in _texts(tree.node(name))):
            refs.append((f"conf.d 的 {safe_label(name)}（state store 沒有這筆紀錄，tenant-api 下次開機會匯入）",
                         f"刪除 {safe_label(str(path))}，或先讓 tenant-api 匯入後再以 API 清除"))
    if not state.has(subset_key(tenant)):
        for ext in (".yaml", ".yml"):
            path = base / "_federation" / f"{tenant}{ext}"
            if config_file(f"_federation/{tenant}{ext}"):
                refs.append((f"conf.d 的 _federation/{safe_label(path.name)}（tenant-api 下次開機會匯入為子集紀錄）",
                             f"刪除 {safe_label(str(path))}"))
    return refs


def state_keys_used(tenant):
    """這個工具判定 tenant 的 state 引用時用到的紀錄（ADR-036 §12）：groups、
    views 與該租戶的 federation 子集。federation 白名單與 AccountID 帳本不在
    其中——前者不含租戶引用，後者只列為資訊。"""
    return (KEY_GROUPS, KEY_VIEWS, subset_key(tenant))


def undetermined_state_records(tenant, state):
    """用到的紀錄中 tenant-api 無法使用的（帶 problem）：[(key, Problem)]。
    這些紀錄的引用無法判定——絕不當成空（ADR-036 §12）。"""
    return [(k, state.problem(k)) for k in state_keys_used(tenant)
            if state.problem(k) is not None]


def check_state_references(tenant, state, config_dir=None):
    """tenant-api state store 中對 tenant 的引用（#2883）。

    回傳 (references, info)：references 是 (說明, 要呼叫的 API) 的清單，
    info 是只供參考的行（AccountID 配發）。config_dir 給定時，另列 conf.d
    殘留舊檔中 tenant-api 下次開機會匯入的引用。帶 problem 的紀錄不在這裡
    判定（`undetermined_state_records` 點名它們）。
    """
    refs = _legacy_refs(tenant, state, config_dir)
    if state.problem(KEY_GROUPS) is None:
        for gid, g in sorted(state.groups().items()):
            members = [str(m) for m in (g.get("members") or [])]
            if tenant in members:
                refs.append((f"群組 {safe_label(gid)} 的成員",
                             f"PUT /api/v1/groups/{safe_label(gid)}（members 移除 {safe_label(tenant)}）"))
    if state.problem(KEY_VIEWS) is None:
        for vid, v in sorted(state.views().items()):
            filters = v.get("filters") or {}
            if not isinstance(filters, dict):
                continue
            keys = sorted(str(k) for k, val in filters.items() if str(val) == tenant)
            if keys:
                refs.append((f"view {safe_label(vid)} 的 filters（{safe_label(', '.join(keys))}）",
                             f"PUT 或 DELETE /api/v1/views/{safe_label(vid)}"))
    key = subset_key(tenant)
    if state.has(key) and state.problem(key) is None:
        refs.append((f"federation 子集紀錄 {safe_label(key)}",
                     "tenant-api 沒有刪除子集的 API：照 docs/internal/tenant-offboarding-runbook.md 步驟 2 "
                     "以 kubectl patch 移除 ConfigMap 的這個 key"))
    info = []
    reg_problem = state.problem(KEY_ACCOUNT_REGISTRY)
    if reg_problem is not None:
        info.append(f"AccountID 帳本 tenant-api 無法使用（{safe_label(reg_problem.kind)}）："
                    f"{safe_label(reg_problem.message)}——配發無法列出（只供參考，不擋下架）")
    else:
        account = state.account_id(tenant)
        if account is not None:
            info.append(f"AccountID 配發 {safe_label(account)}：永不回收（ADR-021），不需要也不可以清")
    return refs, info


def run_precheck(tenant, config_dir, state=None, *, emit=None):
    """執行完整 Pre-check，回傳 (can_proceed, report_lines)。

    state: _lib_tenant_state.TenantState，或 None（未提供 --state：state
    store 的引用標為未判定）。

    #3206: when the checks pass, the batch `--execute` sends is asked of
    `da-guard write --dry-run` BEFORE the verdict: a delete it refuses is
    exit 2 (`write_all_or_exit`, each reason named), and "✅ Pre-check 通過"
    is never printed for it. *emit* (the CLI's `print`) gets every report
    line before that judgement, so the report is on screen when it exits.
    """
    report = []
    issues = []

    report.append(f"{'='*60}")
    report.append(f"🔍 Tenant 下架 Pre-check: {safe_label(tenant)}")
    report.append(f"{'='*60}\n")

    # 1. 檔案存在性：宣告這個租戶的每一個租戶檔（`find_carriers`），--execute 一批刪
    carriers = find_carriers(tenant, config_dir)
    for rel in carriers:
        config_file = str(Path(config_dir) / rel)
        report.append(f"✅ 設定檔案: {safe_label(config_file)}")
        # ADR-036 §2: whether deleting this file offboards exactly this tenant
        # is parse-tree's answer — the tenants its `tenants:` declares.
        # (`find_carriers` keeps only the files that declare it.)
        declared = declared_by(config_file, config_dir)
        if declared is not None and declared != [tenant]:
            others = [t for t in declared if t != tenant]
            if others:
                report.append(f"❌ {safe_label(config_file)} 另宣告了 "
                              f"{safe_label(', '.join(others))}——刪檔會一併下架它們")
                issues.append(f"設定檔另宣告了其他 tenant: {safe_label(', '.join(others))}")
    if len(carriers) > 1:
        report.append(f"⚠️  {safe_label(tenant)} 由 {len(carriers)} 個檔宣告（exporter 兩處都讀），"
                      "下架會一批刪除全部")
    # A tenant file NAMED for the tenant that declares no such tenant: name
    # it, whether or not other files declare the tenant (it is not deleted —
    # it serves nothing of this tenant). With no carrier it is the reason
    # there is nothing to offboard, not "not found".
    named = [rel for rel, e in _selected(config_dir)
             if e.cls == "tenant" and e.tenant_named == tenant and rel not in carriers]
    mark = "⚠️ " if carriers else "❌"
    for rel in named:
        path = str(Path(config_dir) / rel)
        declared = declared_by(path, config_dir) or []
        what = (f"宣告了 {safe_label(', '.join(declared))}" if declared
                else "沒有宣告任何租戶")
        report.append(f"{mark} {safe_label(path)} 檔名是 {safe_label(tenant)} 但沒宣告它（{what}），不刪")
    if not carriers:
        if not named:
            report.append(f"❌ 找不到設定檔案: {safe_label(tenant)}.yaml")
        issues.append("沒有宣告此 tenant 的設定檔")

    # 2. 載入所有 configs
    # #2179 / #3206: a file that cannot be read never gets here — the
    # reader's tree read refused the run (exit 2) before this report.
    configs = load_all_configs(config_dir)
    report.append(f"\n📂 掃描目錄: {safe_label(config_dir)} "
                  f"({len(configs)} 個檔案)\n")

    # 3. Cross-reference check
    refs = check_cross_references(tenant, configs, carriers)
    if refs:
        report.append(f"⚠️  發現跨檔案引用 (請手動確認):")
        for ref in refs:
            report.append(f"   → {safe_label(ref)}")
        issues.append(f"跨檔案引用: {safe_label(', '.join(refs))}")
    else:
        report.append(f"✅ 無跨檔案引用")

    # 3b. tenant-api state store 的引用（#2883）
    if state is None:
        report.append("⚠️  state store 引用：未判定（未提供 --state；群組、views、"
                      "federation 子集存在 tenant-api 的 state store，不在 conf.d）")
    else:
        for key, prob in undetermined_state_records(tenant, state):
            report.append(f"⚠️  state store 引用：未判定——{safe_label(key)} tenant-api 無法使用"
                          f"（{safe_label(prob.kind)}）：{safe_label(prob.message)}")
        state_refs, state_info = check_state_references(tenant, state, config_dir)
        if state_refs:
            report.append(f"⚠️  state store 引用 ({safe_label(state.source)})，"
                          "工具不寫 state store，請在下架後呼叫 tenant-api 清除:")
            for what, how in state_refs:
                report.append(f"   → {what}: {how}")
            issues.append(f"跨檔案引用 (state store): {len(state_refs)} 處")
        else:
            report.append(f"✅ 無 state store 引用 ({safe_label(state.source)})")
        for line in state_info:
            report.append(f"   ℹ️  {line}")

    # 4. 列出 tenant 的所有指標
    metrics = get_tenant_metrics(tenant, configs, carriers)
    if metrics:
        report.append(f"\n📊 此 tenant 的已設定指標 ({len(metrics)} 個):")
        for key, val in metrics.items():
            report.append(f"   • {safe_label(key)}: {safe_label(val)}")
    else:
        # ⛔ 不寫「全部使用平台預設值」（#1321）：`_defaults.yaml` 的
        # `optional_overrides:` 宣告層只有 key 名、沒有值，那些 key 沒被租戶設定
        # 就是**沒有值＝靜默**，不是「沿用平台預設」。下架 pre-check 的讀者正是要
        # 判斷「拿掉這個 tenant 會失去什麼」的人，講反了會低估。
        report.append("\n📊 此 tenant 未設定任何指標覆寫 "
                      "(有平台預設值的 key 沿用預設；平台只宣告、不主張值的 key 維持靜默)")

    # 5. 最終判定
    emitted = 0

    def flush():
        nonlocal emitted
        if emit is not None:
            for line in report[emitted:]:
                emit(line)
        emitted = len(report)

    can_proceed = all(i.startswith("跨檔案引用") for i in issues)
    if can_proceed:
        # #3206: da-guard write's judgement of the same batch is part of the
        # verdict — a refusal exits 2 here (the report already shown), before
        # any "通過" line.
        flush()
        results = _lib_confedit.write_all_or_exit(
            config_dir, [_lib_confedit.delete_entry(rel, tenants=[tenant]) for rel in carriers],
            tool=_TOOL, dry_run=True)
        for r in results:
            path = safe_label(str(Path(config_dir) / r["path"]))
            if r["status"] == _lib_confedit.UNCHANGED:
                report.append(f"❌ {path} 已不在（da-guard write 判定時沒有這個檔）")
                issues.append("設定檔案不存在")
            else:
                report.append(f"🔎 da-guard write --dry-run：可刪除 {path}")
        can_proceed = all(i.startswith("跨檔案引用") for i in issues)
    report.append(f"\n{'='*60}")

    if not issues:
        report.append("✅ Pre-check 通過！可安全下架。")
    elif can_proceed:
        report.append("⚠️  Pre-check 有警告，但可手動確認後繼續。")
    else:
        report.append("❌ Pre-check 失敗，無法下架。")
    report.append(f"{'='*60}")
    flush()

    return can_proceed, report


def execute_offboard(tenant, config_dir):
    """執行下架：刪除宣告 tenant 的每一個設定檔（`find_carriers`）——一批 `da-guard
    write` 的刪除項目（ADR-036 §2），整批判一次，全部通過才刪。

    同一批的 `--dry-run` 是 Pre-check 的一部分（`run_precheck`）。

    True 是刪了；False 是沒有檔可刪（找不到，或 da-guard 判定時
    檔都已不在：`unchanged`）。da-guard 拒刪（不是一般檔、讀不了、拿掉後樹變壞……）、
    沒有可用的 da-guard、或寫到一半停下，由 `_lib_confedit.write_all_or_exit` 具名印在
    stderr、結束碼 2；被拒時什麼都沒刪。"""
    rels = find_carriers(tenant, config_dir)
    if not rels:
        print(f"❌ 找不到 {safe_label(tenant)} 的設定檔案", file=sys.stderr)
        return False

    results = _lib_confedit.write_all_or_exit(
        config_dir, [_lib_confedit.delete_entry(rel, tenants=[tenant]) for rel in rels], tool=_TOOL)
    gone = [r["path"] for r in results if r["status"] == _lib_confedit.UNCHANGED]
    done = [r["path"] for r in results if r["status"] != _lib_confedit.UNCHANGED]
    for rel in gone:
        print(f"⚠️  {safe_label(str(Path(config_dir) / rel))} 已不在"
              "（da-guard write 判定時沒有這個檔）", file=sys.stderr)
    if not done:
        print("❌ 沒有刪除任何檔", file=sys.stderr)
        return False
    for rel in done:
        print(f"🗑️  已刪除: {safe_label(str(Path(config_dir) / rel))}")
    print(f"\n📋 後續步驟:")
    print(f"  1. threshold-exporter 將在下次 reload (30s) 時自動清除 "
          f"{safe_label(tenant)} 的閾值")
    print(f"  2. Prometheus 下次 scrape 時，{safe_label(tenant)} 的向量將消失")
    print(f"  3. 所有相關 Alert 將自動解除")
    print(f"  4. 請記得一併清理 Alertmanager 中 "
          f"tenant={safe_label(tenant)} 的 routing 設定")
    return True


@exit_on_yaml_file_error  # #1654: an unreadable YAML file → rc 2, named
@_lib_parse_cache.exit_on_parse_tree_error  # ADR-036 §2: no usable da-guard → rc 2
def main():
    """CLI entry point: 安全的 Tenant 下架工具。."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="安全的 Tenant 下架工具 — Pre-check + 安全移除"
    )
    parser.add_argument("tenant", help="要下架的 tenant 名稱 (例如 db-a)")
    parser.add_argument("--config-dir",
                        default="components/threshold-exporter/config/conf.d",
                        help="conf.d 目錄路徑")
    parser.add_argument("--execute", action="store_true",
                        help="實際執行下架 (預設只做 Pre-check)")
    parser.add_argument("--state",
                        help="`tenant-api state show` 印出的 JSON（- 為標準輸入）："
                             "`kubectl exec deploy/tenant-api -c tenant-api -- tenant-api state show > state.json`")
    parser.add_argument("--skip-state-check", action="store_true",
                        help="state store 引用無法判定時仍允許 --execute：沒給 --state，"
                             "或用到的紀錄（groups、views、該租戶的 subset）tenant-api 無法使用")

    args = parser.parse_args()
    # ADR-035: the id names a conf.d key and a file — refuse what the
    # tenant-id rule refuses before anything is read or deleted (#3206).
    if not is_valid_tenant_id(args.tenant):
        print(f"ERROR: 不合法的 tenant id {safe_label(repr(args.tenant))}"
              f"（{tenant_id_rule()[1]}）", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    state = None
    if args.state:
        try:
            state = load_state(args.state)
        except StateInputError as e:
            print(f"ERROR: {safe_label(e)}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

    with _lib_parse_cache.session():  # one parse-tree document for the run
        _run(args, state)


def _run(args, state):
    # Each line printed as it is final: a pre-check whose dry run is refused
    # (exit 2) still shows the report above the refusal.
    can_proceed, _report = run_precheck(args.tenant, args.config_dir, state, emit=print)

    if args.execute:
        if can_proceed and state is None and not args.skip_state_check:
            print("\n❌ 未提供 --state，無法確認 tenant-api state store 的引用。"
                  "加 --state，或明確加 --skip-state-check 後再執行下架。")
            sys.exit(EXIT_CALLER_ERROR)
        undetermined = [] if state is None else undetermined_state_records(args.tenant, state)
        if can_proceed and undetermined and not args.skip_state_check:
            names = ", ".join(safe_label(k) for k, _ in undetermined)
            print(f"\n❌ state store 引用無法判定：tenant-api 無法使用 {names}。"
                  "修好這些紀錄後重新取得 state show，或明確加 --skip-state-check 後再執行下架。")
            sys.exit(EXIT_CALLER_ERROR)
        if can_proceed:
            print(f"\n⚡ 正在執行下架...\n")
            if not execute_offboard(args.tenant, args.config_dir):
                sys.exit(EXIT_CALLER_ERROR)  # nothing was deleted: not a success
        else:
            print(f"\n❌ Pre-check 未通過，無法執行下架。")
            sys.exit(EXIT_VIOLATION)
    else:
        print(f"\n💡 這是 Pre-check 模式。要實際下架，請加 --execute 參數。")
        if not can_proceed:
            # #2179: a failed pre-check is a failed run, with or without
            # --execute (it used to print "❌ Pre-check 失敗" and exit 0).
            sys.exit(EXIT_VIOLATION)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
