"""Every raw write sink under ``scripts/`` is on one of two lists (#3981 c).

WHAT THIS GATE IS FOR
---------------------
A tool's file writes are meant to go through the two entries of
``_lib_output.OUTPUT_ENTRY_POINTS`` — an operator output or a repo artifact
(``docs/internal/lint-policy.md``). The run-time write gate
(``test_output_path_write_failure``) holds the tools that take an output
flag to that, per flag. It never sees a tool with no such flag, or one that
never calls a writer by name: a generator that rewrites a tracked file with
``Path.write_text``, a ``--fix`` mode, a linter's scratch file. This gate is
for those, and it runs no tool.

Population: every ``.py`` file of the repository under ``scripts/`` —
derived, no tool is named, nothing excluded: the modules the entries live
in are in it too, their own sinks listed as ``entry``. Sinks: the
vocabulary of ``test_output_write_sites_stay_guarded`` (a write-mode
``open``, ``write_text`` / ``write_bytes``, ``mkdir``, ``touch``, ``chmod``,
``os.makedirs`` / ``os.fdopen``, ``shutil``'s copies and ``move``,
``os.replace`` / ``os.rename``, ``tempfile.mkstemp``, a direct
``atomic_write_text``). A call of an entry is not a sink.

THE TWO LISTS (owner 2026-11-20)
--------------------------------
Each raw sink is listed once, keyed ``"<enclosing function> <sink>
[<class>]": <how many>`` under its file — no line numbers, so an edit that
moves code does not touch the list. Both lists are EXACT in both
directions: a sink that is not listed is red, and so is an entry whose sink
is gone.

* ``DEBT`` — a write the two-class contract covers and that does not go
  through an entry yet: a tracked repo file (``artifact``), a path a flag
  named (``operator``), a CI-provided file such as ``$GITHUB_OUTPUT``
  (``ci_env_file``: append, so operator), a working directory a flag named
  or that sits in the repo (``work_dir``). Meant to shrink to zero:
  ``_DEBT_CEILING`` is its size, and it moves down as sites move to an entry.
  A new write of these kinds goes through an entry, not onto this list.
  SHRINK-ONLY, held against the merge base with the target branch
  (``test_debt_does_not_grow_since_the_base_commit``): the list may not
  hold more than it did there, and no site may leave it in a change that
  adds a ``scratch`` / ``state`` site to ``REGISTERED``.
* ``REGISTERED`` — a write the contract does not cover: an intermediate
  file under a temporary directory the code itself made (``scratch``), a
  marker / lock / cache / log / git-internal file (``state``), a call the
  vocabulary matches that writes no file (``not_a_file``), the entries' own
  writes in the modules they live in (``entry``). NOT shrink-only:
  a new tool that uses ``tempfile`` adds a line here, in the diff, with its
  class.

WHAT IT DOES NOT SEE
--------------------
* **The class is a person's judgement.** Nothing checks that a site labelled
  ``scratch`` is under a temporary directory; a mislabelled entry passes
  (only that its file mentions a temporary location at all). The seed was
  classified by reading where each path comes from (#3981).
* **A swap that keeps the count**: one raw write replaced by another of the
  same kind in the same function.
* **Another function with the same name** in the same file shares a key:
  the count is per (file, enclosing function chain, sink).
* **What a sink's path is.** ``open(p, "w")`` is one sink whatever ``p`` is.
* **Code that is not Python, and child processes**: shell scripts, Go
  generators, a ``subprocess`` that writes.
* **A write the vocabulary does not name** — ``Path.replace`` /
  ``Path.rename`` (measured: mostly ``str.replace``), ``os.open`` +
  ``os.write``, ``Path(p).open(mode)`` with a variable positional mode,
  ``zipfile.ZipFile(p, "w")``, ``tempfile.NamedTemporaryFile`` /
  ``mkdtemp``, ``logging.FileHandler``, ``sqlite3``, a writer reached
  through a variable (``w = Path.write_text``) or ``getattr``; the
  vocabulary's own docstring says why for the first two.
* **Deletions**: ``unlink`` / ``os.remove`` / ``shutil.rmtree`` are not
  sinks — the gate is about what a tool writes, and one that removes a
  tracked file (``gen_agent_adapters``) is not held here.
* **A file that is Python without the suffix** (a shebang script).
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))

from _tree import repo_files  # noqa: E402

import test_output_write_sites_stay_guarded as _floor  # noqa: E402  (the sink vocabulary)

DEBT_CLASSES = ("artifact", "operator", "ci_env_file", "work_dir")
REGISTERED_CLASSES = ("scratch", "state", "not_a_file", "entry")


def entry_homes() -> frozenset[str]:
    """The files the output entries are implemented in: a raw sink there may
    be the entry itself (class ``entry``), and nowhere else. Read off
    ``OUTPUT_ENTRY_POINTS``. They are in the population like any other
    file: a helper added to one with a bare write is unlisted, and red."""
    import _lib_output
    modules = {name.split(".")[0] for names in _lib_output.OUTPUT_ENTRY_POINTS.values()
               for name in names}
    return frozenset(f"scripts/tools/{module}.py" for module in modules)


def raw_sinks(source: str, label: str = "<snippet>") -> Counter[str]:
    """``"<enclosing function chain> <sink>"`` → how many, for every raw
    write sink in *source*. ``<module>`` for module level; a lambda is
    ``<lambda>``."""
    tree = ast.parse(source, label)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    aliases = _floor._named_writer_aliases(tree)
    found: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _floor._name_of(node.func) in _floor._CLOSED_WRITERS:
            continue
        sink = _floor._classify_sink(node, aliases)
        if sink is None:
            continue
        chain, cur = [], node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                chain.append(cur.name)
            elif isinstance(cur, ast.Lambda):
                chain.append("<lambda>")
        found[f"{'.'.join(reversed(chain)) or '<module>'} {sink}"] += 1
    return found


def population() -> list[str]:
    """Every ``.py`` file of the repository under ``scripts/`` (tracked, or
    new and not ignored — ``_tree.repo_files``, so a worktree copy is not
    this tree; a scratch script left untracked there is in it)."""
    rels = (path.relative_to(REPO_ROOT).as_posix() for path in repo_files(".py"))
    return sorted(rel for rel in rels if rel.startswith("scripts/"))


def scan_tree() -> dict[str, Counter[str]]:
    return {rel: sinks for rel in population()
            if (sinks := raw_sinks((REPO_ROOT / rel).read_text(encoding="utf-8"), rel))}


def _split(key: str) -> tuple[str, str]:
    """``"main .write_text [artifact]"`` → (``"main .write_text"``, ``"artifact"``)."""
    site, _, cls = key.rpartition(" [")
    return site, cls.rstrip("]")


def listed(*lists: dict[str, dict[str, int]]) -> dict[str, Counter[str]]:
    """The lists as the scan spells its answer: file → site → count, the
    classes summed (one function may hold a scratch and an artifact write of
    the same kind)."""
    out: dict[str, Counter[str]] = {}
    for one in lists:
        for rel, sites in one.items():
            for key, count in sites.items():
                out.setdefault(rel, Counter())[_split(key)[0]] += count
    return out


def verdict(scanned: dict[str, Counter[str]], want: dict[str, Counter[str]]) -> list[str]:
    """Every difference between what the tree holds and what is listed."""
    faults = []
    for rel in sorted(set(scanned) | set(want)):
        have, said = scanned.get(rel, Counter()), want.get(rel, Counter())
        for site in sorted(set(have) | set(said)):
            if have[site] > said[site]:
                faults.append(
                    f"{rel}: {have[site] - said[site]} unlisted raw write(s) at `{site}` — "
                    "write a tracked file or a flag-named path through its entry "
                    "(_lib_output.write_repo_artifact / write_operator_output); only a "
                    "write under a temporary directory the code made, or a marker / "
                    "lock / cache, is added to REGISTERED, with its class — a tracked "
                    "file listed there is a mislabel nothing here catches")
            elif have[site] < said[site]:
                faults.append(
                    f"{rel}: `{site}` is listed {said[site]} time(s) and the file has "
                    f"{have[site]} — take the entry off (and lower _DEBT_CEILING if it "
                    "was debt)")
    return faults


# ── The lists ──────────────────────────────────────────────────────────────
# Seeded 2026-11-20 from main efd02e901 (#3981: 213 sinks read one by one).

#: Writes the contract covers, not through an entry yet. Shrink-only.
DEBT: dict[str, dict[str, int]] = {
    "scripts/mkdocs/rule_packs_bridge.py": {
        "on_pre_build .mkdir [work_dir]": 1,
        "on_pre_build shutil.copy2 [work_dir]": 1,
    },
    "scripts/ops/bench_synth_tenants.py": {
        "cmd_inject .write_text [operator]": 1,
    },
    "scripts/ops/file_race_report.py": {
        "main open(w) [ci_env_file]": 1,
    },
    "scripts/ops/inject_default_key.py": {
        "inject .mkdir [operator]": 1,
        "inject .write_text [operator]": 2,
    },
    "scripts/ops/parse_go_test_json.py": {
        "main .mkdir [operator]": 1,
        "main .write_text [operator]": 1,
    },
    "scripts/ops/plan_race_shards.py": {
        "_write_github_output open(w) [ci_env_file]": 1,
        "fetch_timings .mkdir [operator]": 1,
        "fetch_timings .write_text [operator]": 1,
        "main .mkdir [operator]": 1,
        "main .write_text [operator]": 1,
        "merge .mkdir [operator]": 1,
        "merge .open(w) [operator]": 1,
    },
    "scripts/ops/workflow_path_filter.py": {
        "cmd_assert .write_text [operator]": 1,
        "cmd_generate .write_bytes [artifact]": 1,
        # Where `record` and `merge` both write the trace file (#3756): the
        # two sites that were `cmd_record`'s, moved. `merge` is a second
        # CALLER of the same raw sinks — this ledger counts sinks, not
        # callers, so that did not show here.
        "_write_job_record .mkdir [artifact]": 1,
        "_write_job_record .write_text [artifact]": 1,
    },
    "scripts/tools/dx/analyze_bench_history.py": {
        "download_artifact .mkdir [work_dir]": 1,
        "download_artifact .write_text [work_dir]": 1,
        "main .mkdir [operator]": 1,
    },
    "scripts/tools/dx/bench_verdict.py": {
        "main open(w) [ci_env_file]": 1,
    },
    "scripts/tools/dx/bump_docs.py": {
        "_init_changelog_entry .write_text [artifact]": 2,
        "_init_changelog_entry os.chmod [artifact]": 2,
        "apply_count_updates .write_text [artifact]": 1,
        "apply_count_updates os.chmod [artifact]": 1,
        "apply_rules .write_text [artifact]": 3,
        "apply_rules os.chmod [artifact]": 3,
    },
    "scripts/tools/dx/bump_playbook_versions.py": {
        "apply_bump .write_bytes [artifact]": 1,
    },
    "scripts/tools/dx/check_release_immutable.py": {
        "write_output open(w) [ci_env_file]": 1,
    },
    "scripts/tools/dx/gen_agent_adapters.py": {
        "write_outputs open(w) [artifact]": 1,
        "write_outputs os.chmod [artifact]": 1,
        "write_outputs os.makedirs [artifact]": 1,
    },
    "scripts/tools/dx/generate_platform_data.py": {
        "main .mkdir [artifact]": 2,
        "main .write_text [artifact]": 2,
        "main os.chmod [artifact]": 2,
    },
    "scripts/tools/dx/measure_judgement_budget.py": {
        "judge os.mkdir [work_dir]": 1,
        "measure os.makedirs [work_dir]": 1,
        "prepare_repo shutil.copytree [work_dir]": 1,
        "run_judgement os.makedirs [work_dir]": 1,
    },
    "scripts/tools/dx/run_chaos_soak.py": {
        "trigger_reload .write_text [operator]": 1,
    },
    "scripts/tools/dx/suggest_related.py": {
        "main .write_text [artifact]": 1,
    },
    "scripts/tools/lint/check_head_blob_hygiene.py": {
        "_fix_working_copy .write_bytes [artifact]": 1,
    },
    "scripts/tools/lint/fix_file_hygiene.py": {
        "fix_file open(w) [artifact]": 1,
    },
    "scripts/tools/ops/_gsvc_child.py": {
        "_parent_dir os.mkdir [work_dir]": 1,
        "_write_file os.fdopen [work_dir]": 1,
        "apply_writes os.makedirs [work_dir]": 1,
        "materialise os.makedirs [work_dir]": 1,
        "materialise os.mkdir [work_dir]": 1,
        "read_tree open(w) [work_dir]": 1,
        "read_tree os.chmod [work_dir]": 1,
    },
    "scripts/tools/ops/generate_rule_pack_split.py": {
        "_safe_mkdir .mkdir [operator]": 1,
        "process_rule_packs open(w) [operator]": 1,
    },
    "scripts/tools/ops/generator_service.py": {
        "Service._new_job_dir os.mkdir [work_dir]": 1,
        "Service._new_self_check_dir os.mkdir [work_dir]": 1,
        "Service._policy_now os.makedirs [work_dir]": 1,
        "_prepare_work_dir os.makedirs [operator]": 1,
        "_prepare_work_dir os.mkdir [work_dir]": 1,
        "snapshot_policy open(w) [work_dir]": 1,
        "snapshot_policy os.chmod [work_dir]": 1,
    },
}

#: How many sinks DEBT holds: one number to read. A site that moves to an
#: entry lowers it. What stops it going UP is not this line (it is edited in
#: the same diff as the list) but the comparison with the base commit below.
_DEBT_CEILING = 71

#: Writes the two-class contract does not cover. Exact, not shrink-only.
REGISTERED: dict[str, dict[str, int]] = {
    "scripts/tools/_lib_atomic_write.py": {
        "_try_atomic os.chmod [entry]": 1,
        "_try_atomic os.fdopen [entry]": 1,
        "_try_atomic os.replace [entry]": 1,
        "_try_atomic tempfile.mkstemp [entry]": 1,
        "_write_in_place .write_text [entry]": 1,
        "_write_in_place os.chmod [entry]": 1,
    },
    "scripts/tools/_lib_output.py": {
        "_kernel_open_operator_output os.chmod [entry]": 1,
        "_operator_text io.open(w) [entry]": 1,
        "ensure_dir .mkdir [entry]": 1,
        "write_repo_artifact atomic_write_text() [entry]": 1,
    },
    # #3756. The state file and the per-segment list beside it are the
    # measurement's own working files (in CI under $RUNNER_TEMP); the canaries
    # live in the git directory; `semantics` writes only under the
    # tempfile.mkdtemp directory it makes.
    "scripts/ops/atime_trace.py": {
        "_collect open(w) [state]": 1,
        "make_canaries .mkdir [state]": 1,
        "make_canaries open(w) [state]": 2,
        "measure_semantics open(w) [scratch]": 1,
        "measure_semantics os.chmod [scratch]": 1,
        "measure_semantics os.replace [scratch]": 1,
        "measure_semantics.in_place open(w) [scratch]": 1,
        "measure_semantics.make open(w) [scratch]": 1,
        "measure_semantics.make os.makedirs [scratch]": 1,
        "save_state open(w) [state]": 1,
        "save_state os.replace [state]": 1,
    },
    "scripts/ops/bench_synth_tenants.py": {
        "apply_merge_patch os.fdopen [scratch]": 1,
        "apply_merge_patch tempfile.mkstemp [scratch]": 1,
    },
    "scripts/ops/commit_helper.py": {
        "check_commit_msg os.fdopen [scratch]": 1,
        "check_commit_msg tempfile.mkstemp [scratch]": 1,
    },
    "scripts/ops/fuse_plumbing_commit.py": {
        "plumbing_commit .mkdir [state]": 1,
        "plumbing_commit .write_text [state]": 1,
        "plumbing_commit shutil.copy [state]": 1,
    },
    "scripts/session-guards/_guardlib.py": {
        "append_jsonl .mkdir [state]": 1,
        "append_jsonl .open(w) [state]": 1,
        "state_dir .mkdir [state]": 1,
    },
    "scripts/session-guards/paths_map.py": {
        "save_fired .write_text [state]": 1,
    },
    "scripts/session-guards/session-init.py": {
        "_do_init .mkdir [state]": 1,
        "_do_init .write_text [state]": 1,
        "_heal_pre_commit_shebang .write_text [state]": 1,
        "_heal_pre_commit_shebang os.chmod [state]": 1,
        "_install_commit_msg_hook .mkdir [state]": 1,
        "_install_commit_msg_hook .write_bytes [state]": 1,
        "_install_commit_msg_hook os.chmod [state]": 1,
        "_touch_heartbeat .mkdir [state]": 1,
        "_touch_heartbeat .write_text [state]": 1,
        "_write_log .mkdir [state]": 1,
        "_write_log .open(w) [state]": 1,
    },
    "scripts/session-guards/stop_evidence.py": {
        "run_hook .write_text [state]": 1,
    },
    "scripts/session-guards/vscode_git_toggle.py": {
        "save_settings .mkdir [state]": 1,
        "save_settings open(w) [state]": 1,
    },
    "scripts/tools/_lib_confedit.py": {
        "NewFile.preview open(w) [scratch]": 1,
        "NewFile.preview os.chmod [scratch]": 1,
    },
    "scripts/tools/_lib_consumer_read.py": {
        "prometheus_rule_text_results os.chmod [scratch]": 1,
        "prometheus_rule_text_results os.fdopen [scratch]": 1,
    },
    "scripts/tools/_lib_exitcodes.py": {
        "sanitize_std_fds open(w) [not_a_file]": 2,
    },
    "scripts/tools/_lib_tenant_uniqueness.py": {
        "verdict_for .mkdir [scratch]": 1,
        "verdict_for shutil.copy2 [scratch]": 1,
    },
    "scripts/tools/dx/_recipe_preview.py": {
        "judge .write_text [scratch]": 1,
        "preview_recipe .mkdir [scratch]": 1,
        "preview_recipe .write_text [scratch]": 2,
    },
    "scripts/tools/dx/check_release_immutable.py": {
        "_helm_show_chart open(w) [scratch]": 1,
        "_helm_show_chart os.chmod [scratch]": 1,
    },
    "scripts/tools/dx/generate_rulepack_derived.py": {
        "rewrite_problems .mkdir [scratch]": 1,
        "rewrite_problems .write_text [scratch]": 1,
    },
    "scripts/tools/dx/pr_preflight.py": {
        "write_marker .mkdir [state]": 1,
        "write_marker .touch [state]": 1,
    },
    "scripts/tools/lint/check_admin_config_schema.py": {
        "config_check open(w) [scratch]": 1,
        "config_check os.chmod [scratch]": 1,
    },
    "scripts/tools/lint/check_chart_version_bump.py": {
        "_git_visible_copy .mkdir [scratch]": 1,
        "_git_visible_copy .write_bytes [scratch]": 1,
        "package_digest .mkdir [scratch]": 1,
    },
    "scripts/tools/lint/check_cli_contract.py": {
        "portal_commands .write_text [scratch]": 1,
    },
    "scripts/tools/lint/check_exporter_queries.py": {
        "served_names .write_text [scratch]": 1,
    },
    "scripts/tools/lint/check_iac_helm.py": {
        "kube_linter_lint .write_text [scratch]": 2,
    },
    "scripts/tools/lint/check_iac_vibe_rules.py": {
        "run_hadolint .write_text [scratch]": 1,
    },
    "scripts/tools/lint/validate_mermaid.py": {
        "MermaidValidator._ensure_puppeteer_config os.fdopen [scratch]": 1,
        "MermaidValidator._ensure_puppeteer_config tempfile.mkstemp [scratch]": 1,
        "MermaidValidator._mmdc_once .write_text [scratch]": 1,
    },
    "scripts/tools/ops/_registry_lib.py": {
        "_confd_batches os.fdopen [scratch]": 1,
        "_confd_batches os.mkdir [scratch]": 1,
    },
    "scripts/tools/ops/backtest_threshold.py": {
        "_written_tree open(w) [scratch]": 1,
        "_written_tree os.chmod [scratch]": 1,
    },
    "scripts/tools/ops/batch_diagnose.py": {
        "configmap_tree open(w) [scratch]": 1,
        "configmap_tree os.mkdir [scratch]": 1,
    },
    "scripts/tools/ops/deprecate_rule.py": {
        "exporter_after_writes .mkdir [scratch]": 1,
        "exporter_after_writes .write_bytes [scratch]": 1,
    },
    "scripts/tools/ops/explain_route.py": {
        "run_amtool_trace open(w) [scratch]": 1,
        "run_amtool_trace os.chmod [scratch]": 1,
    },
    "scripts/tools/ops/generator_service.py": {
        "Service._verdict .touch [not_a_file]": 1,
    },
    "scripts/tools/ops/grafana_import.py": {
        "import_dashboard .write_bytes [scratch]": 1,
    },
    "scripts/tools/ops/migrate_builtin_defaults.py": {
        "_served_after shutil.copytree [scratch]": 1,
    },
    "scripts/tools/ops/migrate_credentials.py": {
        "_still_refused open(w) [scratch]": 1,
        "_still_refused os.chmod [scratch]": 1,
        "_still_refused shutil.copytree [scratch]": 1,
    },
    "scripts/tools/ops/patch_config.py": {
        "_ConfdView.__init__ open(w) [scratch]": 1,
        "_ConfdView.__init__ os.chmod [scratch]": 1,
        "config_hash open(w) [scratch]": 1,
        "config_hash os.chmod [scratch]": 1,
    },
    "scripts/tools/validate_all.py": {
        "_isolated_tree .mkdir [scratch]": 1,
        "_isolated_tree shutil.copy2 [scratch]": 1,
        "main .write_text [state]": 1,
        "main open(w) [state]": 1,
        "main os.chmod [state]": 2,
        "main os.replace [state]": 1,
    },
}


# ── The gate ───────────────────────────────────────────────────────────────
def test_every_raw_write_sink_is_listed_and_every_entry_is_real():
    scanned = scan_tree()
    assert len(scanned) > 40, "the scan found almost nothing: it measured the wrong tree"
    faults = verdict(scanned, listed(DEBT, REGISTERED))
    assert not faults, "raw write sinks and the lists disagree:\n  " + "\n  ".join(faults)


def test_the_debt_ceiling_is_the_size_of_the_debt_list():
    """One number to read. It does not stop a raise made in the same diff as
    a new entry — `test_debt_does_not_grow_since_the_base_commit` does."""
    size = sum(count for sites in DEBT.values() for count in sites.values())
    assert size == _DEBT_CEILING, (
        f"DEBT holds {size} sink(s) and _DEBT_CEILING says {_DEBT_CEILING}: lower the "
        "ceiling with the entries you removed (it cannot go up: the comparison "
        "with the base commit refuses a larger DEBT)")


# ── Shrink-only, against the merge base with the default branch ────────────
_SELF = "tests/shared/test_raw_write_sinks_are_listed.py"


def lists_in(source: str) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """(DEBT, REGISTERED) as *source* — a revision of this file — writes
    them. Read as literals, never executed: the base commit's code is not
    this run's code."""
    found = {node.target.id: ast.literal_eval(node.value)
             for node in ast.parse(source).body
             if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
             and node.target.id in ("DEBT", "REGISTERED")}
    assert set(found) == {"DEBT", "REGISTERED"}, (
        f"the two list literals were not both found ({sorted(found)}): they are "
        "annotated assignments of dict literals at module level, and stay that")
    return found["DEBT"], found["REGISTERED"]


