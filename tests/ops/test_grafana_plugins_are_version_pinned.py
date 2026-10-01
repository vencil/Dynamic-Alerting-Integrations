"""Every Grafana plugin we install is pinned to an exact version (TRK-2605).

Until TRK-2605 the Grafana Deployment set
`GF_INSTALL_PLUGINS=victoriametrics-logs-datasource`, with no version, so every
boot pulled whatever grafana.com served that day — the one residual risk in
platform-log-aggregation-runbook.md §7.6 T5 after #2594 made third-party images
digest-pinned by rule. Grafana 12.4 also installs a few app plugins of its own
(`defaultPreinstallPlugins` in pkg/setting/setting_plugins.go), unpinned and
auto-updated, unless they are disabled.

Measured against the grafana/grafana v12.4.12 source:

* `GF_INSTALL_PLUGINS` is deprecated: the image's run.sh warns, and the server
  migrates it to `preinstall_sync` (version syntax `id 1.2.3`).
* `GF_PLUGINS_PREINSTALL_SYNC` / `GF_PLUGINS_PREINSTALL` take `id@version`; a
  pinned entry is installed at exactly that version and never auto-updated, an
  entry without a version is installed at latest and updated on every start.
* `id@version@url` installs from an arbitrary URL instead of the catalog. The
  catalog path checks the zip against the sha256 grafana.com publishes; the URL
  path has no such checksum, so it is rejected here as well.

The rules below, over every Grafana container in `k8s/**` and
`try-local/docker-compose.yaml`:

0. the env is readable: no envFrom / env_file, no `GF_*__FILE` install key;
1. no `GF_INSTALL_PLUGINS` (deprecated, and its syntax is not checked here);
2. every `GF_PLUGINS_PREINSTALL[_SYNC]` entry is `<id>@<X.Y.Z>`, literal value;
3. Renovate tracks every pinned entry (the grafana.com catalog datasource in
   renovate.json), so a pin is not a dead pin;
4. each k8s Grafana disables Grafana's own unpinned default preinstalls, and the
   Grafana minor is the one that list was taken from;
5. every non-core datasource type the k8s provisioning ConfigMap declares is a
   pinned plugin. This is the vacuity floor, and it does not come from the env
   it checks: drop the env (or the whole install point) and the provisioned
   VictoriaLogs datasource has no plugin behind it, which goes red here.

⛔ SCOPE — deliberately NOT covered:

* try-local's Grafana is not required to disable the default preinstalls: its
  images are tag-only on purpose (test_trylocal_compose_pins.py) and nothing it
  installs is deployed. Rules 1–3 still apply to it.
* A plugin baked into a custom image, or installed through grafana.ini
  `[plugins] preinstall`, is not parsed. The tripwire test makes the second one
  (and any install key in a file this module does not parse) go red instead of
  being missed.
* The plugin's content. Grafana verifies the catalog sha256 and the plugin
  signature; this module checks only that the version is fixed.
"""
from __future__ import annotations

import json
import re

import yaml
from _tree import REPO_ROOT as REPO
from _tree import repo_files

K8S = REPO / "k8s"
TRYLOCAL_COMPOSE = REPO / "try-local" / "docker-compose.yaml"
PROVISIONING_CM = K8S / "03-monitoring" / "configmap-grafana.yaml"
RENOVATE_JSON = REPO / "renovate.json"

# grafana/grafana and its -oss / -enterprise variants, under any registry or
# mirror prefix (`docker.io/grafana/grafana`, `mirror.example/grafana/grafana`).
_GRAFANA_REPO = re.compile(r"(?:^|/)grafana/grafana(?:-oss|-enterprise)?$")
DEPRECATED_ENV = "GF_INSTALL_PLUGINS"
PREINSTALL_ENVS = ("GF_PLUGINS_PREINSTALL", "GF_PLUGINS_PREINSTALL_SYNC")
DISABLE_ENV = "GF_PLUGINS_DISABLE_PLUGINS"

# `<plugin id>@<X.Y.Z>` and nothing after it (no `@url`). Grafana plugin ids
# are lowercase `org-name-type`.
_PINNED = re.compile(r"[a-z0-9][a-z0-9-]*@[0-9]+\.[0-9]+\.[0-9]+")

# grafana/grafana v12.4.12, pkg/setting/setting_plugins.go L35-L40. Two more
# (grafana-advisor-app, grafana-pathfinder-app) are added only behind feature
# toggles that default to false in that release. A new Grafana minor can change
# this list, so rule 4 also pins the minor it was read from: bumping Grafana to
# another minor goes red until someone re-reads the list for that release.
DEFAULT_PREINSTALLS = {
    "grafana-lokiexplore-app",
    "grafana-pyroscope-app",
    "grafana-exploretraces-app",
    "grafana-metricsdrilldown-app",
}
DEFAULTS_READ_FROM_MINOR = "12.4"

