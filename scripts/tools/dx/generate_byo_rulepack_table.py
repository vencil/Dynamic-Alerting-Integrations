#!/usr/bin/env python3
"""generate_byo_rulepack_table.py — BYO Prometheus 規則包表產生器（#1267）

Lint class & scope (lint-policy.md §3): drift-guard (map↔source)，作用域為
`docs/integration/byo-prometheus-integration.{md,en.md}` 內的「可用的規則包」表。

為什麼是就地注入而不是 `docs/includes/` 片段：這兩份是**客戶會直接在 GitHub 上讀
的整合指南**，而 `--8<--` snippet 指令在 GitHub 的 Markdown 渲染器下是純文字，
讀者會在表格的位置看到一行 `--8<-- "docs/includes/..."`。故比照
`scripts/dx/generate_adr_index.py` 的 sentinel 就地注入，讓表格在 GitHub 與
MkDocs 兩邊都正常渲染。

這張表在 #1246 之前是手抄的，且沒有任何 gate：實測 13 列中 9 列數字過期、
jvm / nginx / liveness 三個 pack 整個漏列。#1246 手動校正後 **不到一天**又被
#1266 新增的兩條平台告警弄過期（39→41）——所以修法是讓它不再由人維護。

資料來源（兩個，都是既有正典，本工具不新增任何 SoT）：
  * ConfigMap 名稱與「內容」欄的檔名 ← `k8s/03-monitoring/deployment-prometheus.yaml`
    的 projected volume `sources[].configMap`（`name` + `items[].path`），
    也就是「你實際掛得到什麼」的唯一真實來源。
  * 規則數 ← 直接沿用 `generate_rule_pack_stats.py` 的 `gather_stats()`，
    不另寫一份計數邏輯，避免兩處對「什麼算一條規則」意見分歧。

用法:
  python3 scripts/tools/dx/generate_byo_rulepack_table.py            # 印出將產生的表
  python3 scripts/tools/dx/generate_byo_rulepack_table.py --check    # CI 模式 (exit 1 on drift)
  python3 scripts/tools/dx/generate_byo_rulepack_table.py --generate # 就地寫入目標文件
  python3 scripts/tools/dx/generate_byo_rulepack_table.py --generate --lang all
"""
import argparse
import os
import re
import sys
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _atomic_write import atomic_write_text  # noqa: E402
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_VIOLATION  # noqa: E402
from generate_rule_pack_stats import gather_stats  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

DEPLOYMENT = REPO_ROOT / "k8s" / "03-monitoring" / "deployment-prometheus.yaml"
DOC_DIR = REPO_ROOT / "docs" / "integration"

SENTINEL_START = "<!-- BYO_RULEPACK_TABLE_START -->"
SENTINEL_END = "<!-- BYO_RULEPACK_TABLE_END -->"

_CM_PREFIX = "prometheus-rules-"

LANG_STRINGS = {
    "zh": {
        "header": "| ConfigMap 名稱 | 內容 | 規則數 |",
        "sep": "|----------------|------|--------|",
    },
    "en": {
        "header": "| ConfigMap Name | Contents | Rules |",
        "sep": "|---|---|---|",
    },
}


def _doc_path(lang: str) -> Path:
    """目標文件（zh / en）。"""
    name = "byo-prometheus-integration.en.md" if lang == "en" else "byo-prometheus-integration.md"
    return DOC_DIR / name


def read_projected_packs() -> list:
    """從 Prometheus Deployment 的 projected volume 讀出掛載中的 Rule Pack。

    回傳 [(pack_key, configmap_name, [file, ...]), ...]，順序即 manifest 內的
    宣告順序（刻意不排序：那個順序是部署面的既有語意）。
    """
    docs = [d for d in yaml.safe_load_all(DEPLOYMENT.read_text(encoding="utf-8"))
            if d and d.get("kind") == "Deployment"]
    if not docs:
        raise ValueError(f"{DEPLOYMENT} 內找不到 kind: Deployment")
    volumes = docs[0]["spec"]["template"]["spec"]["volumes"]
    projected = [v for v in volumes if "projected" in v]
    if len(projected) != 1:
        raise ValueError(
            f"預期恰好一個 projected volume，實際 {len(projected)} 個 —— "
            "表格的來源假設已不成立，請先確認 manifest 結構。"
        )
    out = []
    unexpected = []
    for src in projected[0]["projected"]["sources"]:
        cm = src.get("configMap")
        if not cm:
            # A non-configMap projected source (secret / downwardAPI / serviceAccountToken)
            # is not a Rule Pack and legitimately has no row.
            continue
        name = cm["name"]
        if not name.startswith(_CM_PREFIX):
            # ⚠️ Do NOT skip silently. A mounted pack that this loop drops never
            # enters `mounted`, so the mounted-vs-counted cross-check below stays
            # happy and the table ships MISSING A PACK while --check reports green
            # — precisely the failure this generator exists to prevent (#1246 lost
            # jvm / nginx / liveness exactly that way). Found by adversarial review.
            unexpected.append(name)
            continue
        files = [i["path"] for i in cm.get("items", [])]
        out.append((name[len(_CM_PREFIX):], name, files))
    if unexpected:
        raise ValueError(
            "projected volume 內有不以 `%s` 開頭的 ConfigMap：%s\n"
            "本工具以該前綴辨識 Rule Pack；若命名慣例真的改了，請同步改 _CM_PREFIX，"
            "不要讓它被靜默略過（那會讓表格漏列而 --check 仍然通過）。"
            % (_CM_PREFIX, sorted(unexpected))
        )
    return out


