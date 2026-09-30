"""The three sticky-comment report workflows: one run per PR, stamped comments.

#1518. `blast-radius.yml`, `config-diff.yaml` and `guard-defaults-impact.yml`
each edit ONE sticky PR comment in place. Two facts make that dangerous:

  1. Without a concurrency group, two pushes in quick succession race, and the
     comment ends up describing whichever run finished LAST — possibly the
     older commit.
  2. A run that never reaches its posting step (step skipped, or the job red
     earlier) leaves the previous comment standing, looking current.

What this module holds, and how:

  * Each workflow declares a TOP-LEVEL `concurrency`. Its `group` and
    `cancel-in-progress` are EVALUATED — not string-matched — against synthetic
    `github` contexts, one per event: two pushes to one PR share a group and
    cancel; two PRs do not share; the three workflows never share (two of them
    fire on the same paths, and a shared group would let one cancel the other);
    and every non-`pull_request` event neither cancels nor shares a group with
    anything, including another run of the same event on the same ref (a group
    with cancel-in-progress false still replaces its PENDING run).
  * The posting step's github-script is EXECUTED under node against a mocked
    `github` / `context`, on both the create and the update path, and the body
    it sends must carry the PR's HEAD sha (not the merge sha `context.sha`
    holds on a pull_request) and the run link.

⛔ Fail-closed boundaries, stated once:
  * The expression evaluator below reads only `==`, `!=`, `&&`, `||`, `!`,
    parentheses, string/number/bool/null literals, `format()`, and `github.*`
    paths it models. Anything else RAISES; it never guesses a value.
  * A trigger outside `_MODELLED_EVENTS` raises: `pull_request_target`, for
    one, also carries `github.event.pull_request` and would need its own
    context before anything here could vouch for it.
  * A github-script body containing an Actions `${{ }}` interpolation raises —
    the harness would be executing text the runner never sees.

What this does NOT show: that the posting step is REACHED on a given run (its
`if:`), or that GitHub honours the group as modelled here. The evaluator
mirrors the documented expression semantics; it is not the runner.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = (
    ".github/workflows/blast-radius.yml",
    ".github/workflows/config-diff.yaml",
    ".github/workflows/guard-defaults-impact.yml",
)

_MODELLED_EVENTS = {"pull_request", "workflow_dispatch", "push", "schedule"}
# Evaluated for EVERY workflow, declared or not: a push/schedule trigger added
# later must already be safe, not become safe after someone remembers here.
_NON_PR_EVENTS = ("workflow_dispatch", "push", "schedule")

_NODE = shutil.which("node")


def _load(rel: str) -> dict:
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


def _triggers(wf: dict) -> set[str]:
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    on = wf.get("on", wf.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return set(on)
    if isinstance(on, dict):
        return set(on)
    raise AssertionError(f"unreadable `on:` shape: {on!r}")


# ============================================================
# ── A small, fail-closed GitHub Actions expression evaluator ──
# ============================================================


class UnsupportedExpression(Exception):
    """The expression uses a shape this evaluator does not model."""


_TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<str>'(?:[^']|'')*')
    | (?P<num>-?\d+(?:\.\d+)?)
    | (?P<op>==|!=|&&|\|\||!|\(|\)|,)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)
    )""", re.X)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    toks, pos = [], 0
    expr = expr.rstrip()
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m or m.end() == pos:
            raise UnsupportedExpression(f"cannot tokenise {expr[pos:]!r}")
        kind = m.lastgroup
        toks.append((kind, m.group(kind)))
        pos = m.end()
    return toks


def _truthy(v) -> bool:
    return v not in (None, False, 0, "")


def _to_str(v) -> str:
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


