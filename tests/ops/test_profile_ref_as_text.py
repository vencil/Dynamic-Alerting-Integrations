"""A tenant's `_profile:` names the profile its source TEXT names (#2297).

The exporter reads `_profile` as text (`ScheduledValue` keeps it), and keys
`profiles:` by the key's text: `_profile: 010` binds profile `010`. PyYAML's
YAML 1.1 typing reads the plain `010` as the int 8 — so before #2297 the
Python readers below each disagreed with the exporter on the same tree:
`describe_tenant` served the chain's value (no profile), `check_profiles`
skipped the int (a reference to a profile that does not exist passed),
`config_diff`'s profile refs and `diagnose`'s lookup / inheritance chain
lost the tenant's profile.

The oracle is the exporter's EFFECTIVE value (`da-guard served-values`:
`container_cpu` 55 from the profile, 80 from the defaults), not a profile
name — a reader that agrees on the name but not on what it applies is still
wrong. The fixture for `da_guard` follows tests/ops/test_tenant_id_as_text.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "scripts" / "tools"

import _lib_tenant_values  # noqa: E402
import config_diff  # noqa: E402
import diagnose  # noqa: E402
import validate_config  # noqa: E402

# #2115 0-B／B3: validate-config 的 profiles 列經 da-guard 讀租戶，每次都需要 da-guard
# （conftest 以 `go build` 建出；建不起來就 fail、不 skip）。
pytestmark = pytest.mark.usefixtures("da_guard_env")

_PROFILE_CPU = 55.0
_DEFAULT_CPU = 80.0

# (the tenant's `_profile:` as written, the profile name the exporter reads)
_REFS = [
    ("010", "010"),      # plain: PyYAML's int 8
    ("'010'", "010"),    # quoted: text everywhere already
    ("std", "std"),      # control: a name PyYAML reads as text anyway
    ("123", "123"),      # plain int naming NO profile
    ("'123'", "123"),    # its quoted twin
]
# The profile key as written in `_profiles.yaml`: both spellings name `010`.
_PROFILE_KEYS = ["'010'", "010"]


@pytest.fixture(scope="module")
def da_guard(tmp_path_factory):
    """da-guard built from this checkout, or None when `go` is missing (the
    test then skips; VIBE_REQUIRE_GO=1 fails instead)."""
    if shutil.which("go") is None:
        assert os.environ.get("VIBE_REQUIRE_GO") != "1", \
            "VIBE_REQUIRE_GO=1 but `go` is not on PATH"
        return None
    out = tmp_path_factory.mktemp("da-guard") / "da-guard"
    env = {**os.environ}
    env.setdefault("GOTOOLCHAIN", "auto")
    subprocess.run(["go", "build", "-buildvcs=false", "-o", str(out),
                    "./cmd/da-guard"],
                   cwd=REPO_ROOT / "components" / "threshold-exporter" / "app",
                   env=env, check=True, timeout=600)
    return str(out)


def _tree(root: Path, ref: str, profile_key: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "_defaults.yaml").write_text(
        "defaults:\n  container_cpu: 80\n", encoding="utf-8")
    (root / "_profiles.yaml").write_text(
        f"profiles:\n  {profile_key}:\n    container_cpu: 55\n"
        "  std:\n    container_cpu: 55\n", encoding="utf-8")
    (root / "tx.yaml").write_text(
        f"tenants:\n  tx:\n    _profile: {ref}\n", encoding="utf-8")
    return root


def _served_cpu(conf_d: Path, da_guard) -> float:
    if da_guard is None:
        pytest.skip("go not on PATH: the oracle is da-guard "
                    "(set VIBE_REQUIRE_GO=1 to fail instead)")
    served = _lib_tenant_values.load_served_values(conf_d, binary=da_guard)
    return float(served["tx"].values["container_cpu"])


_CASES = [pytest.param(ref, name, pkey, id=f"_profile={ref}|key={pkey}")
          for ref, name in _REFS for pkey in _PROFILE_KEYS]


@pytest.mark.parametrize("ref,name,pkey", _CASES)
def test_every_reader_binds_the_profile_the_exporter_applies(
        tmp_path, da_guard, ref, name, pkey):
    d = _tree(tmp_path / "conf.d", ref, pkey)
    served = _served_cpu(d, da_guard)
    bound = served == _PROFILE_CPU
    # The fixture's premise: a reference to 010/std binds, one to 123 does not.
    assert bound == (name in ("010", "std")), (served, ref, pkey)

    # describe_tenant: the effective value, as the exporter serves it.
    p = subprocess.run(
        [sys.executable, str(TOOLS / "dx" / "describe_tenant.py"), "tx",
         "-c", str(d), "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 0, p.stderr[-2000:]
    doc = json.loads(p.stdout)
    eff = doc.get("effective_config", doc)
    assert float(eff["container_cpu"]) == served, eff
    assert eff["_profile"] == name, eff

    # validate_config.check_profiles: a bound ref passes and is counted; an
    # unbound one — plain int included — is an unknown profile.
    res = validate_config.check_profiles(str(d))
    if bound:
        assert res["status"] == validate_config.PASS, res
        assert "1 profile refs" in res["details"][0], res
    else:
        assert res["status"] == validate_config.WARN, res
        assert (f'tenant=tx: _profile references unknown profile "{name}"'
                in res["details"]), res

    # config_diff: the ref is reported under the name, and that name is a
    # key of the profiles it reads (so the impact report can join them).
    assert config_diff.load_tenant_profile_refs(str(d)) == {name: ["tx"]}
    assert (name in config_diff.load_profiles_from_dir(str(d))) == bound
    assert config_diff.load_settings_from_dir(str(d)) == {"tx": {"_profile": name}}

    # diagnose: the lookup names it; the chain has a profile layer iff bound.
    assert diagnose.lookup_tenant_profile("tx", str(d)) == name
    chain = diagnose.resolve_inheritance_chain("tx", str(d))
    layers = [layer["source"] for layer in chain["chain"]]
    assert (f"_profiles.yaml → {name}" in layers) == bound, chain
    assert float(chain["resolved"]["container_cpu"]) == served, chain


def _outcome(load, d: Path):
    """What a loader does with `d`: its result, or the exception it raises
    (type and the file it names) — a copy that raises where the original
    returns, or the reverse, differs here."""
    try:
        return ("ok", load(str(d)))
    except Exception as exc:  # noqa: BLE001 — compared, not handled
        return ("raises", type(exc).__name__, getattr(exc, "path", None))


# One conf.d per shape: a file that raises would hide the others' results.
_LOADER_SHAPES = {
    "mixed": {
        "_defaults.yaml": "defaults:\n  a: 1\n",
        ".hidden.yaml": "tenants:\n  h:\n    a: 1\n",
        "w.yaml": "tenants:\n  010:\n    a: 2\n  yes:\n    a: 3\n",
        "flat.YML": "a: 4\n_profile: std\n",
        "empty.yaml": "",
    },
    # The shared loader reads ONE document: a second one is a YAML error,
    # not "take the first".
    "multi-document": {"m.yaml": "tenants:\n  m1:\n    a: 1\n---\ntenants:\n  m2:\n    a: 2\n"},
    # A tenant whose value is not a mapping is dropped, not kept.
    "non-mapping-tenant": {"n.yaml": "tenants:\n  a: 1\n  b:\n    x: 2\n"},
    # `tenants:` that is not a mapping: the file is a flat tenant named
    # after it, `tenants` key and all.
    "tenants-list": {"l.yaml": "tenants: []\n"},
}


@pytest.mark.parametrize("shape", sorted(_LOADER_SHAPES))
def test_config_diff_profile_loader_lists_the_shared_loaders_tenants(tmp_path, shape):
    """`config_diff`'s `_profile`-as-text loader copies the shared loader's
    walk (it may not change that loader, #2115): the same result — or the
    same error — for every shape of file that walk distinguishes."""
    d = tmp_path / "conf.d"
    d.mkdir()
    for name, body in _LOADER_SHAPES[shape].items():
        (d / name).write_text(body, encoding="utf-8")
    own = _outcome(config_diff._load_tenant_configs_profile_text, d)
    shared = _outcome(config_diff._load_tenant_configs_raw, d)
    assert own == shared, (own, shared)


def test_config_diff_sees_a_switch_between_spellings_of_one_int(tmp_path):
    """`_profile: 010` → `_profile: 8` switches the exporter from profile
    `010` to profile `8`; PyYAML reads both as 8, so the settings diff
    printed no change."""
    old = _tree(tmp_path / "old", "010", "'010'")
    new = _tree(tmp_path / "new", "8", "'010'")
    assert (config_diff.load_settings_from_dir(str(old))
            != config_diff.load_settings_from_dir(str(new)))


def _describe_json(*args) -> dict:
    p = subprocess.run(
        [sys.executable, str(TOOLS / "dx" / "describe_tenant.py"), *args,
         "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout)


def test_describe_what_if_on_the_unchanged_carrier_changes_nothing(tmp_path, da_guard):
    """`--what-if` with the root carrier itself, unchanged: the platform
    `tenants:` block's `_profile: 010` is read from the what-if document by
    `_load_platform_doc`. Read as PyYAML's 8 there (the baseline reads
    "010"), the simulation lost the profile and reported a change —
    `_profile` "010" → 8 and `container_cpu` 55 → 80 — for a file nobody
    edited. #2097 F1: the unchanged carrier goes in as a copy with
    `--replaces` (the carrier itself as `--what-if` is now rc 2)."""
    d = tmp_path / "conf.d"
    d.mkdir()
    carrier = d / "_defaults.yaml"
    carrier.write_text("defaults:\n  container_cpu: 80\n"
                       "tenants:\n  tx:\n    _profile: 010\n", encoding="utf-8")
    (d / "_profiles.yaml").write_text(
        "profiles:\n  010:\n    container_cpu: 55\n", encoding="utf-8")
    (d / "tx.yaml").write_text("tenants:\n  tx:\n    mysql_connections: 5\n",
                               encoding="utf-8")
    assert _served_cpu(d, da_guard) == _PROFILE_CPU   # the fixture binds 010
    copy = tmp_path / "copy.yaml"
    copy.write_text(carrier.read_text(encoding="utf-8"), encoding="utf-8")
    got = _describe_json("tx", "-c", str(d), "--what-if", str(copy),
                         "--replaces", str(carrier))
    assert got["substitution_type"] == "substitute", got
    assert got["changed_keys"] == {}, got
    assert got["added_keys"] == {} and got["removed_keys"] == {}, got
    assert got["merged_hash_changed"] is False, got


@pytest.mark.parametrize("ref", ["'010 '", "|\n      010"],
                         ids=["trailing-space", "block-scalar"])
def test_config_diff_profile_refs_strip_the_name(tmp_path, da_guard, ref):
    """The exporter's `profileNameOf` trims the name, as describe_tenant,
    diagnose and validate_config do: `'010 '` and a block scalar (`"010\\n"`)
    bind profile 010. config_diff's refs reported `'010 '` / `'010\\n'`, a
    name no profile has, so the impact report lost the tenant."""
    d = _tree(tmp_path / "conf.d", ref, "'010'")
    assert _served_cpu(d, da_guard) == _PROFILE_CPU
    assert diagnose.lookup_tenant_profile("tx", str(d)) == "010"
    assert validate_config.check_profiles(str(d))["status"] == validate_config.PASS
    assert config_diff.load_tenant_profile_refs(str(d)) == {"010": ["tx"]}
