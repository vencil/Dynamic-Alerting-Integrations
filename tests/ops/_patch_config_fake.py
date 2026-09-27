"""A fake `kubectl` for patch_config's apply path (replaces `run_cmd`).

It holds one ConfigMap and N exporter pods. Each pod has the data it last
"loaded" and a reload counter shown as `Last reload`; its /metrics is
`render(loaded data, pod)`. A patch changes the ConfigMap and, when
`reload(patch index)` is true, makes every pod load it and bump the counter.

/api/v1/config/identity (#2069) answers for the loaded data: config_hash by
`fake_config_hash` — written here from the exporter's rule, NOT by calling
patch_config, so a wrong Python copy of the rule cannot agree with itself —
and parse_failed the loaded keys `rejects(key, text)` names (default: YAML
that does not load). `identity=False` makes every pod answer 404 like an
exporter older than the endpoint; a callable(pod) picks per pod.
"""
import hashlib
import json

import yaml

import patch_config as pc

NOT_FOUND = ("Error from server (NotFound): the server could not find the "
             "requested resource")


def fake_config_hash(data, mode="directory"):
    """What an exporter serving `data` ({key: text}) reports as config_hash."""
    if mode == "single-file":
        return hashlib.sha256(data["config.yaml"].encode("utf-8")).hexdigest()
    keys = sorted(k for k in data if not k.startswith(".")
                  and k.lower().endswith((".yaml", ".yml")))
    return hashlib.sha256("".join(
        hashlib.sha256(data[k].encode("utf-8")).hexdigest() for k in keys
    ).encode("ascii")).hexdigest()


def yaml_rejects(key, text):
    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        return True
    return False


def render_thresholds(data, pod=None):
    """Toy exporter: one `user_threshold` per non-`_` key of each tenant, one
    `user_silent_mode` per `_silent_mode` other than `disable` (target = its
    text)."""
    lines = []
    for key in sorted(data):
        if key.startswith("_") or not data[key]:
            continue
        doc = yaml.safe_load(data[key]) or {}
        for tenant, block in sorted((doc.get("tenants") or {}).items()):
            for metric, value in sorted((block or {}).items()):
                if metric == "_silent_mode" and str(value) != "disable":
                    lines.append(f'user_silent_mode{{target_severity="{value}",'
                                 f'tenant="{tenant}"}} 1')
                elif not metric.startswith("_") and str(value) != "disable":
                    lines.append(f'user_threshold{{metric="{metric}",'
                                 f'tenant="{tenant}"}} {value}')
    return "\n".join(lines) + "\n"