# Datasource types built into Grafana (no plugin install needed). Only the ones
# this repo could plausibly provision; an unlisted core type reads as a plugin
# and fails rule 5 loudly, which is the safe direction.
CORE_DATASOURCE_TYPES = {
    "prometheus", "alertmanager", "loki", "tempo", "jaeger", "zipkin",
    "elasticsearch", "graphite", "influxdb", "mysql", "postgres", "mssql",
    "cloudwatch", "testdata",
}

# Keys that install plugins, as they appear in any text file: env names (also
# the `__FILE` form, which the image's run.sh reads from a file), and the
# grafana.ini `[plugins]` keys (go-ini accepts both `=` and `:`).
_INSTALL_KEY = re.compile(
    r"\bGF_(?:INSTALL_PLUGINS|PLUGINS_PREINSTALL(?:_SYNC)?)(?:__FILE)?\b"
    r"|^[ \t]*preinstall(?:_sync)?[ \t]*[=:]", re.M)
_TRIPWIRE_ROOTS = ("k8s/", "helm/", "try-local/")
# Whole-line YAML / shell / ini comments: a key named in prose installs nothing.
_COMMENT_LINE = re.compile(r"^[ \t]*[#;].*$", re.M)


# ── discovery ──────────────────────────────────────────────────────────────

def _containers(node):
    """Yield every container dict under a parsed k8s object."""
    if isinstance(node, dict):
        for key in ("containers", "initContainers"):
            for c in node.get(key) or []:
                if isinstance(c, dict):
                    yield c
        for v in node.values():
            yield from _containers(v)
    elif isinstance(node, list):
        for v in node:
            yield from _containers(v)


def _is_grafana(image) -> bool:
    if not isinstance(image, str):
        return False
    repo = image.split("@")[0]
    if ":" in repo.rsplit("/", 1)[-1]:          # a tag, not a registry port
        repo = repo[:repo.rfind(":")]
    return bool(_GRAFANA_REPO.search(repo))


def _opaque_env_sources(spec: dict) -> list[str]:
    """Env sources whose variable names are not in the spec itself: k8s
    `envFrom`, compose `env_file`."""
    return [key for key in ("envFrom", "env_file") if spec.get(key)]


def _k8s_grafanas() -> tuple[list[dict], list[str]]:
    """Grafana containers in k8s/**, as {file, image, env, opaque}. env maps a
    name to its literal value, or to None when it is set through valueFrom;
    opaque lists env sources whose names this module cannot see (envFrom)."""
    found, unparsable = [], []
    for path in sorted(repo_files(".yaml", ".yml")):
        rel = path.relative_to(REPO).as_posix()
        if not rel.startswith("k8s/"):
            continue
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError as exc:
            unparsable.append(f"{rel}: {exc}")
            continue
        for doc in docs:
            for c in _containers(doc):
                if _is_grafana(c.get("image")):
                    env = {e["name"]: (e.get("value") if "valueFrom" not in e else None)
                           for e in c.get("env") or [] if isinstance(e, dict) and "name" in e}
                    found.append({"file": rel, "image": c["image"], "env": env,
                                  "opaque": _opaque_env_sources(c)})
    return found, unparsable


def _trylocal_grafanas() -> list[dict]:
    compose = yaml.safe_load(TRYLOCAL_COMPOSE.read_text(encoding="utf-8"))
    out = []
    for svc in (compose.get("services") or {}).values():
        if not _is_grafana(svc.get("image")):
            continue
        raw = svc.get("environment") or {}
        if isinstance(raw, list):
            raw = dict(item.split("=", 1) if "=" in item else (item, None) for item in raw)
        out.append({"file": TRYLOCAL_COMPOSE.relative_to(REPO).as_posix(),
                    "image": svc["image"], "env": {k: v for k, v in raw.items()},
                    "opaque": _opaque_env_sources(svc)})
    return out


def _all_grafanas() -> list[dict]:
    k8s, unparsable = _k8s_grafanas()
    assert not unparsable, "k8s manifests this module could not parse:\n" + "\n".join(unparsable)
    return k8s + _trylocal_grafanas()


def _pinned_entries(grafanas: list[dict]) -> list[tuple[str, str, str]]:
    """(file, env name, entry) for every PREINSTALL entry."""
    out = []
    for g in grafanas:
        for name in PREINSTALL_ENVS:
            value = g["env"].get(name)
            if isinstance(value, str):
                out += [(g["file"], name, e.strip()) for e in value.split(",") if e.strip()]
    return out


