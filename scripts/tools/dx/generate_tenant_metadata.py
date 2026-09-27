#!/usr/bin/env python3
"""租戶元資料產生器 — 從 conf.d/ 解析 YAML，推斷 rule_packs、owner、tier、routing_channel。

Usage:
    python3 scripts/tools/dx/generate_tenant_metadata.py              # 產生 JSON
    python3 scripts/tools/dx/generate_tenant_metadata.py --config-dir conf.d
    python3 scripts/tools/dx/generate_tenant_metadata.py --output /tmp/out.json
    python3 scripts/tools/dx/generate_tenant_metadata.py --check      # CI drift 偵測
    python3 scripts/tools/dx/generate_tenant_metadata.py --dry-run    # 只印出不寫檔
"""
import argparse
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_confd import (  # noqa: E402
    has_yaml_extension,
    is_hidden_name,
    is_reserved_name,
    unusable_config_entries,
    unusable_reason,
    warn_nested,
)
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_io import (  # noqa: E402  (#1789)
    OutputWriteError,
    exit_on_output_write_error,
    output_write,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "components" / "threshold-exporter" / "config" / "conf.d"


# ---------------------------------------------------------------------------
# Metric prefix to rule pack mapping (from _lib_constants.py)
# ---------------------------------------------------------------------------
METRIC_PREFIX_DB_MAP = {
    "mysql_": "mariadb",
    "mariadb_": "mariadb",
    "pg_": "postgresql",
    "postgres_": "postgresql",
    "redis_": "redis",
    "mongo_": "mongodb",
    "mongodb_": "mongodb",
    "kafka_": "kafka",
    "rabbitmq_": "rabbitmq",
    "rabbit_": "rabbitmq",
    "elasticsearch_": "elasticsearch",
    "es_": "elasticsearch",
    "oracle_": "oracle",
    "clickhouse_": "clickhouse",
    "db2_": "db2",
    "container_": "kubernetes",
    "jvm_": "jvm",
    "nginx_": "nginx",
}

# Always-included packs (inferred from reserved metric keys)
RESERVED_PACKS = {"operational", "platform"}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def infer_rule_packs(tenant_config: dict) -> list[str]:
    """Infer rule packs from metric key prefixes."""
    packs = set(RESERVED_PACKS)

    for key in tenant_config.keys():
        # Skip reserved keys
        if key.startswith("_"):
            continue

        # Extract metric prefix (everything before first underscore or [)
        metric = key.split("[")[0] if "[" in key else key

        # Match metric prefix to rule pack
        for prefix, pack in METRIC_PREFIX_DB_MAP.items():
            if metric.startswith(prefix):
                packs.add(pack)
                break

    return sorted(list(packs))


def extract_owner(tenant_config: dict) -> str:
    """Extract owner from _metadata.owner if present."""
    metadata = tenant_config.get("_metadata", {})
    if isinstance(metadata, dict):
        return metadata.get("owner", "")
    return ""


def extract_tier(tenant_config: dict) -> str:
    """Extract tier from _metadata.tier if present."""
    metadata = tenant_config.get("_metadata", {})
    if isinstance(metadata, dict):
        return metadata.get("tier", "")
    return ""


def extract_environment(tenant_name: str, metadata: dict) -> str:
    """Infer environment from _metadata or tenant name pattern."""
    if isinstance(metadata, dict):
        env = metadata.get("environment", "")
        if env:
            return env

    # Infer from name pattern
    if tenant_name.startswith("prod-") or "production" in tenant_name:
        return "production"
    elif tenant_name.startswith("staging-") or "staging" in tenant_name:
        return "staging"
    elif tenant_name.startswith("dev-") or "development" in tenant_name:
        return "development"

    return ""


def extract_db_type(tenant_config: dict, metadata: dict) -> str:
    """Extract db_type from _metadata or infer from metric prefixes."""
    if isinstance(metadata, dict):
        db_type = metadata.get("db_type", "")
        if db_type:
            return db_type

    # Infer from metric key prefixes
    db_prefixes = {
        "mysql_": "mariadb",
        "mariadb_": "mariadb",
        "pg_": "postgresql",
        "postgres_": "postgresql",
        "redis_": "redis",
        "mongo_": "mongodb",
        "mongodb_": "mongodb",
        "kafka_": "kafka",
        "rabbitmq_": "rabbitmq",
        "elasticsearch_": "elasticsearch",
        "oracle_": "oracle",
        "clickhouse_": "clickhouse",
    }
    for key in tenant_config.keys():
        if key.startswith("_"):
            continue
        metric = key.split("[")[0] if "[" in key else key
        for prefix, db in db_prefixes.items():
            if metric.startswith(prefix):
                return db
    return ""


def extract_tags(metadata: dict) -> list[str]:
    """Extract tags from _metadata.tags if present."""
    if isinstance(metadata, dict):
        tags = metadata.get("tags", [])
        if isinstance(tags, list):
            return [str(t) for t in tags]
    return []


def extract_groups(metadata: dict) -> list[str]:
    """Extract group memberships from _metadata.groups if present."""
    if isinstance(metadata, dict):
        grps = metadata.get("groups", [])
        if isinstance(grps, list):
            return [str(g) for g in grps]
    return []


def extract_routing_channel(tenant_config: dict) -> str:
    """Extract routing channel from _routing.receiver (format: type:url or type:endpoint)."""
    routing = tenant_config.get("_routing", {})
    if not isinstance(routing, dict):
        return ""

    receiver = routing.get("receiver", {})
    if not isinstance(receiver, dict):
        return ""

    recv_type = receiver.get("type", "")
    if not recv_type:
        return ""

    # Format: type:url or type:endpoint
    if recv_type == "webhook":
        url = receiver.get("url", "")
        return f"{recv_type}:{url}" if url else ""
    elif recv_type == "email":
        to = receiver.get("to", "")
        return f"{recv_type}:{to}" if to else ""
    elif recv_type == "slack":
        api_url = receiver.get("api_url", "")
        return f"{recv_type}:{api_url}" if api_url else ""
    elif recv_type == "teams":
        webhook_url = receiver.get("webhook_url", "")
        return f"{recv_type}:{webhook_url}" if webhook_url else ""
    elif recv_type == "pagerduty":
        # The keys are credentials: name the Events API version, never the
        # key. service_key wins when both are set, as it does in Alertmanager.
        if receiver.get("service_key"):
            return f"{recv_type}:v1"
        if receiver.get("routing_key"):
            return f"{recv_type}:v2"
        return ""

    return ""


def detect_operational_mode(tenant_config: dict) -> str:
    """Detect operational mode: normal (default), silent, or maintenance."""
    # Check for maintenance state
    if "_state_maintenance" in tenant_config:
        state_val = tenant_config["_state_maintenance"]
        if state_val != "disable":
            return "maintenance"

    # Check for silent mode
    silent_mode = tenant_config.get("_silent_mode", "")
    if silent_mode and silent_mode != "disable":
        return "silent"

    return "normal"


def count_metrics(tenant_config: dict) -> int:
    """Count non-reserved metric keys."""
    count = 0
    for key in tenant_config.keys():
        if not key.startswith("_"):
            count += 1
    return count


def get_git_head_commit() -> str:
    """Get current git HEAD commit hash, or empty if not in git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:7]  # Short hash
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


# ---------------------------------------------------------------------------
# Build tenant metadata
# ---------------------------------------------------------------------------
def build_tenant_metadata(config_dir: Path) -> dict[str, Any]:
    """Parse tenant YAML files and build metadata structure."""
    tenant_groups = {}
    tenant_metadata = {}
    tenant_configs = {}

    # Load all tenant YAML files
    # #1911: flat by design here — but a hierarchical conf.d must not
    # look like an empty one. Name the files this scan cannot see.
    warn_nested(config_dir, tool="generate_tenant_metadata")
    entries = sorted(config_dir.iterdir())
    # #1607: `is_file()` below is a THIRD axis and it was silent. This tool
    # feeds the portal's tenant list, so an entry it drops is a tenant that
    # does not appear — the same shape as the `.YAML` bug above, one axis
    # over. Same stderr channel as the parse failure below.
    # ⛔ `_`-prefixed entries excluded — the loop below skips them whatever
    # their shape, so naming one would report a loss that did not happen.
    # ⛔ `.yml` IS A TENANT CARRIER HERE NOW (#1603). Both sites below used to
    # pass `(".yaml",)`, so a tenant declared in `db-a.yml` produced NO entry
    # in `tenant_metadata` and NO line on stderr — and this file feeds
    # `generate_platform_data`, i.e. the portal's tenant list. Measured on two
    # trees whose contents are byte-identical and differ only in the
    # extension:
    #
    #     db-a.yaml -> 1 tenant,  rc=0, stderr 0 bytes   <- control
    #     db-a.yml  -> 0 tenants, rc=0, stderr 0 bytes   <- before
    #     db-a.yml  -> 1 tenant,  rc=0, stderr 0 bytes   <- after
    #
    # The exporter (`scanDirHierarchical`, `config_hierarchy.go`) lowercases the entry name and
    # accepts both spellings, so it was serving a tenant the portal could not
    # name. Omitting the argument takes `CONFIG_SUFFIXES`, the exporter's set.
    # ⚠️ No `is_hidden_name` filter needed here (#2055): the helper itself
    # already skips `.`-prefixed entries, mirroring the exporter's walker.
    for bad in unusable_config_entries(
        [p for p in entries if not is_reserved_name(p.name)],
    ):
        print(f"WARNING: {safe_label(bad.name)}: {unusable_reason(bad)}",
              file=sys.stderr)
    for yaml_file in (
        p for p in entries
        if p.is_file() and has_yaml_extension(p.name)   # both spellings (#1603)
    ):
        # ⛔ #2055: skip what the exporter skips — `_` control files AND
        # `.`-prefixed names. Reading a hidden file here minted portal
        # tenants the exporter never serves, and `tenant_configs.update`
        # let a hidden file replace a real tenant's WHOLE body whenever the
        # real carrier sorts before `.` (0x2e), e.g. `-acme.yaml`.
        if is_reserved_name(yaml_file.name) or is_hidden_name(yaml_file.name):
            continue
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if data and "tenants" in data and isinstance(data["tenants"], dict):
                tenant_configs.update(data["tenants"])
        except Exception as e:
            print(f"WARNING: {safe_label(yaml_file.name)}: {safe_label(e)}",
                  file=sys.stderr)

    # Process each tenant
    for tenant_name, tenant_config in sorted(tenant_configs.items()):
        if not isinstance(tenant_config, dict):
            continue

        # Extract metadata fields
        metadata = tenant_config.get("_metadata", {})
        environment = extract_environment(tenant_name, metadata)
        region = metadata.get("region", "") if isinstance(metadata, dict) else ""
        domain = metadata.get("domain", "") if isinstance(metadata, dict) else ""

        # v2.5.0: extract extended metadata fields
        db_type = extract_db_type(tenant_config, metadata)
        tags = extract_tags(metadata)
        group_memberships = extract_groups(metadata)

        # Build tenant entry
        tenant_entry = {
            "environment": environment,
            "region": region,
            "tier": extract_tier(tenant_config),
            "domain": domain,
            "db_type": db_type,
            "rule_packs": infer_rule_packs(tenant_config),
            "owner": extract_owner(tenant_config),
            "routing_channel": extract_routing_channel(tenant_config),
            "operational_mode": detect_operational_mode(tenant_config),
            "metric_count": count_metrics(tenant_config),
            "last_config_commit": get_git_head_commit(),
            "tags": tags,
            "groups": group_memberships,
        }

        # Clean up empty fields
        for key in ["environment", "region", "domain", "db_type"]:
            if not tenant_entry[key]:
                tenant_entry[key] = ""

        tenant_metadata[tenant_name] = tenant_entry

        # Track tenant groups by environment
        if environment:
            if environment not in tenant_groups:
                tenant_groups[environment] = {
                    "label": environment.capitalize(),
                    "tenants": [],
                }
            tenant_groups[environment]["tenants"].append(tenant_name)

    # v2.5.0: Load custom groups from _groups.yaml if it exists
    custom_groups = _load_custom_groups(config_dir)

    # v2.5.0: Build multi-dimension group views
    dimension_groups = _build_dimension_groups(tenant_metadata)

    return {
        "_comment": "Auto-generated by generate_tenant_metadata.py — DO NOT EDIT",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "scripts/tools/dx/generate_tenant_metadata.py",
        "tenant_groups": tenant_groups,
        "custom_groups": custom_groups,
        "dimension_groups": dimension_groups,
        "tenant_metadata": tenant_metadata,
        "totals": {
            "tenants": len(tenant_metadata),
            "groups": len(tenant_groups),
        },
    }


def _load_custom_groups(config_dir: Path) -> dict:
    """Load custom groups from _groups.yaml if it exists."""
    groups_file = config_dir / "_groups.yaml"
    if not groups_file.exists():
        return {}

    try:
        data = yaml.safe_load(groups_file.read_text(encoding="utf-8"))
        if data and isinstance(data.get("groups"), dict):
            result = {}
            for gid, gdef in data["groups"].items():
                if isinstance(gdef, dict):
                    result[gid] = {
                        "label": gdef.get("label", gid),
                        "description": gdef.get("description", ""),
                        "members": gdef.get("members", []),
                    }
            return result
        return {}
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: failed to parse _groups.yaml: {safe_label(exc)}",
              file=sys.stderr)
        return {}


def _build_dimension_groups(tenant_metadata: dict[str, dict]) -> dict:
    """Build multi-dimension group views from tenant metadata.

    Produces three dimension views: by-domain, by-db_type, by-region.
    Each view maps dimension value → list of tenant IDs.
    """
    dimensions = {
        "by_domain": "domain",
        "by_db_type": "db_type",
        "by_region": "region",
    }
    result: dict[str, dict[str, list[str]]] = {}

    for dim_key, field in dimensions.items():
        groups: dict[str, list[str]] = {}
        for tenant_id, meta in tenant_metadata.items():
            value = meta.get(field, "")
            if value:
                groups.setdefault(value, []).append(tenant_id)
        # Sort member lists for deterministic output
        for members in groups.values():
            members.sort()
        result[dim_key] = dict(sorted(groups.items()))

    return result


# ---------------------------------------------------------------------------
# --output: atomic replace that does not regress the in-place write (#2082)
# ---------------------------------------------------------------------------
# 0644, the mode this tool has always left on --output (a pre-existing 0600
# file comes out 0644 too — measured on the in-place writer this replaces).
_OUTPUT_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
_NO_SPACE_ERRNOS = frozenset(
    e for e in (errno.ENOSPC, getattr(errno, "EDQUOT", None)) if e is not None
)
# fsync errnos that mean "this filesystem does not do fsync", not "the bytes
# did not land". ENOSPC / EIO from fsync are NOT here: with delayed
# allocation that is where a full disk shows up, and replacing the good file
# with that tmp would be the exact damage this function exists to prevent.
_FSYNC_UNSUPPORTED_ERRNOS = frozenset(
    e for e in (errno.EINVAL, getattr(errno, "ENOTSUP", None),
                getattr(errno, "EOPNOTSUPP", None)) if e is not None
)
# mkstemp's prefix embeds the target name; cap it (in UTF-8 BYTES) so a
# target name near NAME_MAX cannot turn a write that works in place into
# ENAMETOOLONG. (If it still happens, the mkstemp fallback catches it.)
_TMP_PREFIX_NAME_CAP = 64
# os.replace errnos meaning "this name cannot be renamed over", where writing
# through it may still work — so the answer is the in-place write, not rc 2:
# EBUSY — the target is a mount point (docker `-v file:file`, k8s subPath);
# EXDEV — the same, seen through some bind setups; EPERM / EACCES — rename
# refused by policy rather than by file mode (an LSM such as SELinux /
# AppArmor, an append-only or immutable attribute). ⚠️ Not the sticky-/tmp
# case: measured, that never reaches the rename (a non-root run falls back
# at the fchown first, root has CAP_FOWNER).
_REPLACE_REFUSED_ERRNOS = frozenset({errno.EBUSY, errno.EXDEV, errno.EPERM, errno.EACCES})
# listxattr / getxattr errnos that mean "this filesystem has no xattrs": that
# side reads as {}. Any OTHER failure is "cannot compare" ⇒ in place.
_XATTR_UNSUPPORTED_ERRNOS = frozenset(
    e for e in (getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None))
    if e is not None
)
# Kernel-maintained xattrs left out of the old-vs-new comparison. IMA's
# ``security.ima`` is a hash of the CONTENT and EVM's ``security.evm`` an HMAC
# over inode / uid / gid / mode: they differ on ANY new file, so comparing them
# would send every write in place on an IMA/EVM host and cost #2082 its
# protection — yet they are not "lost": the kernel computes them afresh for
# the replacement. Exactly these two; every other security.* is compared.
_KERNEL_MAINTAINED_XATTRS = frozenset({"security.ima", "security.evm"})


class OutputNoSpaceError(OutputWriteError):
    """``OutputWriteError`` for a full / over-quota filesystem (#2082).

    The shared message ends in "check the value given to --output", which
    for ENOSPC sends the operator to the one thing that is right. Same class
    family (so ``exit_on_output_write_error`` still turns it into rc 2 with no
    traceback), different hint — and it says whether the previous file
    survived, because on the atomic path it did and on the in-place
    fallbacks it may not have.
    """

    def __init__(self, path: Any, cause: OSError, *, flag: str | None,
                 previous_kept: bool) -> None:
        super().__init__(path, cause, flag=flag)
        self.previous_kept = previous_kept

    def __str__(self) -> str:
        detail = self.strerror or str(self.cause)
        if self.errno is not None:
            detail = f"{detail} (errno {self.errno})"
        state = ("the previous file is unchanged" if self.previous_kept
                 else "the file may now be incomplete")
        return (f"cannot {self.action} {self.path}: {detail} — the filesystem "
                f"holding it is full or over quota; free space and re-run "
                f"({state})")


def _output_error(out: Path, exc: OSError, *, previous_kept: bool) -> OutputWriteError:
    """The #1789 exception for a failure while producing *out*, named by *out*.

    Named by the ``--output`` value, never by the private tmp file or the
    symlink's resolved target: those are paths the operator did not type.
    """
    if getattr(exc, "errno", None) in _NO_SPACE_ERRNOS:
        return OutputNoSpaceError(out, exc, flag="--output", previous_kept=previous_kept)
    return OutputWriteError(out, exc, flag="--output")


def _warn_not_atomic(out: Path, why: str) -> None:
    print(
        f"WARN: {safe_label(str(out))}: {why}; writing it in place instead "
        f"of atomically — an interrupted run can leave it truncated",
        file=sys.stderr,
    )


def _write_in_place(out: Path, content: str, *, set_mode: bool) -> None:
    """The pre-#2082 writer: truncate, write, chmod. Only for the fallbacks."""
    try:
        out.write_text(content, encoding="utf-8", newline="\n")
        if set_mode:
            os.chmod(out, _OUTPUT_MODE)
    except OSError as exc:
        raise _output_error(out, exc, previous_kept=False) from exc


def _xattrs(ref: str | int) -> dict[str, bytes]:
    """Every extended attribute of *ref* (a path or an fd), as ``{name: value}``.

    A filesystem without xattr support reads as ``{}``; a name that vanished
    between list and get (ENODATA) is skipped, and so is
    ``_KERNEL_MAINTAINED_XATTRS``. Any other ``OSError`` is raised.
    """
    try:
        names = os.listxattr(ref)
    except OSError as exc:
        if exc.errno in _XATTR_UNSUPPORTED_ERRNOS:
            return {}
        raise
    found: dict[str, bytes] = {}
    for name in names:
        if name in _KERNEL_MAINTAINED_XATTRS:
            continue
        try:
            found[name] = os.getxattr(ref, name)
        except OSError as exc:
            if exc.errno in _XATTR_UNSUPPORTED_ERRNOS:
                return {}
            if exc.errno == getattr(errno, "ENODATA", None):
                continue
            raise
    return found


def _xattr_mismatch(target: Path, fd: int) -> str | None:
    """The WARN text when the tmp *fd* would not reproduce *target*'s xattrs.

    A replace makes a NEW inode, and POSIX ACLs (``system.posix_acl_*``),
    the SELinux label (``security.selinux``) and ``user.*`` are all xattrs:
    measured, a ``user.*`` xattr is gone after a replace and kept by the
    in-place write. So the tmp's xattrs — whatever the directory gave it
    (default ACL, SELinux default context) — are compared with the target's,
    and only a difference sends the write in place. On an SELinux host both
    usually get the same ``security.selinux`` ⇒ atomic stays. IMA / EVM
    values are not compared (``_KERNEL_MAINTAINED_XATTRS``).
    Not copied: setting ``security.*`` / ``trusted.*`` needs privileges.

    ⚠️ Only what the RUNNING user can list is compared. Without
    CAP_SYS_ADMIN, ``listxattr`` omits ``trusted.*`` on BOTH sides, so they
    compare equal and a replace silently drops a root-set ``trusted.*``
    (measured as uid 65534).

    ⚠️ Linux only: without ``os.listxattr`` (macOS, the BSDs, Windows)
    nothing is compared and a replace does not keep xattrs (``com.apple.*``
    and the like) — the pre-comparison behaviour of #2082.
    """
    if not hasattr(os, "listxattr"):
        return None
    try:
        old, new = _xattrs(str(target)), _xattrs(fd)
    except OSError as exc:
        return (f"its extended attributes cannot be compared (errno {exc.errno}) "
                f"and a replacement might not keep them")
    if old == new:
        return None
    differ = sorted(n for n in old.keys() | new.keys() if old.get(n) != new.get(n))
    return ("it carries extended attributes (ACLs / security labels) that a "
            f"replacement would not keep: {', '.join(differ)}")


def atomic_replace_output(out: Path, content: str) -> None:
    """Write *content* to ``--output`` so an interrupted run keeps the old file.

    #2082: the in-place ``write_text`` truncated first, so ENOSPC / a kill /
    a FUSE drop mid-write left half a JSON document where the last good one
    was. When the file is *eligible* (below) this writes a private tmp file
    beside the REAL target, fsyncs it and ``os.replace``-s it over the
    target: a reader sees the old bytes or the new ones, never a prefix.

    **The rule**: wherever the in-place write succeeded or refused, this ends
    the same way. When atomic is not possible it falls back to the in-place
    write — with a WARN when that silently costs atomicity on an existing
    file — and never adds an rc 2 of its own.

    **Eligible for atomic** = no file yet, or a regular file with ONE link
    that we may write (``os.access(out, W_OK)``). Everything else goes in
    place, as before:

    * a non-regular file (``/proc/self/fd/1`` → pipe / tty, a FIFO, a
      directory) — nothing to replace; no chmod on what we did not create;
      a directory still ends in rc 2;
    * a hard-linked file — a replace would detach this name from the other
      links without a word; WARN and write in place so every name updates;
    * a read-only file — it is refused with EACCES (rc 2) as before, instead
      of being replaced through a writable directory.

    ⛔ Not the shared ``_atomic_write.atomic_write_text``, measured to
    regress this tool three ways: the target here is ``os.path.realpath``
    (a symlink stays a symlink, its target is replaced — across filesystems
    too); the tmp comes from ``mkstemp`` (a user's ``<out>.tmp`` is never
    touched); and an unwritable directory falls back instead of failing.
    What makes :func:`_try_atomic` give up is listed there.

    ⚠️ A replace makes a NEW inode: extended attributes, POSIX ACLs and the
    SELinux label of the old file are not carried over — the new inode gets
    the directory's defaults. :func:`_try_atomic` compares the two sets and
    gives up where they differ. Mode and owner are carried.

    Every ``OSError`` leaves as :class:`OutputWriteError` (or
    :class:`OutputNoSpaceError`) naming *out*, so ``main``'s
    ``exit_on_output_write_error`` ends it at rc 2 with no traceback.
    """
    try:
        st = os.stat(out)  # follows symlinks, /proc magic links included
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise _output_error(out, exc, previous_kept=True) from exc

    regular = st is None or stat.S_ISREG(st.st_mode)
    eligible = st is None or (
        regular and st.st_nlink == 1 and os.access(out, os.W_OK)
    )
    if eligible:
        done, why = _try_atomic(out, content, st)
        if done:
            return
        # No file yet ⇒ nothing to truncate, and the in-place write reports
        # the real failure itself: a WARN would only be noise ahead of it.
        if why is not None and st is not None:
            _warn_not_atomic(out, why)
    elif regular and st.st_nlink > 1:
        _warn_not_atomic(
            out,
            f"it has {st.st_nlink} hard links and replacing it would detach "
            f"this name from the others",
        )
    _write_in_place(out, content, set_mode=regular)


def _try_atomic(out: Path, content: str,
                st: os.stat_result | None) -> tuple[bool, str | None]:
    """Replace *out* atomically: ``(True, None)``, or ``(False, why)`` to fall back.

    ``why`` is the WARN text, or ``None`` when no WARN is due. On a
    ``False`` return nothing has changed on disk — the tmp is gone. It gives
    up (``False``) exactly where the in-place write would still have
    produced the old result:

    * ``mkstemp`` fails for ANY reason (EACCES, EROFS, ENOSPC for inodes,
      ENAMETOOLONG, …): the directory cannot take a new file but the
      existing one may still be writable — the in-place write either
      succeeds as it always did or reports the real error;
    * the tmp cannot be handed back to the file's OWNER (``fchown``, any
      ``OSError`` — EPERM for a non-root run, EINVAL for an unmapped id in a
      rootless / userns container). No WARN: for a non-root run the in-place
      write then fails its own chmod (rc 2), exactly as before, and a WARN
      ahead of that ERROR is noise. Only the GROUP not being settable while
      the owner matches is not a reason: the owner can still rewrite the
      file, so the replace goes ahead (the group becomes ours);
    * ``os.replace`` refused with one of ``_REPLACE_REFUSED_ERRNOS``.

    * the tmp's extended attributes differ from the target's
      (:func:`_xattr_mismatch`) — a ``user.*`` xattr, a named ACL, a custom
      SELinux label the directory defaults would not reproduce — or cannot
      be compared (any errno but ENOTSUP / ENODATA). WARN, in place, where
      they survive. Linux only, and only the namespaces the running user can
      list (a non-root run cannot see ``trusted.*``) — see there. Compared AFTER ``fchown`` / ``fchmod``: a ``chmod`` of a
      file whose access ACL was inherited from a default ACL rewrites the
      mask entry inside ``system.posix_acl_access``, and the in-place writer
      chmods too, so this is the state a replace would really leave.

    A real write failure (write / fsync: ENOSPC, EIO, …) is NOT a fallback:
    the tmp is removed, the target is untouched, and it is raised as
    :class:`OutputWriteError` naming *out* — writing in place at that point
    would put the truncation risk straight back.

    ⚠️ **The one deliberate exception to "same result as in place"**: with
    the data blocks full, the in-place write could truncate the old file and
    reuse its blocks, and succeed; here the tmp write fails with ENOSPC and
    the run ends rc 2 with the old file intact. That truncation is exactly
    what #2082 exists to prevent, so it is kept (owner ruling on #2082).

    The mode is set with ``fchmod`` on the open fd, before any byte is
    written: a path-based chmod could follow a symlink someone swapped in
    for the tmp name.
    """
    target = Path(os.path.realpath(out))
    # Capped in BYTES, not characters: 64 four-byte characters would push
    # the tmp name past NAME_MAX (255 bytes) for a target name that fits.
    name_head = os.fsencode(target.name)[:_TMP_PREFIX_NAME_CAP].decode(
        "utf-8", "ignore")
    fd, tmp_name, replaced = -1, None, False
    try:
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{name_head}.", suffix=".tmp",
            )
        except OSError as exc:
            # ANY failure to create the tmp: the in-place write may still
            # work (an unwritable or read-only directory holding a writable
            # file — readOnlyRootFilesystem + a subPath / `-v file:file`
            # mount), and where it cannot, it reports the real error itself.
            return False, f"cannot create a temporary file beside it ({exc.strerror})"
        if st is not None and hasattr(os, "fchown") and (
                st.st_uid != os.geteuid() or st.st_gid != os.getegid()):
            try:
                os.fchown(fd, st.st_uid, st.st_gid)
            except OSError:
                if st.st_uid != os.geteuid():
                    return False, None
        if hasattr(os, "fchmod"):
            os.fchmod(fd, _OUTPUT_MODE)
        else:  # Windows before 3.13: only the read-only bit exists anyway
            os.chmod(tmp_name, _OUTPUT_MODE)
        if st is not None:
            why = _xattr_mismatch(target, fd)
            if why is not None:
                return False, why
        fh = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        fd = -1  # the file object owns it now
        with fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                if exc.errno not in _FSYNC_UNSUPPORTED_ERRNOS:
                    raise
        try:
            os.replace(tmp_name, target)
        except OSError as exc:
            if exc.errno not in _REPLACE_REFUSED_ERRNOS:
                raise
            return False, f"it cannot be replaced ({exc.strerror})"
        replaced = True
        return True, None
    except OSError as exc:
        if isinstance(exc, OutputWriteError):
            raise
        raise _output_error(out, exc, previous_kept=True) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name is not None and not replaced:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _read_existing_for_check(out: Path) -> tuple[dict | None, str | None]:
    """``(metadata, None)`` or ``(None, why it cannot be compared)`` (#2082).

    Every way the ``--output`` file can be unusable — half-written JSON, a
    non-object top level, non-UTF-8 bytes, a directory, an unreadable file —
    becomes a reason string instead of a traceback, so ``--check`` can tell
    "damaged" apart from "outdated".
    """
    try:
        raw = out.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return None, f"not UTF-8 ({exc.reason} at byte {exc.start})"
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or exc})"
    try:
        existing = json.loads(raw)
    except ValueError as exc:
        return None, f"not valid JSON ({exc})"
    except RecursionError:
        return None, "not usable JSON (nested too deeply to parse)"
    if not isinstance(existing, dict):
        kind = {list: "an array", str: "a string", bool: "a boolean",
                int: "a number", float: "a number", type(None): "null",
                }.get(type(existing), type(existing).__name__)
        return None, f"top level is {kind}, expected a JSON object"
    return existing, None


