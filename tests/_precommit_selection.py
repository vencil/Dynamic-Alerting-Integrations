"""Config keys that take a pre-commit hook off its files without touching `files:`.

Shared by every test that pins "this hook really runs": a test that reads only
the hook's `entry` / `files:` is satisfied by a hook pre-commit then skips.
Moved here from `tests/lint/test_e2e_spec_lint.py` (#2539) so the
`open-encoding-audit` pins in `tests/dx/test_line_ending_policy.py` read the
same list instead of a second one.
"""
from __future__ import annotations


def silencing_keys(hook: dict, config: dict) -> list[str]:
    """Every reason pre-commit would NOT run this hook on its files.

    ⛔ Returns a list rather than a bool so the failure names the key. The
    mechanisms below were measured against real pre-commit in a throwaway git
    repo (4.5.1 for the first four), all of which leave `files:` reading
    perfectly:

    * `stages:` on the hook without `pre-commit` in it;
    * a top-level `default_stages:` without `pre-commit`, when the hook does
      not override it — THIS REPO ALREADY SETS THAT KEY, so the interaction is
      live, not hypothetical;
    * `types:` narrowed off the default `[file]`, which ANDs with `files:`;
    * any `exclude_types:`, which subtracts from it;
    * `types_or:` (see the comment below);
    * `exclude:` on the hook, or a top-level `exclude:` / `files:` — they
      filter every hook's file set. Measured on `open-encoding-audit` (#2539):
      `exclude: .*` makes `pre-commit run <id> --all-files` print
      "(no files to check) Skipped" and exit 0. Any `exclude:` is reported,
      not only a total one: whether a pattern empties the set is a question
      about the tree, and a path that must be exempt is readable in `files:`.

    The first version asserted `"stages" not in hook`. That is a key-existence
    check, and it was wrong in both directions at once: `stages: [pre-commit,
    pre-push]` is a legitimate widening that it reds, while the top-level
    default it never reads silences the hook just as completely.
    """
    reasons: list[str] = []
    stages = hook.get("stages")
    if stages is None:
        stages = config.get("default_stages")
        where = "the top-level `default_stages:`"
    else:
        where = "the hook's `stages:`"
    if stages is not None and "pre-commit" not in stages:
        reasons.append(f"{where} is {stages!r}, which omits `pre-commit`")

    types = hook.get("types")
    if types is not None and list(types) != ["file"]:
        reasons.append(
            f"`types: {types!r}` narrows the file set beyond `files:` "
            "(pre-commit ANDs them; the default is `[file]`)")
    if hook.get("exclude_types"):
        reasons.append(f"`exclude_types: {hook['exclude_types']!r}` subtracts "
                       "from the file set without touching `files:`")
    # ⛔ `types_or:` is the fifth member of the same closed set, one line away
    # from `types:` in pre-commit's own schema — `run.py` ANDs it in as
    # `(not types_or or tags & types_or)`. A blind review set it to `[python]`
    # against the real hook and got "(no files to check) Skipped" with this
    # file green. Enumerating here is legitimate BECAUSE the set is closed and
    # published; the failure was reading four of five, not the approach.
    if hook.get("types_or"):
        reasons.append(f"`types_or: {hook['types_or']!r}` narrows the file set "
                       "the same way `types:` does (pre-commit ANDs it in)")
    if hook.get("exclude"):
        reasons.append(f"the hook's `exclude: {hook['exclude']!r}` can empty "
                       "its file set without touching `files:`")
    for key in ("exclude", "files"):
        if config.get(key):
            reasons.append(f"the top-level `{key}: {config[key]!r}` filters "
                           "every hook's file set before the hook's own `files:`")
    return reasons
