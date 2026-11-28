"""#2989：`_routing_enforced.match` 只接受 list（Alertmanager matcher 字串的清單）。

schema（`definitions.routingEnforced.match`，`type: array`）與文件只記載 list 寫法
（`match: ['severity="critical"']`）。寫成 `routes[i].match` 的 mapping 寫法
（`{severity: critical}`）、字串或數字時，產生器原本**默默丟掉**整個 `match`，NOC
路由因此收全部告警，`--validate --strict` 與 validate-config 都回 0。

現在：該區塊不啟用（與 `enabled` 不是布林同一套處理）——不產出 enforced route、
印 `WARN … skipping`（kind `routing_enforced_match_invalid`），`--validate` 與
validate-config 回 1；輸出模式照常產出、結束碼不變。較早的根目錄檔已啟用的區塊保留。
`match` 缺或 null 仍是「不加 matcher」。跨語言判定由
tests/shared/routing_policy_parity_matrix.json 的 `routing-enforced-match-*-2989`
四棵樹釘住；這裡釘 CLI 與各讀者的行為。租戶 id 都是 fixture 名稱（CLAUDE.md #9）。

#4368（檔尾 `TestMigratedLabel`）：生效中區塊的 `match` 有 matcher 寫在
`da_migrated`（`MIGRATED_LABEL`，只有 migrate-rule 的告警帶）上時，給
`routing_enforced_match_migrated`：WARN、`blocks: never`，路由照寫的產出。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
from _grar_parse import load_tenant_tree  # noqa: E402
from _grar_routes import _build_enforced_routes, enforced_match_invalid  # noqa: E402
from _grar_routes import MIGRATED_LABEL, enforced_match_migrated_label  # noqa: E402

# validate-config 的 profiles 列經 da-guard 讀租戶（conftest 以 `go build` 建出）。
pytestmark = pytest.mark.usefixtures("da_guard_env")

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_VC = REPO / "scripts" / "tools" / "ops" / "validate_config.py"

_TENANT = ("tenants:\n  t-a:\n    cpu_usage_percent: '85'\n    _routing:\n"
           "      receiver: {type: webhook, url: \"https://t-a.example.com/h\"}\n")
_LINE = "_routing_enforced in _noc.yaml: 'match' must be a list of Alertmanager matcher strings"


def _enforced(match: str, url: str = "https://noc.example.com/h") -> str:
    return (f"_routing_enforced:\n  enabled: true\n  match: {match}\n"
            f"  receiver: {{type: webhook, url: \"{url}\"}}\n")


def _tree(tmp_path: Path, noc: str, defaults_extra: str = "") -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(parents=True)
    (d / "_defaults.yaml").write_text("defaults:\n  cpu_usage_percent: 80\n" + defaults_extra,
                                      encoding="utf-8")
    (d / "_noc.yaml").write_text(noc, encoding="utf-8")
    (d / "t-a.yaml").write_text(_TENANT, encoding="utf-8")
    return d


def _gar(d: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300, check=False)


def _enforced_routes(d: Path) -> list[dict]:
    """The rendered enforced routes (receiver `platform-enforced*`)."""
    out = d.parent / "routes.yaml"
    p = _gar(d, "--output-alertmanager", "-o", str(out))
    assert p.returncode == 0, p.stdout + p.stderr
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    return [r for r in doc["route"]["routes"]
            if str(r.get("receiver", "")).startswith("platform-enforced")]


# Written and not a list: refused. Absent / null / a list: accepted.
_REFUSED = ["{severity: critical}", "{}", "'severity=\"critical\"'", "''", "42", "0", "yes",
            # a list element that is not a non-empty string (as yaml.v3 reads
            # it — an unquoted `yes` is the string "yes", `true` a boolean):
            # the render used to crash on these (TypeError in re.match)
            "[1]", "[~]", "[true]", "[{severity: critical}]", "['severity=\"critical\"', '']",
            # whitespace only (str.strip(); U+001C counts there): copied into
            # `matchers` verbatim before
            "[' ']", "[\"\\t\\x1c \"]"]
_ACCEPTED = ["~", "[]", "['severity=\"critical\"']"]


class TestRefused:
    @pytest.mark.parametrize("match", _REFUSED)
    def test_no_enforced_route_is_rendered(self, tmp_path, match):
        """The issue's case: before, `{severity: critical}` rendered a NOC route with no
        matcher (every alert)."""
        d = _tree(tmp_path, _enforced(match))
        assert _enforced_routes(d) == []

    @pytest.mark.parametrize("match", _REFUSED)
    def test_validate_fails_and_names_it(self, tmp_path, match):
        d = _tree(tmp_path, _enforced(match))
        for args in (("--validate",), ("--validate", "--strict")):
            p = _gar(d, *args)
            assert p.returncode == 1, (args, p.stdout + p.stderr)
            assert _LINE in p.stdout + p.stderr, (args, p.stdout + p.stderr)

    def test_output_still_exits_0(self, tmp_path):
        d = _tree(tmp_path, _enforced("{severity: critical}"))
        p = _gar(d, "--output-alertmanager", "--dry-run")
        assert p.returncode == 0, p.stdout + p.stderr
        assert _LINE in p.stderr

    def test_the_finding_carries_kind_file_field(self, tmp_path):
        d = _tree(tmp_path, _enforced("{severity: critical}"))
        got = load_tenant_tree(str(d))
        rows = [(w.kind, w.file, w.field, w.blocks) for w in got.schema_warnings
                if getattr(w, "kind", None) == "routing_enforced_match_invalid"]
        assert rows == [("routing_enforced_match_invalid", "_noc.yaml",
                         "_routing_enforced.match", "validate")]
        assert got.enforced_routing is None

    def test_validate_config_fails(self, tmp_path):
        d = _tree(tmp_path, _enforced("{severity: critical}"))
        p = subprocess.run([sys.executable, str(_VC), "--config-dir", str(d)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=300, check=False)
        assert p.returncode == 1, p.stdout + p.stderr
        assert _LINE in p.stdout, p.stdout

    def test_per_tenant_shape_is_refused_too(self, tmp_path):
        """`{{tenant}}` shape: refused before expansion, no per-tenant route."""
        d = _tree(tmp_path, _enforced("{tenant: \"{{tenant}}\"}",
                                      url="https://noc.example.com/{{tenant}}"))
        assert _enforced_routes(d) == []
        assert _gar(d, "--validate").returncode == 1

    def test_earlier_root_block_stays_in_force(self, tmp_path):
        """As for a non-boolean `enabled`: the later refused block does not replace an
        earlier enabled one (root `_` files in name order)."""
        d = _tree(tmp_path, _enforced("{severity: critical}", url="https://noc.example.com/b"),
                  defaults_extra=_enforced("['severity=\"critical\"']",
                                           url="https://noc.example.com/a"))
        routes = _enforced_routes(d)
        assert [r.get("matchers") for r in routes] == [['severity="critical"']]

    def test_the_builder_refuses_it_for_any_caller(self):
        """`_build_enforced_routes` itself never renders a route without its `match`."""
        for match in ({"severity": "critical"}, 'severity="critical"', [1], [None]):
            for url in ("https://noc.example.com/h", "https://noc.example.com/{{tenant}}"):
                block = {"enabled": True, "match": match,
                         "receiver": {"type": "webhook", "url": url}}
                routes, receivers, warnings = _build_enforced_routes(
                    block, {}, tenants={"t-a": {}})
                assert (routes, receivers) == ([], []), (match, url)
                assert [w.kind for w in warnings] == ["routing_enforced_match_invalid"]
                assert warnings[0].field == "_routing_enforced.match"


class TestAccepted:
    def test_a_disabled_block_is_not_judged(self, tmp_path):
        """`enabled: false` with a mapping `match`: nothing is rendered and nothing
        is said — the shape of a block that is off does not matter."""
        noc = _enforced("{severity: critical}").replace("enabled: true", "enabled: false")
        d = _tree(tmp_path, noc)
        assert _enforced_routes(d) == []
        p = _gar(d, "--validate", "--strict")
        assert p.returncode == 0, p.stdout + p.stderr
        assert "'match' must be" not in p.stdout + p.stderr
        got = load_tenant_tree(str(d))
        assert not [w for w in got.schema_warnings
                    if getattr(w, "kind", None) == "routing_enforced_match_invalid"]

    @pytest.mark.parametrize("match", _ACCEPTED)
    def test_rendered_without_a_line(self, tmp_path, match):
        d = _tree(tmp_path, _enforced(match))
        routes = _enforced_routes(d)
        assert len(routes) == 1, routes
        p = _gar(d, "--validate", "--strict")
        assert _LINE not in p.stdout + p.stderr
        assert p.returncode == 0, p.stdout + p.stderr

    def test_list_matchers_are_rendered(self, tmp_path):
        """The documented list form: an edit critical → warning changes the render."""
        crit = _enforced_routes(_tree(tmp_path / "a", _enforced("['severity=\"critical\"']")))
        warn = _enforced_routes(_tree(tmp_path / "b", _enforced("['severity=\"warning\"']")))
        assert [r.get("matchers") for r in crit] == [['severity="critical"']]
        assert [r.get("matchers") for r in warn] == [['severity="warning"']]


_SCHEMA = json.loads((REPO / "docs" / "schemas" / "tenant-config.schema.json")
                     .read_text(encoding="utf-8"))
_ENFORCED_SCHEMA = jsonschema.Draft7Validator(
    {"$ref": "#/definitions/routingEnforced", "definitions": _SCHEMA["definitions"]})

# (match value, the generator's verdict) — the schema must give the same one.
_SHAPES = [
    ({"severity": "critical"}, True), ({}, True), ("x", True), (0, True), (False, True),
    ([1], True), ([None], True), ([""], True), ([" "], True), (["\t\x1c\n"], True),
    ([{"severity": "critical"}], True), (['a="b"', True], True),
    (None, False), ([], False), (['severity="critical"'], False),
    (['severity="critical"', 'team="x"'], False), (['  a="b"  '], False),
]


@pytest.mark.parametrize("value,refused", _SHAPES, ids=repr)
def test_schema_agrees_with_the_generator(value, refused):
    """tenant-config schema `definitions.routingEnforced.match`: null passes (as the
    generator reads it: no matcher), a whitespace-only element is refused (#2989)."""
    block = {"enabled": True, "match": value,
             "receiver": {"type": "webhook", "url": "https://noc.example.com/h"}}
    assert _ENFORCED_SCHEMA.is_valid(block) is not refused, list(_ENFORCED_SCHEMA.iter_errors(block))
    assert enforced_match_invalid(block) is refused


@pytest.mark.parametrize("value,want", [
    ({"severity": "critical"}, True), ({}, True), ("x", True), (0, True), (False, True),
    ([1], True), ([None], True), ([""], True), ([{"severity": "critical"}], True),
    (['a="b"', True], True), ([" "], True), (["\t\x1c\n"], True),
    (['  a="b"  '], False),
    (None, False), ([], False), (['severity="critical"'], False),
    (['severity="critical"', 'team="x"'], False),
])
def test_enforced_match_invalid(value, want):
    assert enforced_match_invalid({"match": value}) is want
    assert enforced_match_invalid({}) is False
    assert enforced_match_invalid("not a block") is False


# ── #4368: a matcher on MIGRATED_LABEL in the enforced block in force ──────
_MIGRATED_KIND = "routing_enforced_match_migrated"
_MIGRATED_LINE = f"is a matcher on {MIGRATED_LABEL}"

# (match as YAML flow, the [index, operator] rows): every shape the
# Alertmanager loader (da-read, `--validate`) reads, plus a quoted name.
_MIGRATED_NAMED = [
    (f"['{MIGRATED_LABEL}=\"true\"']", [[0, "="]]),
    (f"['{MIGRATED_LABEL}=true']", [[0, "="]]),
    (f"['{{{MIGRATED_LABEL}=\"true\"}}']", [[0, "="]]),
    (f"['severity=\"critical\"', 'a=\"b\",{MIGRATED_LABEL}=\"true\"']", [[1, "="]]),
    (f"[' {MIGRATED_LABEL} = \"true\" ']", [[0, "="]]),
    (f"['{MIGRATED_LABEL}=\"\"']", [[0, "="]]),
    (f"['{MIGRATED_LABEL}!=\"true\"', '{MIGRATED_LABEL}!~\"t.*\"']", [[0, "!="], [1, "!~"]]),
    (f"['{MIGRATED_LABEL}=~\".+\"']", [[0, "=~"]]),
    (f"['\"{MIGRATED_LABEL}\"=\"true\"']", [[0, "="]]),
]
# The name elsewhere than a matcher's name: not judged.
_MIGRATED_NOT_NAMED = [
    "['severity=\"critical\"']", f"['x_{MIGRATED_LABEL}=\"true\"']",
    f"['{MIGRATED_LABEL}_x=\"true\"']", f"['alertname=\"{MIGRATED_LABEL}\"']",
    f"['alertname=\"a,{MIGRATED_LABEL}=b\"']",
]


def _migrated_rows(d: Path) -> list[tuple]:
    got = load_tenant_tree(str(d))
    return [(w.kind, w.severity, w.blocks, w.file, w.field) for w in got.schema_warnings
            if getattr(w, "kind", None) == _MIGRATED_KIND]


class TestMigratedLabel:
    @pytest.mark.parametrize("match,want", _MIGRATED_NAMED, ids=[m for m, _ in _MIGRATED_NAMED])
    def test_named_in_any_shape(self, tmp_path, match, want):
        """The issue's case and its other spellings: one warning per element."""
        d = _tree(tmp_path, _enforced(match))
        assert enforced_match_migrated_label(yaml.safe_load(_enforced(match))["_routing_enforced"]) \
            == [tuple(r) for r in want]
        assert _migrated_rows(d) == [
            (_MIGRATED_KIND, "warning", "never", "_noc.yaml", f"_routing_enforced.match[{i}]")
            for i, _op in want]

    @pytest.mark.parametrize("match", _MIGRATED_NOT_NAMED)
    def test_not_named(self, tmp_path, match):
        assert _migrated_rows(_tree(tmp_path, _enforced(match))) == []

    def test_rendered_as_written_and_never_blocks(self, tmp_path):
        """A warning: `--validate --strict` passes and names it; the route keeps the
        matcher (#4355: it is not mistaken for the migrated shadow route)."""
        written = f'{MIGRATED_LABEL}="true"'
        d = _tree(tmp_path, _enforced(f"['{MIGRATED_LABEL}=\"true\"']"))
        assert [r.get("matchers") for r in _enforced_routes(d)] == [[written]]
        p = _gar(d, "--validate", "--strict")
        assert p.returncode == 0, p.stdout + p.stderr
        assert f"_routing_enforced in _noc.yaml: match[0] {_MIGRATED_LINE} (=)" in p.stdout + p.stderr

    def test_judged_on_the_block_in_force(self, tmp_path):
        """Root `_` files in name order, the later enabled block in force: an earlier
        one replaced is not judged; the later one is, under its own file."""
        named = _enforced(f"['{MIGRATED_LABEL}=\"true\"']")
        clean = _enforced("['severity=\"critical\"']")
        assert _migrated_rows(_tree(tmp_path / "a", clean, defaults_extra=named)) == []
        assert [r[3] for r in _migrated_rows(_tree(tmp_path / "b", named, defaults_extra=clean))] \
            == ["_noc.yaml"]

    def test_tenant_shape_is_judged(self, tmp_path):
        d = _tree(tmp_path, _enforced(f"['{MIGRATED_LABEL}=\"true\"']",
                                      url="https://noc.example.com/{{tenant}}"))
        assert [r[4] for r in _migrated_rows(d)] == ["_routing_enforced.match[0]"]

    def test_disabled_or_refused_block_is_not_judged(self, tmp_path):
        """Off: nothing said. A refused `match` (#2989) is that finding alone."""
        named = _enforced(f"['{MIGRATED_LABEL}=\"true\"']")
        assert _migrated_rows(_tree(tmp_path / "a", named.replace("enabled: true",
                                                                  "enabled: false"))) == []
        refused = _enforced(f"['{MIGRATED_LABEL}=\"true\"', 1]")
        got = load_tenant_tree(str(_tree(tmp_path / "b", refused)))
        assert [w.kind for w in got.schema_warnings if getattr(w, "kind", None) in
                (_MIGRATED_KIND, "routing_enforced_match_invalid")] == ["routing_enforced_match_invalid"]