def _total(one: dict[str, dict[str, int]], classes: tuple[str, ...]) -> int:
    return sum(count for sites in one.values() for key, count in sites.items()
               if _split(key)[1] in classes)


def debt_regressions(base: tuple[dict, dict], head: tuple[dict, dict]) -> list[str]:
    """What *head*'s lists do that a shrink-only DEBT forbids, against
    *base*'s.

    1. DEBT holds more sinks than it did.
    2. A site left DEBT in the same change in which REGISTERED gained a
       ``scratch`` / ``state`` site. That is what a relabel looks like, under
       whatever key the site ends up — renamed function, other sink kind,
       other file — and the room it frees in DEBT can take a new debt with
       the total unchanged. It is also what "pay one debt and add a scratch
       file" looks like: the two cannot be told apart from the lists, so
       they go in separate changes."""
    faults = []
    was, now = _total(base[0], DEBT_CLASSES), _total(head[0], DEBT_CLASSES)
    if now > was:
        faults.append(f"DEBT grew from {was} to {now} sink(s): a new tracked-file or "
                      "flag-named write goes through an entry, not onto the list")
    debt_was, debt_now = listed(base[0]), listed(head[0])
    left = sorted(f"{rel}: {site}" for rel, sites in debt_was.items()
                  for site, count in sites.items()
                  if debt_now.get(rel, Counter())[site] < count)
    kept = ("scratch", "state")
    reg_was, reg_now = _total(base[1], kept), _total(head[1], kept)
    if left and reg_now > reg_was:
        faults.append(
            f"REGISTERED gained {reg_now - reg_was} scratch/state site(s) in the same "
            f"change in which DEBT lost {left}: from the lists that is a relabel. Debt is "
            "paid by moving the write to an entry; a new scratch or state write, or a "
            "rename of a function that holds debt, goes in its own change")
    return faults