def _provisioned_plugin_types() -> set[str]:
    cm = yaml.safe_load(PROVISIONING_CM.read_text(encoding="utf-8"))
    ds = yaml.safe_load(cm["data"]["datasources.yaml"])
    return {d["type"] for d in ds.get("datasources") or []} - CORE_DATASOURCE_TYPES


# ── rules ──────────────────────────────────────────────────────────────────

def test_no_deprecated_install_plugins_env() -> None:
    hits = [g["file"] for g in _all_grafanas() if DEPRECATED_ENV in g["env"]]
    assert not hits, (
        f"{DEPRECATED_ENV} is deprecated in Grafana 12.x and installs latest when no "
        f"version is given; use GF_PLUGINS_PREINSTALL_SYNC=<id>@<version>: {hits}")


def test_grafana_env_is_readable() -> None:
    """Rules 1–3 read the container's literal env. A source they cannot read
    (envFrom / env_file, or a `GF_*__FILE` install key, whose value is a path
    run.sh reads at start) would carry an unpinned install past all of them."""
    bad = []
    for g in _all_grafanas():
        bad += [f"{g['file']}: {src}" for src in g["opaque"]]
        bad += [f"{g['file']}: {name}" for name in g["env"]
                if name.endswith("__FILE") and _INSTALL_KEY.fullmatch(name)]
    assert not bad, (
        "Grafana env this module cannot check for plugin installs:\n"
        + "\n".join(f"  - {b}" for b in bad)
        + "\nSet plugin installs as a literal `env` value instead.")


def test_every_preinstall_entry_is_pinned() -> None:
    bad = []
    for g in _all_grafanas():
        for name in PREINSTALL_ENVS:
            if name in g["env"] and g["env"][name] is None:
                bad.append(f"{g['file']}: {name} uses valueFrom; the version cannot be checked")
    bad += [f"{f}: {name}={entry!r}" for f, name, entry in _pinned_entries(_all_grafanas())
            if not _PINNED.fullmatch(entry)]
    assert not bad, (
        "Grafana plugin install entries without an exact version (expected "
        "`<plugin id>@<X.Y.Z>`, no `@url`):\n" + "\n".join(f"  - {b}" for b in bad))


def _renovate_plugin_deps() -> set[tuple[str, str, str]]:
    cfg = json.loads(RENOVATE_JSON.read_text(encoding="utf-8"))
    deps = set()
    for mgr in cfg.get("customManagers", []):
        if mgr.get("datasourceTemplate") != "custom.grafana-plugins":
            continue
        file_rx = [re.compile(p[1:-1]) for p in mgr["managerFilePatterns"]]
        rxs = [re.compile(re.sub(r"\(\?<([a-zA-Z]\w*)>", r"(?P<\1>", s)) for s in mgr["matchStrings"]]
        for path in repo_files():
            rel = path.relative_to(REPO).as_posix()
            if any(r.search(rel) for r in file_rx):
                text = path.read_text(encoding="utf-8")
                for rx in rxs:
                    deps |= {(rel, m["depName"], m["currentValue"]) for m in rx.finditer(text)}
    return deps


def test_renovate_tracks_every_pinned_plugin() -> None:
    cfg = json.loads(RENOVATE_JSON.read_text(encoding="utf-8"))
    url = cfg.get("customDatasources", {}).get("grafana-plugins", {}).get("defaultRegistryUrlTemplate", "")
    assert url.startswith("https://grafana.com/api/plugins/{{packageName}}/versions"), url
    # JSONata turns a one-item `items.{...}` into an object, not an array, and
    # Renovate's custom datasource then rejects the result — a plugin with a
    # single catalog version would become a dead pin with every rule here green.
    # JSONata does not run in this lane, so the array constructor is pinned
    # instead (measured with Renovate 41.173.1's bundled jsonata, TRK-2605).
    (transform,) = cfg["customDatasources"]["grafana-plugins"]["transformTemplates"]
    assert re.fullmatch(r'\{"releases": \[items\.\{.*\}\]\}', transform), transform
    pinned = {(f, *entry.split("@", 1)) for f, _, entry in _pinned_entries(_all_grafanas())
              if _PINNED.fullmatch(entry)}
    tracked = _renovate_plugin_deps()
    assert pinned == tracked, (
        f"\n  pinned but not tracked by Renovate (a dead pin): {sorted(pinned - tracked)}"
        f"\n  tracked but not a pinned entry:                  {sorted(tracked - pinned)}"
        "\nThe custom.grafana-plugins manager in renovate.json matches ONE entry per "
        "GF_PLUGINS_PREINSTALL_SYNC value, in k8s/03-monitoring/deployment-grafana.yaml.")


