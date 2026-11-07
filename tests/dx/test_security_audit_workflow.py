"""#1488：vibe-security-audit 的 workflow 腳本必須把快照的 workspaceRoot 交給每個 agent。

攻擊類別的 `files:` 以 repo 相對的 `components/...`、`scripts/...` 點名跨元件
路徑。腳本若不提供解析基準，agent 會對自己的 cwd（主樹、不知道哪個 revision）
解析，而輸出上看不出來——「讀錯樹」與「讀對樹、沒發現」不可區分。

這裡**實際執行**腳本：以 node 載入 `audit-workflow.js`，把 Workflow DSL 的
`agent` / `phase` / `log` / `parallel` / `pipeline` 換成記錄用的 stub，讀出每個
agent 真正收到的 prompt。只做字串比對的話，把那句話搬進註解也會照綠。

⚠️ 驗的是「prompt 裡有基準、引數不對就拒絕」，**不是**「agent 真的照它解析」——
後者是模型行為，這裡量不到。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / ".agents" / "skills" / "vibe-security-audit" / "audit-workflow.js"
_SKILL = _SCRIPT.parent / "SKILL.md"

_NODE = shutil.which("node")
# ⛔ Skipped only off CI. On GitHub Actions a missing node would turn every test
# below into a skip, and "nothing ran" would read as "nothing wrong" — the shape
# #1488 is about. The runner image ships node; if that ever changes this goes
# red (test_node_is_present_on_ci) instead of quietly measuring nothing.
_ON_CI = os.environ.get("GITHUB_ACTIONS") == "true"
needs_node = pytest.mark.skipif(_NODE is None and not _ON_CI, reason="node not on PATH")


def test_node_is_present_on_ci():
    if not _ON_CI:
        pytest.skip("only asserted on GitHub Actions")
    assert _NODE is not None, "node is not on PATH on the CI runner — the audit-workflow tests would measure nothing"

# The Workflow DSL runs the file as a script body: top-level `await`/`return`,
# globals `args`, `agent`, `phase`, `log`, `parallel`, `pipeline`. Reproduced
# here with an AsyncFunction; `export const meta` is the one module-only token.
_HARNESS = r"""
const fs = require('fs');
const [scriptPath, argsJson] = process.argv.slice(2);
const src = fs.readFileSync(scriptPath, 'utf8').replace(/^export const meta/m, 'const meta');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const calls = [];
const agent = async (prompt, opts) => {
  opts = opts || {};
  calls.push({ agentType: opts.agentType || null, label: opts.label || null, prompt });
  if (opts.agentType === 'vibe-sec-hunter') {
    return { attack_class: 'x', findings: [{ title: 't', severity: 'LOW', file: 'f',
      attacker: 'a', action: 'b', result: 'c', code_evidence: 'd' }] };
  }
  if (opts.agentType === 'vibe-sec-validator') {
    return { verdict: 'REJECTED', reasoning: 'r', domain_aware: true, misread_designed_behavior: false };
  }
  return {};
};
const parallel = (fns) => Promise.all(fns.map((f) => f()));
const pipeline = (items, a, b) => Promise.all(items.map(async (it) => b(await a(it), it)));
const body = new AsyncFunction('args', 'agent', 'phase', 'log', 'parallel', 'pipeline', src);
body(JSON.parse(argsJson), agent, () => {}, () => {}, parallel, pipeline).then(
  (result) => { process.stdout.write(JSON.stringify({ ok: true, result, calls })); },
  (err) => { process.stdout.write(JSON.stringify({ ok: false, error: String(err && err.message || err) })); },
);
"""

_WS = "/snap/sec-audit"
_TARGET = _WS + "/components/tenant-api"


def _run(args: dict, tmp_path: Path) -> dict:
    harness = tmp_path / "harness.cjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run([_NODE, str(harness), str(_SCRIPT), json.dumps(args)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@needs_node
def test_every_agent_prompt_carries_the_snapshot_root(tmp_path):
    out = _run({"target": _TARGET, "workspaceRoot": _WS}, tmp_path)
    assert out["ok"], out
    calls = out["calls"]
    roles = {c["agentType"] for c in calls}
    # Not vacuous: recon, every hunter, at least one validator and the synthesis ran.
    assert {"vibe-sec-recon", "vibe-sec-hunter", "vibe-sec-validator"} <= roles, roles
    path_agents = [c for c in calls if c["agentType"] in
                   ("vibe-sec-recon", "vibe-sec-hunter", "vibe-sec-validator")]
    for c in path_agents:
        assert f"is relative to {_WS}" in c["prompt"], (c["label"], c["prompt"][:400])
        assert "NEVER resolve a path against your current working directory" in c["prompt"]
    assert out["result"]["workspaceRoot"] == _WS


@needs_node
def test_the_cross_component_class_resolves_against_the_snapshot(tmp_path):
    """The injection class is the one whose files name components/... and scripts/...
    — the exact paths #1488 is about."""
    out = _run({"target": _TARGET, "workspaceRoot": _WS}, tmp_path)
    hunt = [c for c in out["calls"] if c["label"] == "hunt:injection"]
    assert len(hunt) == 1, [c["label"] for c in out["calls"]]
    prompt = hunt[0]["prompt"]
    assert "components/threshold-exporter/app/pkg/config/" in prompt
    assert f"explicit components/... or scripts/... path is relative to {_WS}" in prompt
    assert "unless an explicit components/... or scripts/... path is given" not in prompt, (
        "the old wording names explicit paths but gives them no base")


@needs_node
@pytest.mark.parametrize("args, needle", [
    ({"target": _TARGET}, "workspaceRoot is required"),
    ({"target": _TARGET, "workspaceRoot": "snap/sec-audit"}, "must be an ABSOLUTE path"),
    ({"target": _TARGET, "workspaceRoot": "/"}, "is a filesystem root"),
    ({"target": _TARGET, "workspaceRoot": "C:\\"}, "is a filesystem root"),
    ({"target": "/main/components/tenant-api", "workspaceRoot": _WS}, "is not under workspaceRoot"),
    # prefix, not a parent: /snap/sec-audit-old is not under /snap/sec-audit
    ({"target": _WS + "-old/components/tenant-api", "workspaceRoot": _WS}, "is not under workspaceRoot"),
], ids=["missing", "relative", "posix-root", "drive-root", "elsewhere", "sibling-prefix"])
def test_a_workspace_root_that_cannot_anchor_the_target_is_refused(tmp_path, args, needle):
    out = _run(args, tmp_path)
    assert not out["ok"], f"accepted {args}: agents would read an unanchored tree"
    assert needle in out["error"], out["error"]
    assert "calls" not in out


@needs_node
@pytest.mark.parametrize("ws, target", [
    (_WS + "/", _TARGET),
    ("C:\\snap\\sec-audit", "C:/snap/sec-audit/components/tenant-api"),
    (_WS, _WS),
], ids=["trailing-slash", "windows-separators", "target-is-root"])
def test_equivalent_spellings_of_the_same_tree_are_accepted(tmp_path, ws, target):
    out = _run({"target": target, "workspaceRoot": ws}, tmp_path)
    assert out["ok"], out


def test_the_skill_invocation_passes_workspace_root():
    """SKILL.md's Workflow example is what an operator copies; it must carry the
    argument the script now refuses to run without."""
    text = _SKILL.read_text(encoding="utf-8")
    example = text.split("Workflow({", 1)[1].split("})", 1)[0]
    assert "workspaceRoot:" in example, example
