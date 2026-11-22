"""_iac_warn_baseline.py — the Layer 1 WARN register and its generated table (#3863).

hadolint ``warning`` findings do not block a merge, but each one must carry a
written reason (docs/internal/iac-lint-baseline.md, Severity -> Action). The
reasons live here, in ``REGISTER``, keyed by ``(Dockerfile, rule code)`` —
never by line, so moving a line does not unregister it. The table in the doc
is generated from this run's WARN findings joined with ``REGISTER`` and
written between the ``BEGIN`` / ``END`` markers; nobody edits it by hand.

``check_iac_vibe_rules.py`` is the only caller. Under ``--ci`` it fails (rc 1)
on any of the three problems ``problems`` / ``splice`` report:

  unregistered  a WARN whose (file, code) has no entry here
  stale         an entry whose (file, code) no WARN produced in this run
  drift         the doc's generated table differs from ``render``'s output

and ``--write-baseline`` rewrites the table. A registered WARN stays a WARN.

The register's text is Chinese because it is rendered into a zh document
(docs/internal/** has no English mirror); the messages the wrapper prints
about it are English, like the rest of scripts/tools.

Pure functions only: no engine run, no file IO (the wrapper does both).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

DOC = "docs/internal/iac-lint-baseline.md"
BEGIN = "<!-- BEGIN GENERATED: iac-l1-warn-baseline -->"
END = "<!-- END GENERATED: iac-l1-warn-baseline -->"

Key = tuple[str, str]   # (repo-relative posix Dockerfile path, hadolint rule code)


@dataclass(frozen=True)
class Entry:
    """Why one (Dockerfile, rule) WARN is accepted, and what ends it."""
    summary: str     # 說明：what the finding is about in this image
    rationale: str   # 為什麼接受
    exit: str        # 退場／修補條件


_APK = "apk 不 pin 版本是平台政策（見本節上方「DL3018 政策」）。"
_APK_EXIT = "政策性 deferred；除非改採 pinned-base 策略，否則保留"

# ⛔ The one place a Layer 1 WARN is accepted. Add an entry in the same PR
# that introduces the WARN; delete it in the PR that makes the WARN go away
# (a leftover entry is "stale" and fails the run). An entry whose reason is
# not known yet says so ("待 owner 補：…") rather than inventing one.
REGISTER: dict[Key, Entry] = {
    ("components/da-tools/app/Dockerfile", "DL3018"): Entry(
        summary="runtime `apk add --no-cache git` 未 pin",
        rationale=_APK + "issue 1444：`da-tools init` 產出的 GitLab pipeline 在這個映像"
                  "**裡面**跑 blast-radius job，要用 git 讀 MR 的 base commit；映像以 nonroot"
                  " 執行，job 裡沒辦法 `apk add`，只能裝進映像。",
        exit=_APK_EXIT),
    ("components/tenant-api/Dockerfile", "DL3018"): Entry(
        summary="`apk add --no-cache git ca-certificates tzdata` 未 pin",
        rationale=_APK + "git：tenant-api 的 gitops writer 要 shell out 到 git"
                  "（同 Layer 2 `EXEMPTIONS` 的 tenant-api 列）。",
        exit=_APK_EXIT),
    ("helm/federation-gateway/audit-sidecar/Dockerfile", "DL3018"): Entry(
        summary="runtime `apk add --no-cache logrotate` 未 pin",
        rationale=_APK,
        exit=_APK_EXIT),
    ("helm/vector/projection-gate/Dockerfile", "DL3018"): Entry(
        summary="#908 tenantProjections gate 的 init-container image "
                "`apk add --no-cache python3 py3-yaml` 未 pin",
        rationale=_APK + "py3-yaml 是 distro 套件，映像沒有 pip 與 build toolchain。",
        exit=_APK_EXIT),
    ("try-local/generator/Dockerfile", "DL3018"): Entry(
        summary="runtime `apk add --no-cache git` 未 pin（try-local demo stack 專用映像，#2903）",
        rationale=_APK + "該行上方的註解即援引此政策。generator-service 用 git 讀 `--repo`"
                  " 與請求的 base commit（`scripts/tools/ops/generator_service.py` 的"
                  " `--git` 旗標，#4035）；映像以 nonroot（uid 10001）執行，不能在執行期"
                  " `apk add`。nightly image scan 有掃這個映像。",
        exit=_APK_EXIT),
}


def _check_register(register: Mapping[Key, Entry]) -> None:
    """Import-time: every entry renders as one table row and says something."""
    for (path, code), e in register.items():
        if not path or path.startswith("/") or "\\" in path or not code.strip():
            raise ValueError(f"REGISTER[{(path, code)!r}]: key must be (repo-relative posix path, code)")
        for field in ("summary", "rationale", "exit"):
            text = getattr(e, field)
            if not text.strip():
                raise ValueError(f"REGISTER[{(path, code)!r}].{field} is empty")
            if "\n" in text or "\r" in text:
                raise ValueError(f"REGISTER[{(path, code)!r}].{field} must be one line")


_check_register(REGISTER)


def warn_keys(warns: Iterable[Mapping]) -> dict[Key, list[int]]:
    """hadolint warning findings -> {(file, code): sorted line numbers}."""
    out: dict[Key, list[int]] = {}
    for f in warns:
        key = (str(f.get("file")), str(f.get("code")))
        out.setdefault(key, []).append(int(f.get("line") or 0))
    return {k: sorted(v) for k, v in sorted(out.items())}


def problems(warns: Iterable[Mapping], register: Mapping[Key, Entry]) -> list[str]:
    """The unregistered and stale (file, code) pairs, one message each."""
    seen = warn_keys(warns)
    out = []
    for (path, code), lines in seen.items():
        if (path, code) not in register:
            where = ", ".join(f"{path}:{n}" for n in lines)
            out.append(f"unregistered WARN {code} in {path} ({where}): add a "
                       f"REGISTER entry with its rationale in "
                       f"scripts/tools/lint/_iac_warn_baseline.py, or fix the finding")
    for path, code in sorted(register):
        if (path, code) not in seen:
            out.append(f"stale register entry ({path}, {code}): hadolint no longer "
                       f"reports it — delete the entry")
    return out


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def render(warns: Iterable[Mapping], register: Mapping[Key, Entry]) -> str:
    """The generated block, markers included, for the registered WARNs."""
    seen = warn_keys(warns)
    rows = [(k, len(v)) for k, v in seen.items() if k in register]
    lines = [BEGIN]
    if not rows:
        lines.append("（目前沒有 hadolint WARN。）")
    else:
        lines += ["| # | Dockerfile | Code | 筆數 | 說明 | Rationale | 退場 / 修補 |",
                  "|---:|---|---|---:|---|---|---|"]
        for i, ((path, code), n) in enumerate(rows, 1):
            e = register[(path, code)]
            lines.append(f"| {i} | `{path}` | {code} | {n} | {_cell(e.summary)} | "
                         f"{_cell(e.rationale)} | {_cell(e.exit)} |")
    lines.append(END)
    return "\n".join(lines)


class BaselineDocError(ValueError):
    """The doc does not hold exactly one well-formed generated block."""


def splice(doc_text: str, block: str) -> str:
    """``doc_text`` with its generated block replaced by ``block``.

    The markers must each sit alone on a line, exactly once, BEGIN first;
    anything else is a BaselineDocError (never a silent append)."""
    lines = doc_text.split("\n")
    begins = [i for i, ln in enumerate(lines) if ln.strip() == BEGIN]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == END]
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        raise BaselineDocError(
            f"{DOC} must hold `{BEGIN}` then `{END}`, each exactly once on its own "
            f"line (found {len(begins)} BEGIN, {len(ends)} END)")
    b, e = begins[0], ends[0]
    return "\n".join(lines[:b] + block.split("\n") + lines[e + 1:])