def base_commit() -> str:
    """The merge base of HEAD and the branch this change targets — resolved
    by the repo's one resolver (``$LINT_DIFF_BASE``, else
    ``origin/$GITHUB_BASE_REF``, else ``origin/main``). On a pull request's
    CI checkout (the merge commit) that is the tip of the target branch;
    locally it is where the branch forked. On the default branch itself it
    is HEAD, and the comparison is of a list with itself: the growth was
    stopped on the pull request.

    ⛔ Fails — never skips — when it cannot be resolved: a comparison that
    was not made is not a comparison that passed. ``fetch-depth: 0`` does
    not guarantee an ``origin/<base>`` ref; the CI jobs that run this fetch
    it in a step of their own."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "lint"))
    from _lint_helpers import DiffBaseMissingError, resolve_diff_base_ref
    try:
        _ref, commit = resolve_diff_base_ref()
    except DiffBaseMissingError as exc:
        pytest.fail(f"the shrink-only comparison of DEBT was NOT made:\n{exc}")
    return commit


def lists_at(commit: str) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """The two lists as *commit* holds them. ⛔ No "that commit has no such
    file, so pass": every commit a change can be based on holds it (it
    arrived with the lists, #4124), so one without it is a comparison that
    could not be made — a failure, like an unresolved base."""
    shown = subprocess.run(["git", "show", f"{commit}:{_SELF}"], cwd=REPO_ROOT,
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    if shown.returncode != 0:
        pytest.fail(f"cannot read {_SELF} at the base commit {commit} "
                    f"({shown.stderr.strip()}): the shrink-only comparison of DEBT was "
                    "NOT made. A base older than the lists is a stale base ref — "
                    "`git fetch origin main`, or set $LINT_DIFF_BASE.")
    return lists_in(shown.stdout)


def test_debt_does_not_grow_since_the_base_commit():
    """`_DEBT_CEILING` and the list are edited in one diff, so this file's
    own text cannot hold "shrink-only" — measured: a new entry with the
    ceiling raised, and an entry moved to REGISTERED with the code unchanged,
    both passed. The lists of the base commit can."""
    base = base_commit()
    faults = debt_regressions(lists_at(base), (DEBT, REGISTERED))
    assert not faults, f"against the base commit {base[:9]}:\n  " + "\n  ".join(faults)


def test_the_base_comparison_says_no():
    base = ({"scripts/x.py": {"main .write_text [artifact]": 2}},
            {"scripts/x.py": {"main open(w) [scratch]": 1},
             "scripts/tools/_lib_output.py": {"ensure_dir .mkdir [entry]": 1}})
    assert debt_regressions(base, base) == []
    # paid: one site moved to an entry
    paid = {"scripts/x.py": {"main .write_text [artifact]": 1}}
    assert debt_regressions(base, (paid, base[1])) == []
    # a function that holds debt renamed, nothing else: the total holds
    assert debt_regressions(base, ({"scripts/x.py": {"run .write_text [artifact]": 2}},
                                   base[1])) == []
    # a new scratch site, debt untouched
    more = {**base[1], "scripts/y.py": {"f open(w) [scratch]": 1}}
    assert debt_regressions(base, (base[0], more)) == []
    # one more debt entry
    grown = {**base[0], "scripts/y.py": {"f open(w) [operator]": 1}}
    assert len(debt_regressions(base, (grown, base[1]))) == 1
    # relabelled, same key
    same_key = {"scripts/x.py": {"main open(w) [scratch]": 1, "main .write_text [scratch]": 1}}
    assert len(debt_regressions(base, (paid, {**base[1], **same_key}))) == 1
    # relabelled under another key: renamed function, other sink kind, other file
    for key in ("run .write_text [scratch]", "main open(w) [state]"):
        moved = {**base[1], "scripts/z.py": {key: 1}}
        assert len(debt_regressions(base, (paid, moved))) == 1, key
    # relabelled, and the room it freed taken by a new debt: the total holds
    swapped = {"scripts/x.py": {"main .write_text [artifact]": 1},
               "scripts/new.py": {"f .write_text [artifact]": 1}}
    assert len(debt_regressions(base, (swapped, {**base[1], "scripts/z.py":
                                                 {"run .write_text [scratch]": 1}}))) == 1
    # an entry module's own sink added while debt is paid is not a relabel
    entry = {**base[1], "scripts/tools/_lib_output.py": {"ensure_dir .mkdir [entry]": 2}}
    assert debt_regressions(base, (paid, entry)) == []


def test_the_lists_are_read_back_from_this_files_own_source():
    """`lists_in` is what reads the base commit: on this revision it must
    read exactly the lists this module holds."""
    assert lists_in(Path(__file__).read_text(encoding="utf-8")) == (DEBT, REGISTERED)
    with pytest.raises(AssertionError, match="not both found"):
        lists_in("DEBT: dict = {}\nREGISTERED = {}\n")


def _how_it_ends(call) -> str:
    """``"failed: <message>"``, ``"skipped"`` or ``"returned"``. A skip is
    caught here on purpose: ``pytest.raises(pytest.fail.Exception)`` lets one
    through, and the test around it would then be SKIPPED, which a CI run
    reads as green — measured, on the first version of this control."""
    try:
        call()
    except pytest.fail.Exception as exc:
        return f"failed: {exc}"
    except pytest.skip.Exception:
        return "skipped"
    return "returned"


def test_a_comparison_that_cannot_be_made_is_a_failure(monkeypatch):
    """Not a skip, not a pass: a base ref that does not resolve, and a base
    that does not hold this file. Softened to either, this is what turns red."""
    assert base_commit(), "control: the base resolves on this checkout"
    roots = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=REPO_ROOT,
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    root = roots.stdout.split()[0]
    unreadable = _how_it_ends(lambda: lists_at(root))
    assert unreadable.startswith("failed:") and "NOT made" in unreadable, unreadable
    monkeypatch.setenv("LINT_DIFF_BASE", "origin/no-such-branch-3981")
    unresolved = _how_it_ends(base_commit)
    assert unresolved.startswith("failed:") and "NOT made" in unresolved, unresolved


def test_every_entry_names_a_class_of_its_list_and_a_tracked_file():
    files = set(population())
    for name, one, classes in (("DEBT", DEBT, DEBT_CLASSES),
                               ("REGISTERED", REGISTERED, REGISTERED_CLASSES)):
        for rel, sites in one.items():
            assert rel in files, f"{name}: {rel} is not a tracked file of the population"
            assert sites, f"{name}: {rel} lists nothing — drop it"
            for key, count in sites.items():
                site, cls = _split(key)
                assert cls in classes and site and count > 0, (
                    f"{name}: {rel}: {key!r}: {count} — the class is one of {classes}")


def test_no_key_of_either_list_is_written_twice():
    """A dict literal keeps the LAST of two equal keys and says nothing: a
    file or a site written twice would drop an entry, and the sums above
    would be of what is left. Read from this file's own source."""
    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    seen = 0
    for node in ast.walk(module):
        if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id in ("DEBT", "REGISTERED")):
            continue
        seen += 1
        assert isinstance(node.value, ast.Dict)
        for literal in (node.value, *node.value.values):
            keys = [key.value for key in literal.keys]
            twice = sorted(k for k, n in Counter(keys).items() if n > 1)
            assert not twice, f"{node.target.id}: written twice: {twice}"
    assert seen == 2, "the two list literals were not found: this test measured nothing"


