"""parse-tree 說「沒有讀者讀」的檔，每支改讀 parse-tree 的工具都不讀它（#2906 review）。

缺陷類別：工具自己列舉 conf.d（`_lib_confd.iter_config_files` / `list_config_tree`），
再用 Python 版的 carrier 選擇（`select_defaults_carrier` / `unselected_carriers` /
`readable_carriers`）決定讀哪個，然後經 `_lib_parse_cache.ConfdReader` 讀。那是同一條
規則的第二份實作：它挑中一個 parse-tree 列在 `not_read` 的檔時，`read` 丟
`NotSelected`，工具在合法的樹上以 rc 2 結束。根治是「讀或跳過」一律問 parse-tree
（`ConfdReader.not_read` / `split_not_read` / `carrier`）。

判準的 oracle 是 parse-tree 本身：

1. 每棵樹先跑 `da-guard parse-tree`，斷言它的 `not_read` 正好是我們放進去的那些檔
   （tree 模式下 `not_read` 唯一的 reason 是 `unselected_defaults_carrier`，見
   parse_tree.go `notReadUnselectedCarrier`；隱藏檔與非 YAML 檔不在 walk 裡、
   tree 模式不列）。形狀涵蓋根目錄與子目錄、`.yaml` / `.yml` 與大小寫變體。
2. 每支工具跑在每棵樹上：不得 rc 2、不得 traceback；`not_read` 檔的內容（刻意寫成
   每支工具都會報的錯值，含記號 `zzqmark`）不得出現在任何輸出裡。
3. 產生器在快取模式（產生器容器：不跑 tree 模式）下對 `not_read` 的判斷與 CLI 相同。

⚠️ 第 2 條的「記號不得出現」只對會把讀到的值寫進輸出的工具有牙齒。`policy_engine`、
`explain_keys` 只讀根目錄的 carrier，在子目錄的形狀上只能證明「不崩」；`assemble_config_dir`
的產出目錄整個掃進比對文字。

⚠️ 在 4c87384 上第 2 條量不紅：今天兩份選擇規則在這些形狀上答案一致（見 PR 回報）。
這裡守的是類別——下一次兩份實作分岔時，紅的是這支，而不是使用者的 rc 2。
"""
from __future__ import annotations

import functools
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
MARK = "zzqmark"

_RECEIVER = ("    _routing:\n      receiver:\n        type: webhook\n"
             "        url: https://hook.example/x\n")
_TENANT = 'tenants:\n  tenant-a:\n    mysql_connections: "70"\n' + _RECEIVER
_SELECTED = "defaults:\n  mysql_connections: 80\n"
#: The content of every file parse-tree lists in `not_read`: a value each
#: tool would report if it read the file — a threshold that is no number, a
#: routing / policy / custom-alert block of the wrong shape, a tenant that
#: references a profile nobody defines — each carrying MARK.
_UNREAD = (f"defaults:\n  {MARK}_metric: \"{MARK}\"\n  mysql_connections: {MARK}\n"
           f"_routing_enforced:\n  enabled: {MARK}\n"
           f"_routing_defaults:\n  receiver: {MARK}\n"
           f"_policies:\n  - name: {MARK}\n    target: {MARK}\n    operator: {MARK}\n"
           f"_custom_alerts: {MARK}\n"
           f"tenants:\n  {MARK}:\n    _routing_profile: {MARK}\n"
           f"  tenant-a:\n    mysql_connections: {MARK}\n")

#: shape → ({rel: content}, the files parse-tree must list in `not_read`).
#: A `.yaml` spelling beats a `.yml` one; among `.yaml` case variants the
#: last in byte order wins, among `.yml` ones the first (`SelectDefaultsCarriers`).
_SHAPES: dict[str, dict[str, str]] = {
    "root .yml beside .yaml": {"_defaults.yaml": _SELECTED, "_defaults.yml": _UNREAD},
    "root .yaml case variants": {"_DEFAULTS.YAML": _UNREAD, "_defaults.yaml": _SELECTED},
    "root .yml case variants": {"_Defaults.yml": _SELECTED, "_defaults.YML": _UNREAD},
    "root .YML beside .Yaml": {"_Defaults.Yaml": _SELECTED, "_DEFAULTS.YML": _UNREAD},
    "root three spellings": {"_defaults.yaml": _SELECTED, "_DEFAULTS.YAML": _UNREAD,
                             "_defaults.yml": _UNREAD},
    "nested .yml beside .yaml": {"_defaults.yaml": _SELECTED,
                                 "team-a/_defaults.yaml": _SELECTED,
                                 "team-a/_defaults.yml": _UNREAD},
    "nested .yaml case variants": {"team-a/_Defaults.YAML": _UNREAD,
                                   "team-a/_defaults.yaml": _SELECTED},
    "nested .yml case variants, no root carrier": {"team-a/_DEFAULTS.YML": _SELECTED,
                                                   "team-a/_defaults.yml": _UNREAD},
    "deep, root and nested at once": {"_DEFAULTS.YML": _SELECTED, "_defaults.yml": _UNREAD,
                                      "a/b/_defaults.YAML": _SELECTED,
                                      "a/b/_defaults.yml": _UNREAD},
}
_EXPECTED_NOT_READ = {shape: {rel for rel, text in files.items() if text is _UNREAD}
                      for shape, files in _SHAPES.items()}

