"""Every raw write sink under ``scripts/`` is on one of two lists (#3981 c).

WHAT THIS GATE IS FOR
---------------------
A tool's file writes are meant to go through the two entries of
``_lib_io.OUTPUT_ENTRY_POINTS`` — an operator output or a repo artifact
(``docs/internal/lint-policy.md``). The run-time write gate
(``test_output_path_write_failure``) holds the tools that take an output
flag to that, per flag. It never sees a tool with no such flag, or one that
never calls a writer by name: a generator that rewrites a tracked file with
``Path.write_text``, a ``--fix`` mode, a linter's scratch file. This gate is
for those, and it runs no tool.

Population: every Python file of the repository under ``scripts/`` —
derived, no tool is named — minus the modules the entries themselves live in (read off
``OUTPUT_ENTRY_POINTS``). Sinks: the vocabulary of
``test_output_write_sites_stay_guarded`` (a write-mode ``open``,
``write_text`` / ``write_bytes``, ``mkdir``, ``chmod``, ``shutil.*``,
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
  or that sits in the repo (``work_dir``). ⛔ SHRINK-ONLY, to zero:
  ``_DEBT_CEILING`` is its size, and it moves down as sites move to an entry.
  A new write of these kinds goes through an entry, not onto this list.
* ``REGISTERED`` — a write the contract does not cover: an intermediate
  file under a temporary directory the code itself made (``scratch``), a
  marker / lock / cache / log / git-internal file (``state``), a call the
  vocabulary matches that writes no file (``not_a_file``). NOT shrink-only:
  a new tool that uses ``tempfile`` adds a line here, in the diff, with its
  class.

WHAT IT DOES NOT SEE
--------------------
* **The class is a person's judgement.** Nothing checks that a site labelled
  ``scratch`` is under a temporary directory; a mislabelled entry passes.
  The seed was classified by reading where each path comes from (#3981).
* **Another function with the same name** in the same file shares a key:
  the count is per (file, enclosing function chain, sink).
* **What a sink's path is.** ``open(p, "w")`` is one sink whatever ``p`` is.
* **Code that is not Python, and child processes**: shell scripts, Go
  generators, a ``subprocess`` that writes.
* **A write the vocabulary does not name** — ``Path.replace`` /
  ``Path.rename`` (measured: mostly ``str.replace``), ``os.open``,
  ``sqlite3``; the vocabulary's own docstring says why.
"""
from __future__ import annotations

import ast
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
REGISTERED_CLASSES = ("scratch", "state", "not_a_file")


def entry_homes() -> frozenset[str]:
    """The files the output entries are implemented in: their raw sinks ARE
    the entries. Read off ``OUTPUT_ENTRY_POINTS``, so a new entry module is
    excluded by being registered there and by nothing else."""
    import _lib_io
    modules = {name.split(".")[0] for names in _lib_io.OUTPUT_ENTRY_POINTS.values()
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
    """Every Python file of the repository under ``scripts/`` (tracked, or
    new and not ignored — ``_tree.repo_files``, so a worktree copy is not
    this tree), minus the entries' own modules."""
    homes = entry_homes()
    rels = (path.relative_to(REPO_ROOT).as_posix() for path in repo_files(".py"))
    return sorted(rel for rel in rels if rel.startswith("scripts/") and rel not in homes)


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
                    "(_lib_io.write_repo_artifact / write_operator_output); only a "
                    "scratch or state write is added, to REGISTERED, with its class")
            elif have[site] < said[site]:
                faults.append(
                    f"{rel}: `{site}` is listed {said[site]} time(s) and the file has "
                    f"{have[site]} — take the entry off (and lower _DEBT_CEILING if it "
                    "was debt)")
    return faults


# ── The lists ──────────────────────────────────────────────────────────────
# Seeded 2026-11-20 from main efd02e901 (#3981: 213 sinks read one by one;
# the 10 inside the entries' own modules are not here).