@exit_on_output_write_error
def main():
    """CLI entry point: 租戶元資料產生器."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Generate tenant metadata from conf.d/ YAML files",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help=f"Config directory (default: {DEFAULT_CONFIG_DIR.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file path (default: stdout)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="CI mode: exit 1 if metadata is outdated, "
             "exit 2 if the --output file is damaged or unreadable",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON to stdout without writing file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Format output as pretty JSON",
    )
    args = parser.parse_args()

    # Validate config directory
    if not args.config_dir.exists():
        print(
            f"ERROR: Config directory not found: {args.config_dir}",
            file=sys.stderr,
        )
        sys.exit(EXIT_CALLER_ERROR)

    # Build metadata
    data = build_tenant_metadata(args.config_dir)

    # Format output
    if args.check:
        data.pop("generated", None)

    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    # Handle output
    if args.dry_run or not args.output:
        print(content, end="")
        return

    if args.check:
        if not args.output.exists():
            print(
                f"ERROR: {args.output} does not exist. "
                f"Run without --check first.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VIOLATION)

        existing, damaged = _read_existing_for_check(args.output)
        if damaged is not None:
            # #2082: rc 2, not the "outdated" rc 1 — the file is not an older
            # version of this output, it is not one at all, and a CI consumer
            # must not read a truncated file as ordinary drift.
            print(
                f"ERROR: {safe_label(str(args.output))} is damaged: {damaged}. "
                f"Regenerate it by re-running without --check.",
                file=sys.stderr,
            )
            sys.exit(EXIT_CALLER_ERROR)
        existing.pop("generated", None)
        existing_str = json.dumps(existing, indent=2, ensure_ascii=False) + "\n"

        if existing_str != content:
            print(
                f"ERROR: {args.output} is outdated.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VIOLATION)

        print(f"OK: {args.output} is up to date.")
        return

    # Write output file
    # #1789: the wrapper is given the PARENT — the directory this mkdir
    # actually creates. Handing it the output FILE makes the message a
    # sentence about a path nobody was creating (worked example in
    # `_lib_io.output_write`). The ancestor rule still converts the
    # failure, and the write below keeps naming the file.
    with output_write(args.output.parent, flag="--output",
                      action="create directory"):
        args.output.parent.mkdir(parents=True, exist_ok=True)
    # #2082: atomic replace (0644 set on the tmp before the rename), so a
    # failed run leaves the previous file whole. It converts its own
    # failures to OutputWriteError; the wrapper stays as the #1789 guard for
    # anything it does not, and is what the static pin looks for.
    with output_write(args.output, flag="--output"):
        atomic_replace_output(args.output, content)

    try:
        display_path = args.output.relative_to(REPO_ROOT)
    except ValueError:
        display_path = args.output
    print(f"✅ Generated {display_path}")
    print(f"   {data['totals']['tenants']} tenants, {data['totals']['groups']} groups")


if __name__ == "__main__":
    main()