def test_an_entry_class_site_is_in_an_entry_module_and_only_there():
    """``entry`` says "this raw write IS the entry": true only in the modules
    ``OUTPUT_ENTRY_POINTS`` names. Those modules are in the population like
    any other file, so a helper added to one with a bare write is unlisted."""
    homes = entry_homes()
    assert {"scripts/tools/_lib_output.py", "scripts/tools/_lib_atomic_write.py"} <= homes
    assert homes <= set(population()), "an entry module is missing from the population"
    for rel, sites in REGISTERED.items():
        classes = {_split(key)[1] for key in sites}
        if rel in homes:
            assert classes == {"entry"}, f"{rel}: an entry module lists {classes}"
        else:
            assert "entry" not in classes, f"{rel} is not an entry module"
    assert not set(DEBT) & homes


def test_no_file_reaches_a_sink_module_under_another_name():
    """The vocabulary knows ``os.replace`` and ``shutil.copy2`` by the
    module's own name. ``import os as _os`` or ``from shutil import move``
    would put every such call out of its sight — measured: none of them is
    caught — so no file of the population may spell a sink module, or a
    sink taken from one, any other way."""
    modules = set(_floor.MODULE_SINKS) | set(_floor.OPEN_MODULE_NAMES)
    names = ({name for sinks in _floor.MODULE_SINKS.values() for name in sinks}
             | {name for opens in _floor.OPEN_MODULE_NAMES.values() for name in opens})
    renamed = []
    for rel in population():
        for node in ast.walk(ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), rel)):
            if isinstance(node, ast.Import):
                renamed += [f"{rel}:{node.lineno} import {a.name} as {a.asname}"
                            for a in node.names
                            if a.name in modules and a.asname not in (None, a.name)]
            elif isinstance(node, ast.ImportFrom) and node.module in modules:
                renamed += [f"{rel}:{node.lineno} from {node.module} import {a.name}"
                            for a in node.names if a.name in names]
    assert not renamed, ("a sink module under another name hides its writes from this "
                         "gate — import the module plainly:\n  " + "\n  ".join(renamed))


