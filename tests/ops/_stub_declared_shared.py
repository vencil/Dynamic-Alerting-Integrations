"""One copy of the tenant declared-key stub's format expectations (#1321).

Two generators write that block — ``init_project._gen_tenant_yaml`` (en) and
``scaffold_tenant.write_outputs`` (zh) — through one renderer
(``_registry_lib.render_tenant_declared_stub_lines``). Their test modules were
each carrying their own ``_STUB_KEY_LINE`` / ``_stub_key_lines`` /
``_paste_declared_lines_under_tenant`` / ``_shipped_chart_declared_keys``,
identical except for the placeholder string and the repo-root derivation.

⛔ That is a silent-weakening shape, not merely duplication: both copies pin the
SAME stub format, so a format change plus an update to only one copy leaves the
other package green while it has stopped measuring anything. One module means a
format change can only be absorbed here, where BOTH packages see it.

⚠️ Deliberately NOT named ``test_*`` — ``pyproject.toml`` sets
``python_files = ["test_*.py"]``, so a ``test_``-prefixed helper would be
collected as a (empty) test module. ``verify_diff`` indexes non-``test_``
modules under ``tests/**`` by stem for its import map
(``build_module_index``), so once this file is tracked, editing it selects
every test module that imports it.

What is NOT here, on purpose:
  * the placeholder is exported as a per-language mapping, not read from
    ``_registry_lib`` — the tests are supposed to state the expected format
    independently of the code that produces it;
  * ``shipped_chart_declared_keys`` takes the values-file PATH rather than
    building it, so each test module keeps its own literal
    ``os.path.join(..., "helm", "threshold-exporter", "values.yaml")``.
    ``verify_diff.build_map`` scans only ``test_*.py`` for path references, so
    hiding that join in here would drop
    ``helm/threshold-exporter/values.yaml`` -> those modules from ``text_map``,
    i.e. chart edits would stop selecting the tests that check the chart.
"""
import os
import re

import yaml

# tests/ops/_stub_declared_shared.py -> tests/ops -> tests -> repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# A stub key line, whatever sits on its right-hand side. Deliberately looser
# than "line that carries the placeholder": the regression this guards against
# is a well-meant `key: 300`, so the extractor must still SEE such a line
# rather than filter it out and report a tidy pass.
STUB_KEY_LINE = re.compile(r'^#\s{3,}([a-z][a-z0-9_]*):(.*)$')

# The quoted placeholder as it appears ON the key line, per generator language.
STUB_PLACEHOLDER = {'en': '"<your value>"', 'zh': '"<你的值>"'}

# The same placeholder as YAML parses it once the tenant pastes the line in.
STUB_PLACEHOLDER_VALUE = {'en': '<your value>', 'zh': '<你的值>'}


def stub_key_lines(text):
    """[(key, rhs), …] for the commented key lines of the declared block."""
    return [(m.group(1), m.group(2))
            for m in (STUB_KEY_LINE.match(ln) for ln in text.split('\n')) if m]


def paste_declared_lines_under_tenant(text, tenant):
    """Do literally what the stub tells the tenant to do; return the new text.

    "copy a whole line under your tenant above, drop the leading '# '" — pasted
    as the FIRST entry of the tenant's own mapping, which is both the natural
    reading and the placement that keeps the surrounding indentation
    load-bearing (appending at the very end of a nested block would let a
    too-deep line parse as a sibling of the last nested key). Also applies the
    prose's own precondition: delete a ` {}` flow mapping before pasting under
    it.
    """
    lines = text.split('\n')
    pasted = [ln[2:] for ln in lines if STUB_KEY_LINE.match(ln)]
    assert pasted, 'nothing to paste — the declared block is missing'
    anchor = re.compile(r'^(\s*)%s:(\s*\{\})?\s*$' % re.escape(tenant))
    out, done = [], False
    for line in lines:
        m = anchor.match(line)
        if m and not done:
            out.append(f'{m.group(1)}{tenant}:')
            out.extend(pasted)
            done = True
            continue
        out.append(line)
    assert done, f'no `{tenant}:` line to paste under'
    return '\n'.join(out)


def shipped_chart_declared_keys(values_path):
    """The declared list as it is actually SHIPPED, read from the artifact.

    Deliberately not `shipped_optional_keys_for_packs`: that is the function the
    generators themselves call, so it cannot disagree with them. The chart
    values file is written by a different producer (the registry `--regen`
    block) and is the reachability gate's own assertion source.
    """
    with open(values_path, encoding='utf-8') as f:
        shipped = yaml.safe_load(f)['thresholdConfig']['optional_overrides']
    assert shipped, f'{values_path} ships an empty declared list — nothing to check against'
    return set(shipped)