#: Every CLI this PR moved onto parse-tree that reads a conf.d (the `_PROBES`
#: of test_yaml_v3_reading_across_tools.py, plus explain-keys and assemble,
#: whose reads are parse-tree's too). {d} = the conf.d, {out} = a scratch dir.
_TOOLS: dict[str, tuple[list[str], tuple[str, ...]]] = {
    "ops/generate_alertmanager_routes.py": (
        ["--config-dir", "{d}", "--validate", "--strict", "--findings-json", "{out}/f.json"],
        ("f.json",)),
    "ops/validate_config.py": (["--config-dir", "{d}", "--json"], ()),
    "ops/explain_route.py": (["--config-dir", "{d}", "--tenant", "tenant-a", "--json"], ()),
    "ops/notification_tester.py": (["--config-dir", "{d}", "--dry-run", "--json"], ()),
    "ops/policy_engine.py": (["--config-dir", "{d}", "--json", "--ci"], ()),
    "ops/explain_keys.py": (["--config-dir", "{d}", "--json"], ()),
    "ops/assemble_config_dir.py": (
        ["--sources", "{d}", "--output", "{out}/asm"], ("asm",)),
    "lint/check_routing_profiles.py": (["--config-dir", "{d}", "--strict"], ()),
    "lint/check_confd_schema.py": (["--config-dir", "{d}"], ()),
}


#: A tool whose rc 2 on a tree is a documented verdict, not a failed read:
#: the sentence that says so. explain-keys exits 2 when no root platform file
#: was read (ADR-038 decision 7) — a shape with no root carrier.
_RC2_VERDICT = {"ops/explain_keys.py": "threshold keys are undecidable"}
#: `ConfdReader`'s `NotSelected` message: the read this file guards against.
_NOT_SELECTED = "da-guard parse-tree does not read this file"


def _write_tree(d: pathlib.Path, shape: str) -> None:
    files = {"tenant-a.yaml": _TENANT, **_SHAPES[shape]}
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    if env.get("DA_GUARD_BINARY", "").strip():
        env["DA_GUARD_BINARY"] = os.path.abspath(env["DA_GUARD_BINARY"])
    return env


@pytest.mark.usefixtures("da_guard_env")
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_parse_tree_lists_the_planted_files_in_not_read(shape, tmp_path):
    """The oracle: parse-tree itself says no reader reads the planted files —
    and nothing else (a tree built wrong would make every row below vacuous)."""
    _write_tree(tmp_path, shape)
    r = subprocess.run([os.environ["DA_GUARD_BINARY"], "parse-tree", "--config-dir", str(tmp_path)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120,
                       env=_env())
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    not_read = {e["path"]: e["not_read"] for e in doc["entries"] if e["not_read"] is not None}
    assert set(not_read) == _EXPECTED_NOT_READ[shape], not_read
    assert set(not_read.values()) == {"unselected_defaults_carrier"}
    # No reader, and nothing read: a config file nobody reads is not parsed.
    assert all(e["readers"] == [] for e in doc["entries"] if e["path"] in not_read)
    assert not set(not_read) & set(doc["files"])


@functools.lru_cache(maxsize=None)
def _run(tool: str, shape: str) -> tuple[int, str]:
    argv, outputs = _TOOLS[tool]
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp, "conf.d")
        out = pathlib.Path(tmp, "out")
        d.mkdir()
        out.mkdir()
        _write_tree(d, shape)
        r = subprocess.run(
            [sys.executable, str(TOOLS / tool)] + [a.format(d=d, out=out) for a in argv],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=tmp, env=_env(), timeout=300)
        text = r.stdout + "\n" + r.stderr
        for f in outputs:
            target = out / f
            # An output directory (assemble's) is read whole: a `not_read`
            # file copied into it is the leak this row looks for.
            for q in sorted(target.rglob("*")) if target.is_dir() else [target]:
                if q.is_file():
                    text += "\n" + q.read_text(encoding="utf-8", errors="replace")
    return r.returncode, text


@pytest.mark.usefixtures("da_guard_env")
@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("tool", sorted(_TOOLS))
def test_no_tool_reads_a_file_parse_tree_does_not_read(tool, shape):
    rc, text = _run(tool, shape)
    rc2_ok = tool in _RC2_VERDICT and _RC2_VERDICT[tool] in text
    assert (rc != 2 or rc2_ok) and "Traceback" not in text and _NOT_SELECTED not in text, (
        f"{tool} on {shape!r}: rc {rc} — a file parse-tree lists in `not_read` reached "
        f"its read (NotSelected), or it crashed:\n{text[-3000:]}")
    assert MARK not in text, (
        f"{tool} on {shape!r}: the content of a `not_read` file reached the output — "
        f"the tool read a file no reader reads:\n"
        + "\n".join(line for line in text.splitlines() if MARK in line)[:3000])


@pytest.mark.usefixtures("da_guard_env")
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_generator_under_the_cache_skips_what_parse_tree_skips(shape, tmp_path):
    """The generator container reads through a `ParseCache` and never runs
    tree mode: its verdict on which carrier is not read (file mode,
    `ConfdReader._verdict`) must be the CLI's (tree mode)."""
    for p in (TOOLS, TOOLS / "ops"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    import _grar_parse
    import _lib_parse_cache

    _write_tree(tmp_path, shape)

    def unselected(cache):
        previous = _lib_parse_cache.install(cache)
        try:
            tree = _grar_parse.load_tenant_tree(
                str(tmp_path), reader=_lib_parse_cache.ConfdReader(tmp_path))
        finally:
            _lib_parse_cache.install(previous)
        return sorted(f.file for f in tree.unselected_carriers)

    cli = unselected(None)
    assert set(cli) == _EXPECTED_NOT_READ[shape]
    cache = _lib_parse_cache.ParseCache()
    assert unselected(cache) == cli
    assert cache.parses > 0  # it did read through the cache