def test_a_file_with_a_scratch_site_names_a_temporary_location():
    """The cheap half of "is it really scratch": a file that lists one says
    ``tempfile`` or ``tmp`` somewhere. It cannot tell which write goes
    there — a generator with no temporary location at all cannot hide a
    tracked-file write under this class, and that is all it holds."""
    for rel, sites in REGISTERED.items():
        if any(_split(key)[1] == "scratch" for key in sites):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8").lower()
            assert "tempfile" in text or "tmp" in text, (
                f"{rel} lists a scratch write and names no temporary location")


# ── Controls: the scanner and the verdict, on hand-made input ──────────────
_SNIPPET = """
import os, shutil, tempfile
from pathlib import Path
from _lib_output import write_repo_artifact
def gen(out):
    Path(out).write_text("x")
    write_repo_artifact(out, "x")          # an entry: not a sink
    open(out).read()                        # a read: not a sink
class Fixer:
    def fix(self, p):
        with open(p, "w") as fh:
            fh.write("x")
        os.chmod(p, 0o644)
        os.replace(p, p + ".bak")
with tempfile.TemporaryDirectory() as td:
    (Path(td) / "a").write_text("x")
    shutil.copy2("a", td)
"""


def test_the_scanner_keys_a_sink_by_its_function_chain():
    assert raw_sinks(_SNIPPET) == Counter({
        "gen .write_text": 1,
        "Fixer.fix open(w)": 1, "Fixer.fix os.chmod": 1, "Fixer.fix os.replace": 1,
        "<module> .write_text": 1, "<module> shutil.copy2": 1,
    })


