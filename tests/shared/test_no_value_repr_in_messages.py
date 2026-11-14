"""#1560 ⑤-b：conf.d 的值進訊息只經一個出口——AST 守衛（輔助）。

⚠️ 這支**只攔 repr 類寫法**（f-string 的 `!r`、`repr(...)`、字串裡的 `%r`）。
f-string 直接插值（`'{val}'`）它抓不到，用字串述詞守「值不進訊息」已記為
死路（第 3 輪審查 F-B）。**主守衛是動態 canary 矩陣**
`tests/ops/test_routing_value_canary_matrix.py`：由 schema 推導 routing 的每個鍵、
在每種位置塞 canary、跑產生器，照行為斷言 canary 不出現。

出口是 `_lib_validation.shown_value`（`receiver_value_repr`、`scalar_text`、
`value_text` 都經它）。這裡掃 `scripts/tools/_lib_validation.py` 與
`scripts/tools/ops/_grar_*.py`，每個 repr 類寫法都必須在下面的白名單裡並附理由；
白名單裡對不到程式的條目也會轉紅。
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
    ("_lib_validation.py", "shown_value", "repr(", "value"):
        "THE exit: a credential path is masked first, a container prints type and "
        "length, a string only when it is name-like",
    # F-A (round 4): the URL flags of the tools — every value is the person's
    # own command-line argument or environment variable, echoed back to them.
    ("_lib_validation.py", "url_problem", "!r", "bad"):
        "one character of a URL the person passed on the command line or in the "
        "environment (whitespace / control / non-ASCII), not a conf.d value",
    ("_lib_validation.py", "_one", "!r", "value"):
        "a URL the person passed on the command line / in the environment (argparse "
        "type=), echoed to them; not a conf.d value",
    ("_lib_validation.py", "__call__", "!r", "value"):
        "the same argparse type= URL list item, the person's own argv / env",
    ("_lib_validation.py", "__call__", "!r", "self.separator"):
        "the separator a tool's code passes to url_list_type, a constant",
    ("_lib_validation.py", "__init__", "!r", "empty"):
        "the empty= mode a tool's code passes to url_type, a constant",
    ("_lib_validation.py", "__repr__", "!r", "self.empty"):
        "UrlArg's own repr: the empty= mode, a constant of the calling tool",
    ("_lib_validation.py", "__repr__", "!r", "self.separator"):
        "UrlArg's own repr: the separator, a constant of the calling tool",
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
        "the program the tool runs (kubectl, curl), not a conf.d value",
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
        "a value is quoted into a message outside the exit (shown_value): route it "
        "through it, or allow it below with the reason:\n  "
        + "\n  ".join(unknown))


def test_every_allowed_entry_is_still_in_the_code():
    found = {h[:4] for f in FILES for h in _hits(f)}
    stale = sorted(set(ALLOWED) - found)
    assert not stale, f"allowlist entries no code has any more: {stale}"


def test_every_allowed_entry_says_why():
    assert all(isinstance(why, str) and len(why.strip()) > 10 for why in ALLOWED.values())