class FakeCluster:
    def __init__(self, data, render=render_thresholds, pods=("exporter-0",),
                 reload=lambda n: True, fail_patch=(), fail_raw=None,
                 no_pods=False, not_serving=None, patch_lands=False,
                 fail_get_cm_after=None, concurrent=None, identity=True,
                 rejects=yaml_rejects, mode="directory", on_raw=None):
        self.data = dict(data)
        self.render, self.reload = render, reload
        self.pods = list(pods)
        self.loaded = {p: dict(data) for p in self.pods}
        self.identity = identity  # bool, or callable(pod) -> bool
        self.rejects, self.mode = rejects, mode
        # on_raw(pod, path, cluster): runs before each pods/proxy answer
        self.on_raw = on_raw
        self.gen = {p: 0 for p in self.pods}
        self.patches, self.fail_patch = [], set(fail_patch)
        self.fail_raw = fail_raw  # callable(pod, path, n_patches) -> bool
        self.no_pods = no_pods
        # pod name -> (phase, deletionTimestamp) for pods listed but not serving
        self.not_serving = dict(not_serving or {})
        self.patch_lands = patch_lands  # a failing patch was still applied (bool, or patch indices)
        self.fail_get_cm_after = fail_get_cm_after  # n patches → get cm fails
        # resourceVersion, bumped by every write; a patch carrying another
        # one is refused like the apiserver does (409 Conflict).
        self.rv = 1
        self.preconditions = []
        # concurrent(n, cluster): runs before patch #n — another writer
        self.concurrent = concurrent
        self.calls = []

    def other_writer(self, key, value):
        """Someone else's write: changes `key` and bumps the version."""
        self.data[key] = value
        self.rv += 1

    def deliver(self, data, pods=None):
        """Every pod (or `pods`) loads `data` — whatever the ConfigMap holds —
        and bumps its reload counter."""
        for p in pods or self.pods:
            self.loaded[p] = dict(data)
            self.gen[p] += 1

    def has_identity(self, pod):
        return self.identity(pod) if callable(self.identity) else self.identity

    def identity_doc(self, pod):
        loaded = self.loaded[pod]
        return {"schema": 1, "loaded": True, "mode": self.mode,
                "last_reload": f"2026-01-01T00:00:00.{self.gen[pod]:09d}Z",
                "config_hash": fake_config_hash(loaded, self.mode),
                "parse_failed": sorted(
                    k for k in loaded
                    if not k.startswith(".") and k.lower().endswith((".yaml", ".yml"))
                    and self.rejects(k, loaded[k]))}

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:3] == ["kubectl", "get", "configmap"]:
            if (self.fail_get_cm_after is not None
                    and len(self.patches) >= self.fail_get_cm_after):
                raise pc.KubectlError("get configmap failed")
            return json.dumps({"metadata": {"resourceVersion": str(self.rv)},
                               "data": self.data})
        if cmd[:3] == ["kubectl", "get", "pods"]:
            items = [] if self.no_pods else [
                self._pod(p, "Running", None) for p in self.pods] + [
                self._pod(p, phase, gone)
                for p, (phase, gone) in self.not_serving.items()]
            return json.dumps({"items": items})
        if cmd[:3] == ["kubectl", "get", "--raw"]:
            rest = cmd[3].split("/pods/", 1)[1]
            pod_port, path = rest.split("/proxy/", 1)
            pod = pod_port.split(":")[0]
            if self.fail_raw and self.fail_raw(pod, path, len(self.patches)):
                raise pc.KubectlError(f"proxy to {pod} failed")
            if self.on_raw:
                self.on_raw(pod, path, self)
            if path == "api/v1/config/identity":
                if not self.has_identity(pod):
                    raise pc.KubectlError(
                        f"Error executing: kubectl get --raw ...\n{NOT_FOUND}")
                return json.dumps(self.identity_doc(pod))
            if path == "api/v1/config":
                return (f"Config loaded: true\nLast reload:   "
                        f"2026-01-01T00:00:{self.gen[pod]:02d}Z\n")
            return self.render(self.loaded[pod], pod)
        if cmd[:2] == ["kubectl", "patch"]:
            with open(cmd[cmd.index("--patch-file") + 1], encoding="utf-8") as fh:
                patch = json.load(fh)
            n = len(self.patches)
            if self.concurrent:
                self.concurrent(n, self)
            self.patches.append({"data": patch["data"]})
            want = (patch.get("metadata") or {}).get("resourceVersion")
            self.preconditions.append(want)
            if want is not None and want != str(self.rv):
                raise pc.KubectlError(
                    'Error from server (Conflict): Operation cannot be fulfilled '
                    'on configmaps "threshold-config": the object has been '
                    'modified; please apply your changes to the latest version '
                    'and try again')
            lands = (self.patch_lands is True
                     or (not isinstance(self.patch_lands, bool)
                         and n in self.patch_lands))
            if n in self.fail_patch and not lands:
                raise pc.KubectlError("patch refused")
            for k, v in patch["data"].items():
                if v is None:
                    self.data.pop(k, None)
                else:
                    self.data[k] = v
            self.rv += 1
            if self.reload(n):
                for p in self.pods:
                    self.loaded[p] = dict(self.data)
                    self.gen[p] += 1
            if n in self.fail_patch:
                raise pc.KubectlError("patch timed out (but was applied)")
            return json.dumps({"metadata": {"resourceVersion": str(self.rv)},
                               "data": self.data})
        raise AssertionError(f"unexpected kubectl call: {cmd}")

    @staticmethod
    def _pod(name, phase, deleting):
        meta = {"name": name}
        if deleting:
            meta["deletionTimestamp"] = deleting
        return {"metadata": meta, "status": {"phase": phase},
                "spec": {"containers": [{"ports": [
                    {"name": "http", "containerPort": 8080}]}]}}