def test_a_new_sink_a_removed_one_and_a_moved_one_are_each_a_fault():
    want = {"scripts/x.py": Counter({"main .write_text": 1})}
    assert verdict({"scripts/x.py": Counter({"main .write_text": 1})}, want) == []
    # one more in the same function; one in a new file; the listed one gone
    assert len(verdict({"scripts/x.py": Counter({"main .write_text": 2})}, want)) == 1
    assert len(verdict({**want, "scripts/y.py": Counter({"f open(w)": 1})}, want)) == 1
    assert len(verdict({}, want)) == 1
    # moved to another function: the old key is stale AND the new one unlisted
    assert len(verdict({"scripts/x.py": Counter({"helper .write_text": 1})}, want)) == 2


def test_the_two_lists_sum_per_site():
    debt = {"scripts/x.py": {"main .write_text [artifact]": 1}}
    registered = {"scripts/x.py": {"main .write_text [scratch]": 2}}
    assert listed(debt, registered) == {"scripts/x.py": Counter({"main .write_text": 3})}


@pytest.mark.parametrize("rel", ["scripts/tools/dx/gen_agent_adapters.py",
                                 "scripts/tools/lint/check_iac_helm.py",
                                 "scripts/session-guards/session-init.py"])
def test_the_scan_reaches_each_kind_of_tree(rel):
    """dx generators, lint scratch files and the session guards are all in
    the population — none is excluded by where it lives."""
    assert rel in population() and scan_tree().get(rel)