def test_k8s_grafana_disables_unpinned_default_preinstalls() -> None:
    grafanas, _ = _k8s_grafanas()
    assert grafanas, "no Grafana container found under k8s/"
    bad = []
    for g in grafanas:
        tag = g["image"].split("@")[0].rsplit(":", 1)[-1]
        if ".".join(tag.split(".")[:2]) != DEFAULTS_READ_FROM_MINOR:
            bad.append(f"{g['file']}: Grafana {tag}, but DEFAULT_PREINSTALLS was read from "
                       f"{DEFAULTS_READ_FROM_MINOR}.x — re-read defaultPreinstallPlugins in "
                       "pkg/setting/setting_plugins.go for the new release, then update both")
        disabled = {p.strip() for p in (g["env"].get(DISABLE_ENV) or "").split(",") if p.strip()}
        missing = DEFAULT_PREINSTALLS - disabled
        if missing:
            bad.append(f"{g['file']}: {DISABLE_ENV} misses {sorted(missing)} — Grafana "
                       "installs them unpinned and auto-updates them")
    assert not bad, "\n".join(bad)


def test_every_provisioned_plugin_datasource_is_pinned() -> None:
    types = _provisioned_plugin_types()
    assert types, (
        f"{PROVISIONING_CM.name} provisions no plugin datasource — the floor for the "
        "rules above is gone. If VictoriaLogs really left Grafana, drop this test too.")
    k8s, _ = _k8s_grafanas()
    pinned_ids = {e.split("@", 1)[0] for _, _, e in _pinned_entries(k8s) if _PINNED.fullmatch(e)}
    assert types <= pinned_ids, (
        f"provisioned datasource type(s) with no pinned plugin install: {sorted(types - pinned_ids)}")


def test_every_install_key_is_in_a_parsed_grafana() -> None:
    """Tripwire: a plugin-install key in a file this module did not parse as a
    Grafana container would bypass every rule above."""
    parsed = {g["file"] for g in _all_grafanas()}
    stray = []
    for path in repo_files():
        rel = path.relative_to(REPO).as_posix()
        if not rel.startswith(_TRIPWIRE_ROOTS):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _INSTALL_KEY.search(_COMMENT_LINE.sub("", text)) and rel not in parsed:
            stray.append(rel)
    assert not stray, (
        "Grafana plugin install key(s) outside a Grafana container this module parses: "
        f"{sorted(stray)} — extend the discovery here before installing plugins there.")


def test_the_predicate_rejects_what_it_must() -> None:
    """Control: rule 2 is only as strong as `_PINNED`."""
    assert _PINNED.fullmatch("victoriametrics-logs-datasource@0.32.0")
    for bad in ("victoriametrics-logs-datasource",
                "victoriametrics-logs-datasource@",
                "victoriametrics-logs-datasource@latest",
                "victoriametrics-logs-datasource@0.32",
                "victoriametrics-logs-datasource 0.32.0",
                "victoriametrics-logs-datasource@0.32.0@https://example.com/p.zip",
                "victoriametrics-logs-datasource@@https://example.com/p.zip"):
        assert not _PINNED.fullmatch(bad), bad
    assert _INSTALL_KEY.search("  - name: GF_PLUGINS_PREINSTALL_SYNC")
    assert _INSTALL_KEY.search("[plugins]\npreinstall = a@1.0.0")
    assert _INSTALL_KEY.search("[plugins]\npreinstall_sync: a@1.0.0")
    assert _INSTALL_KEY.fullmatch("GF_PLUGINS_PREINSTALL__FILE")
    assert _INSTALL_KEY.fullmatch("GF_INSTALL_PLUGINS__FILE")
    assert not _INSTALL_KEY.search("GF_PLUGINS_PREINSTALL_ASYNC")
    for img in ("grafana/grafana:12.4.12", "docker.io/grafana/grafana:13.0.0@sha256:ab",
                "grafana/grafana-oss", "localhost:5000/grafana/grafana-enterprise:1.0"):
        assert _is_grafana(img), img
    for img in ("grafana/grafana-image-renderer:4.0", "busybox:1.36", "grafana/loki:3.0"):
        assert not _is_grafana(img), img
    assert _opaque_env_sources({"envFrom": [{"configMapRef": {"name": "x"}}]}) == ["envFrom"]
    assert _opaque_env_sources({"env_file": ".env"}) == ["env_file"]
    assert _opaque_env_sources({"env": [{"name": "A", "value": "b"}]}) == []
    assert not _INSTALL_KEY.search("GF_PLUGINS_DISABLE_PLUGINS")
    assert not _INSTALL_KEY.search(_COMMENT_LINE.sub("", "  # set GF_PLUGINS_PREINSTALL_SYNC"))
    assert _INSTALL_KEY.search(_COMMENT_LINE.sub("", "# note\n  - name: GF_INSTALL_PLUGINS"))