class _Parser:
    def __init__(self, toks, ctx):
        self.toks, self.i, self.ctx = toks, 0, ctx

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def _take(self, value=None):
        tok = self._peek()
        if tok[0] is None or (value is not None and tok[1] != value):
            raise UnsupportedExpression(f"expected {value!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def parse(self):
        v = self._or()
        if self.i != len(self.toks):
            raise UnsupportedExpression(f"trailing tokens: {self.toks[self.i:]}")
        return v

    def _or(self):
        v = self._and()
        while self._peek()[1] == "||":
            self._take()
            rhs = self._and()
            v = v if _truthy(v) else rhs
        return v

    def _and(self):
        v = self._eq()
        while self._peek()[1] == "&&":
            self._take()
            rhs = self._eq()
            v = rhs if _truthy(v) else v
        return v

    def _eq(self):
        v = self._unary()
        if self._peek()[1] in ("==", "!="):
            op = self._take()[1]
            rhs = self._unary()
            if isinstance(v, str) and isinstance(rhs, str):
                same = v.casefold() == rhs.casefold()
            elif type(v) is type(rhs):
                same = v == rhs
            else:
                raise UnsupportedExpression(
                    f"mixed-type comparison {v!r} {op} {rhs!r}: GitHub coerces "
                    f"these and this evaluator does not model the coercion")
            v = same if op == "==" else not same
        return v

    def _unary(self):
        if self._peek()[1] == "!":
            self._take()
            return not _truthy(self._unary())
        return self._primary()

    def _primary(self):
        kind, text = self._peek()
        if kind == "str":
            self._take()
            return text[1:-1].replace("''", "'")
        if kind == "num":
            self._take()
            return float(text) if "." in text else int(text)
        if text == "(":
            self._take()
            v = self._or()
            self._take(")")
            return v
        if kind == "ident":
            self._take()
            if self._peek()[1] == "(":
                return self._call(text)
            if text in ("true", "false"):
                return text == "true"
            if text == "null":
                return None
            return self._lookup(text)
        raise UnsupportedExpression(f"unexpected token {text!r}")

    def _call(self, name):
        if name != "format":
            raise UnsupportedExpression(f"function {name}() is not modelled")
        self._take("(")
        args = [self._or()]
        while self._peek()[1] == ",":
            self._take()
            args.append(self._or())
        self._take(")")
        fmt, rest = _to_str(args[0]), args[1:]

        def sub(m):
            if m.group(0) == "{{":
                return "{"
            if m.group(0) == "}}":
                return "}"
            idx = int(m.group(1))
            if idx >= len(rest):
                raise UnsupportedExpression(f"format index {idx} out of range")
            return _to_str(rest[idx])

        return re.sub(r"\{\{|\}\}|\{(\d+)\}", sub, fmt)

    def _lookup(self, path):
        parts = path.split(".")
        if parts[0] != "github":
            raise UnsupportedExpression(f"context `{parts[0]}` is not modelled")
        node = self.ctx
        for depth, part in enumerate(parts[1:], start=1):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif depth >= 2 and parts[1] == "event":
                # A missing property under the event payload is null in
                # Actions — that is how `pull_request.number` reads on a
                # dispatch. Outside `github.event` a miss means this model is
                # incomplete, and guessing null would be absorbing it.
                return None
            else:
                raise UnsupportedExpression(f"`{path}` is not modelled")
        return node


def evaluate(expr: str, ctx: dict):
    return _Parser(_tokenize(expr), ctx).parse()


def render(value, ctx: dict):
    """A workflow value: a literal, one whole `${{ }}`, or text with several."""
    if not isinstance(value, str):
        return value
    whole = re.fullmatch(r"\s*\$\{\{(.*?)\}\}\s*", value, re.S)
    if whole and "${{" not in whole.group(1):
        return evaluate(whole.group(1), ctx)
    return re.sub(r"\$\{\{(.*?)\}\}",
                  lambda m: _to_str(evaluate(m.group(1), ctx)), value)


def _ctx(workflow_name: str, event: str, *, run_id: int, pr: int | None = None,
         ref: str = "refs/heads/main") -> dict:
    payload: dict = {}
    if pr is not None:
        payload["pull_request"] = {"number": pr,
                                   "head": {"sha": f"{run_id:040x}"}}
        ref = f"refs/pull/{pr}/merge"
    return {"workflow": workflow_name, "event_name": event, "run_id": run_id,
            "ref": ref, "event": payload}


# ============================================================
# ── Concurrency ──
# ============================================================


def _concurrency(rel: str) -> tuple[dict, str]:
    wf = _load(rel)
    conc = wf.get("concurrency")
    assert isinstance(conc, dict) and conc.get("group"), (
        f"{rel}: no top-level `concurrency` with a `group`. Two pushes to one "
        f"PR then race to edit the same sticky comment, and the one that "
        f"finishes LAST wins — which can be the older commit (#1518)."
    )
    triggers = _triggers(wf)
    unmodelled = triggers - _MODELLED_EVENTS
    if unmodelled:
        raise UnsupportedExpression(
            f"{rel}: triggers {sorted(unmodelled)} have no synthetic context "
            f"here. Model them before trusting this test for this file.")
    assert "pull_request" in triggers, f"{rel}: not a pull_request workflow"
    return conc, wf["name"]


def _group_and_cancel(conc: dict, ctx: dict) -> tuple[str, bool]:
    group = _to_str(render(conc["group"], ctx))
    cancel = render(conc.get("cancel-in-progress", False), ctx)
    assert isinstance(cancel, bool), f"cancel-in-progress rendered {cancel!r}"
    return group, cancel


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_pushes_to_one_pr_share_a_group_and_cancel(rel) -> None:
    conc, name = _concurrency(rel)
    g1, c1 = _group_and_cancel(conc, _ctx(name, "pull_request", run_id=1001, pr=7))
    g2, c2 = _group_and_cancel(conc, _ctx(name, "pull_request", run_id=1002, pr=7))
    other, _ = _group_and_cancel(conc, _ctx(name, "pull_request", run_id=1003, pr=8))
    assert g1 == g2, (
        f"{rel}: two pushes to PR #7 land in groups {g1!r} and {g2!r}, so the "
        f"newer run cannot supersede the older one.")
    assert c1 is True and c2 is True, (
        f"{rel}: cancel-in-progress is {c1!r} on a pull_request. The group then "
        f"only queues the superseded run instead of cancelling it (#1518).")
    assert other != g1, (
        f"{rel}: PR #7 and PR #8 share the group {g1!r}, so a push to one PR "
        f"cancels the other PR's report.")


@pytest.mark.parametrize("rel", WORKFLOWS)
@pytest.mark.parametrize("event", _NON_PR_EVENTS)
def test_non_pr_events_never_cancel_or_share(rel, event) -> None:
    conc, name = _concurrency(rel)
    ga, ca = _group_and_cancel(conc, _ctx(name, event, run_id=2001))
    gb, cb = _group_and_cancel(conc, _ctx(name, event, run_id=2002))
    assert ca is False and cb is False, (
        f"{rel}: `{event}` renders cancel-in-progress {ca!r}. Only a "
        f"pull_request run may be superseded; a {event} run must finish.")
    assert ga != gb, (
        f"{rel}: two `{event}` runs on the same ref share the group {ga!r}. "
        f"Even with cancel-in-progress false, a group keeps ONE pending run "
        f"and cancels the older pending one when a newer arrives.")
    pr_groups = {_group_and_cancel(conc, _ctx(name, "pull_request",
                                              run_id=r, pr=p))[0]
                 for r, p in ((3001, 7), (2001, 2001), (2002, 2002))}
    assert ga not in pr_groups and gb not in pr_groups, (
        f"{rel}: a `{event}` run's group {ga!r} collides with a pull_request "
        f"group, so a PR push would cancel it (or it would cancel the PR's).")


def test_the_three_workflows_never_share_a_group() -> None:
    groups = {}
    for rel in WORKFLOWS:
        conc, name = _concurrency(rel)
        groups[rel] = _group_and_cancel(
            conc, _ctx(name, "pull_request", run_id=1001, pr=7))[0]
    assert len(set(groups.values())) == len(groups), (
        f"the same PR maps to a shared group across workflows: {groups}. "
        f"blast-radius.yml and config-diff.yaml fire on the same paths, so "
        f"a shared group lets one workflow cancel the other's report.")


@pytest.mark.parametrize("expr,want", [
    ("github.event_name == 'pull_request'", True),
    ("github.event_name == 'PULL_REQUEST'", True),
    ("github.event_name != 'pull_request'", False),
    ("github.event.pull_request.number || 'x'", 7),
    ("github.event.nope.deeper || 'x'", "x"),
    ("'' && 'b'", ""),
    ("'a' && 'b'", "b"),
    ("!(null)", True),
    ("format('pr-{0}-{1}{{', 7, null)", "pr-7-{"),
])
def test_evaluator_reads_the_modelled_shapes(expr, want) -> None:
    ctx = _ctx("W", "pull_request", run_id=1, pr=7)
    assert evaluate(expr, ctx) == want


@pytest.mark.parametrize("expr", [
    "contains(github.event_name, 'pull')",
    "inputs.config_dir",
    "github.head_ref",
    "github.event_name == 1",
    "github.event_name > 'a'",
    "format('{1}', 'a')",
])
def test_evaluator_refuses_what_it_does_not_model(expr) -> None:
    with pytest.raises(UnsupportedExpression):
        evaluate(expr, _ctx("W", "pull_request", run_id=1, pr=7))


# ============================================================
# ── The sticky comment carries the head sha and the run link ──
# ============================================================

_HEAD_SHA = "feedc0de" + "1" * 32
_MERGE_SHA = "abad1dea" + "2" * 32
_RUN_ID = 424242

_HARNESS = r"""
const spec = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const writes = [];
const fakeFs = {
  readFileSync: () => 'REPORT-BODY',
  existsSync: () => true,
};
const fakeRequire = (m) => {
  if (m === 'fs') return fakeFs;
  throw new Error('harness does not model require(' + JSON.stringify(m) + ')');
};
const issues = {
  listComments: async () => ({ data: spec.existing }),
  createComment: async (a) => { writes.push({ op: 'create', ...a }); return { data: {} }; },
  updateComment: async (a) => { writes.push({ op: 'update', ...a }); return { data: {} }; },
};
const github = { rest: { issues } };
const context = {
  payload: { pull_request: { number: 7, head: { sha: spec.head } } },
  sha: spec.merge, runId: spec.runId, serverUrl: 'https://github.com',
  repo: { owner: 'o', repo: 'r' }, issue: { number: 7 },
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('require', 'github', 'context', 'core', spec.script)(
  fakeRequire, github, context, {})
  .then(() => process.stdout.write(JSON.stringify(writes)))
  .catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(3); });
"""


def _require_node() -> str:
    if _NODE is None:
        if os.environ.get("VIBE_REQUIRE_NODE") == "1":
            pytest.fail("VIBE_REQUIRE_NODE=1 but no `node` on PATH — the "
                        "sticky-comment stamp check would silently skip.")
        pytest.skip("node not on PATH")
    return _NODE


def _script_steps(rel: str) -> list[dict]:
    steps = [s for job in _load(rel)["jobs"].values()
             for s in job.get("steps") or []
             if str(s.get("uses", "")).startswith("actions/github-script@")]
    assert steps, f"{rel}: no github-script step — how is the report posted now?"
    return steps


def _run(script: str, env: dict, existing: list) -> list[dict]:
    spec = {"script": script, "existing": existing, "head": _HEAD_SHA,
            "merge": _MERGE_SHA, "runId": _RUN_ID}
    proc = subprocess.run(
        [_require_node(), "-e", _HARNESS], input=json.dumps(spec),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, **env}, check=False)
    assert proc.returncode == 0, f"script raised under the harness:\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_sticky_comment_is_stamped_with_head_sha_and_run(rel) -> None:
    posted = 0
    for step in _script_steps(rel):
        script = step["with"]["script"]
        if "${{" in script:
            raise UnsupportedExpression(
                f"{rel}: step {step.get('name')!r} interpolates `${{{{ }}}}` "
                f"into its script; the harness would run text the runner never "
                f"sees. Route the value through `env:` instead.")
        env = {k: f"ENV-{k}" for k in (step.get("env") or {})}
        created = _run(script, env, existing=[])
        if not created:
            continue
        posted += 1
        assert [w["op"] for w in created] == ["create"], created
        body = created[0]["body"]
        updated = _run(script, env, existing=[{"id": 99, "body": body}])
        assert [(w["op"], w.get("comment_id")) for w in updated] == [("update", 99)], (
            f"{rel}: the second run did not edit the first run's comment in "
            f"place: {updated}")
        for op, text in (("create", body), ("update", updated[0]["body"])):
            assert _HEAD_SHA[:7] in text, (
                f"{rel}: the {op} body carries no head sha. A stale sticky "
                f"comment is then indistinguishable from the current one "
                f"(#1518).\n{text}")
            assert _MERGE_SHA[:7] not in text, (
                f"{rel}: the {op} body carries the MERGE sha (`context.sha` on "
                f"a pull_request), which no reader can match to a push.")
            assert f"https://github.com/o/r/actions/runs/{_RUN_ID}" in text, (
                f"{rel}: the {op} body carries no link to the run that "
                f"produced it.")
    assert posted >= 1, (
        f"{rel}: no github-script step posted a comment under the harness, so "
        f"every assertion above was skipped.")