#: Writes the contract covers, not through an entry yet. Shrink-only.
DEBT: dict[str, dict[str, int]] = {
    "scripts/mkdocs/rule_packs_bridge.py": {
        "on_pre_build .mkdir [work_dir]": 1,
        "on_pre_build shutil.copy2 [work_dir]": 1,
    },
    "scripts/ops/add_t_parallel.py": {
        "process_file .write_text [artifact]": 1,
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
    "scripts/ops/route_metrics_through_field.py": {
        "process_file .write_text [artifact]": 1,
    },
    "scripts/ops/use_get_metrics.py": {
        "process_file .write_text [artifact]": 1,
    },
    "scripts/ops/workflow_path_filter.py": {
        "cmd_assert .write_text [operator]": 1,
        "cmd_generate .write_bytes [artifact]": 1,
        "cmd_record .mkdir [artifact]": 1,
        "cmd_record .write_text [artifact]": 1,
    },
    "scripts/tools/_lib_exporter_presence.py": {
        "main .write_text [artifact]": 1,
    },
    "scripts/tools/dx/add_frontmatter.py": {
        "write_file_content open(w) [artifact]": 1,
    },
    "scripts/tools/dx/analyze_bench_history.py": {
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
    "scripts/tools/dx/gen_adr_checks.py": {
        "run .write_text [artifact]": 1,
    },
    "scripts/tools/dx/gen_agent_adapters.py": {
        "write_outputs open(w) [artifact]": 1,
        "write_outputs os.chmod [artifact]": 1,
        "write_outputs os.makedirs [artifact]": 1,
    },
    "scripts/tools/dx/gen_am_duration_json.py": {
        "main .mkdir [artifact]": 1,
        "main .write_text [artifact]": 1,
    },
    "scripts/tools/dx/gen_recipe_contract.py": {
        "run .mkdir [artifact]": 1,
        "run .write_text [artifact]": 1,
    },
    "scripts/tools/dx/gen_tenant_id_json.py": {
        "main .mkdir [artifact]": 1,
        "main .write_text [artifact]": 1,
    },
    "scripts/tools/dx/generate_alert_reference.py": {
        "main open(w) [artifact]": 2,
    },
    "scripts/tools/dx/generate_byo_rulepack_table.py": {
        "main atomic_write_text() [artifact]": 1,
    },
    "scripts/tools/dx/generate_crd_schemas.py": {
        "run .mkdir [artifact]": 1,
        "run .write_text [artifact]": 2,
    },
    "scripts/tools/dx/generate_doc_map.py": {
        "main atomic_write_text() [artifact]": 1,
        "main open(w) [artifact]": 1,
        "main os.chmod [artifact]": 1,
    },
    "scripts/tools/dx/generate_platform_data.py": {
        "main .mkdir [artifact]": 2,
        "main .write_text [artifact]": 2,
        "main os.chmod [artifact]": 2,
    },
    "scripts/tools/dx/generate_rule_pack_readme.py": {
        "main open(w) [artifact]": 1,
    },
    "scripts/tools/dx/generate_rule_pack_stats.py": {
        "main .mkdir [artifact]": 1,
        "main atomic_write_text() [artifact]": 2,
    },
    "scripts/tools/dx/generate_rulepack_configmaps.py": {
        "main .write_text [artifact]": 1,
    },
    "scripts/tools/dx/generate_rulepack_derived.py": {
        "main .write_text [artifact]": 1,
    },
    "scripts/tools/dx/generate_tool_map.py": {
        "main .write_text [artifact]": 1,
        "main atomic_write_text() [artifact]": 1,
        "main os.chmod [artifact]": 1,
    },
    "scripts/tools/dx/measure_judgement_budget.py": {
        "judge os.mkdir [work_dir]": 1,
        "measure os.makedirs [work_dir]": 1,
        "prepare_repo shutil.copytree [work_dir]": 1,
        "run_judgement os.makedirs [work_dir]": 1,
    },
    "scripts/tools/dx/migrate_ssot_language.py": {
        "execute_plan .write_text [artifact]": 2,
    },
    "scripts/tools/dx/renovate_fixup.py": {
        "_write .write_text [artifact]": 1,
    },
    "scripts/tools/dx/run_chaos_soak.py": {
        "trigger_reload .write_text [operator]": 1,
    },
    "scripts/tools/dx/scaffold_lint.py": {
        "main .mkdir [artifact]": 1,
        "main .write_text [artifact]": 1,
    },
    "scripts/tools/dx/suggest_related.py": {
        "main .write_text [artifact]": 1,
    },
    "scripts/tools/dx/sync_schema.py": {
        "run .write_text [artifact]": 1,
    },
    "scripts/tools/dx/sync_tool_registry.py": {
        "sync_hub_cards .write_text [artifact]": 1,
        "sync_tool_meta .write_text [artifact]": 1,
    },
    "scripts/tools/lint/check_cli_contract.py": {
        "write_baseline .write_text [artifact]": 1,
    },
    "scripts/tools/lint/check_doc_links.py": {
        "_fix_broken_anchors .write_text [artifact]": 1,
    },
    "scripts/tools/lint/check_head_blob_hygiene.py": {
        "_fix_working_copy .write_bytes [artifact]": 1,
    },
    "scripts/tools/lint/check_includes_sync.py": {
        "_create_en_stub .write_text [artifact]": 1,
        "_create_en_stub os.chmod [artifact]": 1,
    },
    "scripts/tools/lint/check_open_encoding.py": {
        "write_subprocess_baseline .write_text [artifact]": 1,
    },
    "scripts/tools/lint/check_percent_render_pinned.py": {
        "generate .write_text [artifact]": 1,
    },
    "scripts/tools/lint/check_repo_name.py": {
        "scan_file_full open(w) [artifact]": 1,
    },
    "scripts/tools/lint/check_vmalert_coverage.py": {
        "generate .write_text [artifact]": 1,
    },
    "scripts/tools/lint/fix_file_hygiene.py": {
        "fix_file open(w) [artifact]": 1,
    },
    "scripts/tools/lint/validate_docs_versions.py": {
        "_auto_fix .write_text [artifact]": 1,
        "_auto_fix os.chmod [artifact]": 1,
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

#: How many sinks DEBT holds. It only goes DOWN: a site that moves to an
#: entry lowers it, and nothing is added (a new tracked-file or flag-named
#: write goes through an entry from the start).
_DEBT_CEILING = 117

#: Writes the two-class contract does not cover. Exact, not shrink-only.
REGISTERED: dict[str, dict[str, int]] = {
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
    "scripts/ops/path_trace/sitecustomize.py": {
        "_install os.makedirs [state]": 1,
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
    "scripts/tools/dx/analyze_bench_history.py": {
        "download_artifact .mkdir [state]": 1,
        "download_artifact .write_text [state]": 1,
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


def test_debt_only_shrinks():
    size = sum(count for sites in DEBT.values() for count in sites.values())
    assert size == _DEBT_CEILING, (
        f"DEBT holds {size} sink(s) and _DEBT_CEILING says {_DEBT_CEILING}: lower the "
        "ceiling with the entries you removed; a raise needs the owner (#3981)")


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


def test_the_entries_own_modules_are_out_and_nothing_else_is():
    homes = entry_homes()
    assert "scripts/tools/_lib_io.py" in homes and "scripts/tools/_lib_atomic_write.py" in homes
    for rel in homes:
        assert (REPO_ROOT / rel).is_file(), f"{rel}: an entry module that is not there"
    assert not homes & set(population())
    assert not (set(DEBT) | set(REGISTERED)) & homes


# ── Controls: the scanner and the verdict, on hand-made input ──────────────
_SNIPPET = """
import os, shutil, tempfile
from pathlib import Path
from _lib_io import write_repo_artifact
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


@pytest.mark.parametrize("rel", ["scripts/tools/dx/generate_tool_map.py",
                                 "scripts/tools/lint/check_iac_helm.py",
                                 "scripts/session-guards/session-init.py"])
def test_the_scan_reaches_each_kind_of_tree(rel):
    """dx generators, lint scratch files and the session guards are all in
    the population — none is excluded by where it lives."""
    assert rel in population() and scan_tree().get(rel)