def build_rows() -> list:
    """組出表格列；掛載集合與計數集合不一致時 fail loud。

    刻意**不**吃 lang：每一列都是 ConfigMap 名／檔名／數字，沒有可翻譯的成分，
    兩個語言版本的資料列逐字相同（差異只在 `render_table` 的表頭）。
    """
    projected = read_projected_packs()
    per_pack = gather_stats()["per_pack"]

    mounted = {k for k, _, _ in projected}
    counted = set(per_pack)
    if mounted != counted:
        raise ValueError(
            "projected volume 的 Rule Pack 集合與計數來源不一致，表格會靜默漏列或多列。\n"
            f"  只在 deployment-prometheus.yaml：{sorted(mounted - counted)}\n"
            f"  只在 rule-packs/ + configmap-rules-*：{sorted(counted - mounted)}\n"
            "請先讓兩者一致（新 pack 要同時掛進 projected volume），再重跑本工具。"
        )

    rows = []
    for key, cm_name, files in projected:
        counts = per_pack[key]
        contents = ", ".join(f"`{f}`" for f in files) if files else "—"
        rows.append(
            f"| `{cm_name}` | {contents} | {counts['recording']}R + {counts['alert']}A |"
        )
    return rows


def render_table(lang: str) -> str:
    """整張表（表頭 + 分隔列 + 資料列）。只有表頭隨語系不同。"""
    s = LANG_STRINGS[lang]
    return "\n".join([s["header"], s["sep"], *build_rows()]) + "\n"


def replace_sentinel_block(content: str, table: str, doc: Path) -> str:
    """把 SENTINEL_START / SENTINEL_END 之間的內容換成 table。

    ⚠️ 這個 pattern 必須是**冪等**的：結尾 sentinel 不吃換行（`table` 自己以 `\\n`
    結尾，換行由中間的 `.*?` 吸收）。若寫成 `(\\n?END)`，第一次注入產生
    `START\\n<table>END`、第二次比對時 `.*?` 會把換行讓給 group2 再補一個，
    每跑一次就多一個空行 → `--check` 永遠回報 drift。實際踩過。
    """
    pattern = re.compile(
        r"(" + re.escape(SENTINEL_START) + r"\n)"
        r".*?"
        r"(" + re.escape(SENTINEL_END) + r")",
        re.DOTALL,
    )
    if not pattern.search(content):
        # `relative_to` raises for a path outside the repo, which would REPLACE
        # this diagnostic with a confusing "not in the subpath of" error — the
        # failure path must not have its own failure path.
        try:
            shown = doc.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            shown = doc.as_posix()
        raise ValueError(
            f"{shown} 內找不到 sentinel 區塊。"
            f"請在表格位置加上這兩行：\n  {SENTINEL_START}\n  {SENTINEL_END}"
        )
    return pattern.sub(lambda m: m.group(1) + table + m.group(2), content)


def main() -> int:
    """CLI 入口。回傳 0（無 drift／已寫入）、1（drift，`--check`）、2（caller error）。"""
    ap = argparse.ArgumentParser(
        description="產生 BYO Prometheus 文件內的規則包表（sentinel 就地注入）。",
    )
    ap.add_argument("--check", action="store_true",
                    help="CI 模式：有 drift 就 exit 1，不寫檔")
    ap.add_argument("--generate", action="store_true",
                    help="就地寫入目標文件")
    ap.add_argument("--lang", choices=["zh", "en", "all"], default="zh",
                    help="要處理的語言版本（預設 zh；CI 與 --generate 請用 all）")
    args = ap.parse_args()
    # Sibling convention (generate_rule_pack_stats.py / generate_tool_map.py):
    # this tool prints CJK + emoji, and validate_all.py spawns it with
    # `text=True` and no explicit encoding. Without this, a cp950 console
    # emits bytes the parent cannot decode → `result.stdout` is None →
    # validate_all dies with an AttributeError, taking EVERY other check in
    # the run down with it. Found by adversarial review; #489 Phase B burned
    # the same way.
    try_utf8_stdout()

    langs = ["zh", "en"] if args.lang == "all" else [args.lang]

    try:
        rendered = {lang: render_table(lang) for lang in langs}
    except (ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, yaml.YAMLError) as exc:
        # A malformed manifest or an unreadable source is a CALLER/environment
        # error (exit 2), not "the table is stale" (exit 1). Without this the
        # traceback escapes and Python's default exit 1 misreports it as drift.
        print(f"ERROR: 讀取或解析來源失敗：{exc}", file=sys.stderr)
        return 2

    if not args.check and not args.generate:
        for lang in langs:
            print(f"--- {lang} ---")
            print(rendered[lang], end="")
        return 0

    # Two-phase: resolve EVERY language before writing ANY of them, so a
    # failure on the second language cannot leave the first already rewritten
    # ("non-zero exit" must imply "nothing changed"). Found by adversarial review.
    plan = []
    for lang in langs:
        doc = _doc_path(lang)
        try:
            current = doc.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: 讀取 {doc} 失敗：{exc}", file=sys.stderr)
            return 2
        try:
            updated = replace_sentinel_block(current, rendered[lang], doc)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        plan.append((doc, doc.relative_to(REPO_ROOT).as_posix(), current, updated))

    has_drift = False
    for doc, rel, current, updated in plan:
        if args.check:
            if updated.strip() != current.strip():
                print(f"❌ {rel} 的規則包表已過期。"
                      f"請跑 `python3 scripts/tools/dx/generate_byo_rulepack_table.py "
                      f"--generate --lang all`。")
                has_drift = True
            else:
                print(f"✅ {rel} 的規則包表是最新的。")
        else:
            if updated == current:
                print(f"✅ {rel}：無變更")
            else:
                atomic_write_text(doc, updated)
                print(f"✅ 已更新 {rel}")

    if has_drift:
        return EXIT_VIOLATION
    return 0


if __name__ == "__main__":
    sys.exit(main())
