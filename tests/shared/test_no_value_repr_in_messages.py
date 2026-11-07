"""#1560 ⑤-b（第 3 輪）：conf.d 的值只經兩個出口進訊息。

route generator 的訊息會進 stderr、`--findings-json` 與 tenant-api 的 log。
一個 receiver 欄位的值經 `_lib_validation.receiver_value_repr`（憑證位置印
`<masked>`），其他 conf.d 的值經 `_lib_validation.scalar_text`（純量照印，
list／mapping 只印型別與長度：容器可能裝著 receiver）。

這支測試掃 `scripts/tools/_lib_validation.py` 與 `scripts/tools/ops/_grar_*.py`：
凡是 f-string 的 `!r`、`repr(...)` 呼叫、字串裡的 `%r`，都必須在下面的白名單裡，
每一條附理由（為什麼它不是 conf.d 的值，或為什麼可以照印）。新增一個 `!r`，
這裡先轉紅：改走上面兩個出口，或把它連同理由加進白名單。白名單裡對不到
程式的條目也會轉紅，名單不會腐爛。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FILES = [REPO / "scripts" / "tools" / "_lib_validation.py",
         *sorted((REPO / "scripts" / "tools" / "ops").glob("_grar_*.py"))]

_ARGV = "a command-line argument of the tool, not a conf.d value"
_POLICY = ("the deployment --policy file (kept outside the tenant repository, "
           "ADR-036 §5), not a conf.d value")
_SCHEMA = "the tenant-config schema shipped with the tool, not a conf.d value"

#: (file name, enclosing function, kind, expression) → why it may be quoted.
ALLOWED: dict[tuple[str, str, str, str], str] = {
    ("_lib_validation.py", "receiver_value_repr", "repr(", "value"):
        "THE receiver-value exit: masks every credential path first",
    ("_lib_validation.py", "scalar_text", "repr(", "value"):
        "THE other exit: reached for scalars only, containers print type and length",
    ("_lib_validation.py", "positive_seconds", "!r", "value"): _ARGV,
    ("_lib_validation.py", "non_negative_seconds", "!r", "value"): _ARGV,
    ("_lib_validation.py", "positive_int", "!r", "value"): _ARGV,
    ("_lib_validation.py", "positive_int_seconds", "!r", "value"): _ARGV,
    ("_lib_validation.py", "secret_ref_rule", "!r", "exc"): _SCHEMA,
    ("_lib_validation.py", "_receiver_field_schemas", "!r", "alts"): _SCHEMA,
    ("_lib_validation.py", "_receiver_field_schemas", "!r", "ref"): _SCHEMA,
    ("_grar_merge.py", "__new__", "!r", "blocks"):
        "a Finding's blocks argument, set by the generator's own code",
    ("_grar_merge.py", "_destination_problem", "!r", "host"):
        "the host parsed out of a destination URL (userinfo, path and query "
        "dropped) — what allowed_domains names, so the operator can fix the list",
    ("_grar_render.py", "root_receiver_without_integration_warning", "!r", "safe_label(name)"):
        "a receiver name (generated, or the operator's --base-config), never a field value",
    ("_grar_render.py", "custom_firehose_findings", "!r", "safe_label(u.tenant)"):
        "a tenant id, a key (ADR-035 DNS-1123 label)",
    ("_grar_render.py", "custom_firehose_findings", "!r", "CUSTOM_FIREHOSE_RECEIVER"):
        "a constant of the generator",
    ("_grar_render.py", "watchdog_receiver_findings", "!r", "WATCHDOG_RECEIVER"):
        "a constant of the generator",
    ("_grar_render.py", "load_base_config", "!r", "path"):
        "the --base-config path argument, not its content",
    ("_grar_render.py", "_run_binary", "!r", "argv[0]"):
        "the program the tool runs (amtool, da-guard), not a conf.d value",
    ("_grar_validate.py", "_load_shared_secrets", "!r", "policy_path"): _POLICY,
    ("_grar_validate.py", "_load_shared_secrets", "!r", "alias"): _POLICY,
    ("_grar_validate.py", "_load_shared_secrets", "!r", "name"): _POLICY,
    ("_grar_validate.py", "_load_shared_secrets", "!r", "OWN_NAME_PREFIX"):
        "a constant of the generator",
    ("_grar_validate.py", "load_policy_document", "!r", "policy_path"): _POLICY,
    ("_grar_validate.py", "load_policy_document", "!r", "bad"): _POLICY,
    ("_grar_validate.py", "load_policy_document", "!r", "minimum"): _POLICY,
    ("_grar_validate.py", "generator_version_problem", "!r", "own"):
        "the tool's own version",
    ("_grar_validate.py", "invalid_tenant_id_text", "!r", "str(tenant)"):
        "a declared tenant id: a key of conf.d, not a value (keys are named in "
        "every finding; the tenant must see which id is refused)",
}


def _hits(path: Path) -> list[tuple[str, str, str, str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                owner.setdefault(id(node), fn.name)
    out = []
    for node in ast.walk(tree):
        kind = what = None
        if isinstance(node, ast.FormattedValue) and node.conversion == ord("r"):
            kind, what = "!r", ast.unparse(node.value)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "repr"):
            kind, what = "repr(", ast.unparse(node.args[0]) if node.args else ""
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "%r" in node.value:
            kind, what = "%r", node.value
        if kind:
            out.append((path.name, owner.get(id(node), "<module>"), kind, what, node.lineno))
    return out


def test_every_repr_is_one_of_the_allowed_exits():
    found = [h for f in FILES for h in _hits(f)]
    assert found, "the scan found nothing: it is not reading the files"
    unknown = [f"{h[0]}:{h[4]} {h[1]}: {h[2]} {h[3]}" for h in found if h[:4] not in ALLOWED]
    assert not unknown, (
        "a value is quoted into a message outside the two exits (receiver_value_repr, "
        "scalar_text): route it through one, or allow it below with the reason:\n  "
        + "\n  ".join(unknown))


def test_every_allowed_entry_is_still_in_the_code():
    found = {h[:4] for f in FILES for h in _hits(f)}
    stale = sorted(set(ALLOWED) - found)
    assert not stale, f"allowlist entries no code has any more: {stale}"


def test_every_allowed_entry_says_why():
    assert all(isinstance(why, str) and len(why.strip()) > 10 for why in ALLOWED.values())
