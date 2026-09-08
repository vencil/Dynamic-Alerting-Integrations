#!/usr/bin/env python3
"""config_history.py — Config Snapshot & History tracker.

Records and displays the history of conf.d/ configuration changes.
Each snapshot captures the SHA-256 hash + diff of every tenant YAML file.

Usage:
    da-tools config-history --config-dir conf.d/ snapshot          # Take a snapshot
    da-tools config-history --config-dir conf.d/ log               # Show history
    da-tools config-history --config-dir conf.d/ log --limit 5     # Last 5 entries
    da-tools config-history --config-dir conf.d/ diff 2 3          # Diff between snapshots
    da-tools config-history --config-dir conf.d/ show 3            # Show snapshot details

Snapshots are stored in .da-history/ (gitignored by default).
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_python import detect_cli_lang, format_json_report  # noqa: E402
from _lib_confd import has_yaml_extension, is_hidden_name, warn_nested  # noqa: E402
from _lib_io import exit_on_output_write_error, output_write  # noqa: E402  (#1789)

# Canonical lang detection (da-tools ROI r3 W2 bug fix): the former local
# `_detect_lang` only checked the zh prefix per variable, so DA_LANG=en fell
# through to LC_ALL — an explicit DA_LANG=en LOST to LC_ALL=zh, violating the
# canonical contract ("DA_LANG=en wins over LC_ALL=zh"). `detect_cli_lang`
# early-returns on BOTH zh and en prefixes.
_LANG = detect_cli_lang()


def _t(zh, en):
    """Bilingual text helper."""
    return zh if _LANG == 'zh' else en


def _sha256(content):
    """Compute SHA-256 of string content."""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]


def _scan_config_dir(config_dir):
    """Scan config directory, return sorted list of (filename, content, hash)."""
    files = []
    config_path = Path(config_dir)
    if not config_path.is_dir():
        print(_t(f"錯誤：目錄不存在 {config_dir}", f"Error: directory not found {config_dir}"),
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # #1339: flat by design here — but a hierarchical conf.d must not
    # look like an empty one. Name the files this scan cannot see.
    warn_nested(config_path, tool="config_history")
    # #1603: `glob("*.yaml")` here meant a `db-b.yml` carrier the exporter is
    # serving never entered a snapshot — so it could be added, edited or
    # deleted between two `config-history` runs and the diff stayed empty.
    # `has_yaml_extension` (`CONFIG_SUFFIXES`, the exporter's own set) decides
    # the SPELLING and, being case-insensitive, the CASE. The listing is
    # `iterdir()` on purpose: a `glob("*.y*")` pre-filter is case-SENSITIVE
    # on POSIX, so `DB-C.YAML` would never reach the predicate — measured on
    # byte-identical bodies: `alpha.yaml` / `alpha.yml` / `Alpha.YAML` all
    # enter the snapshot, a tree with no tenant does not
    # (tests/ops/test_config_history.py). `iterdir` is in
    # `test_confd_enumeration_contract._FLAT_CALLS`, so this stays in the
    # gate's flat class and the `warn_nested` above stays a subject of its
    # assertions; `glob("*")` would NOT (the classifier reads the literal
    # pattern and only treats `*.y…` as flat). Hidden entries are skipped by
    # the shared `is_hidden_name`, the exporter's own rule.
    #
    # What was measured, and on what: the SELECTION EXPRESSION on its own
    # (`glob(...)` plus the predicate), old form vs new, on the dev container
    # (Linux / Python 3.13.13, the CI interpreter), over one directory holding
    # db-a.yaml, db-b.yml, DB-C.YAML, _defaults.yaml, .hidden.yaml, x.yang,
    # plain.y, notes.txt and a DIRECTORY named dirnamed.yaml. Delta: exactly
    # `+db-b.yml`, nothing removed.
    # ⛔ NOT this function end to end, and the distinction is load bearing:
    # `read_text` on `dirnamed.yaml` raises `IsADirectoryError` — before this
    # change AND after it — so on that fixture the function has no output to
    # diff. The `is_file` axis (#1607 / #1469) is untouched here in the sense
    # that both versions fail identically, not in the sense that both return.
    # See the disclosure below.
    # ⚠️ DISCLOSURE — this widening extends an EXISTING crash to a second
    # spelling. Measured, same probe on `5a03cb8f` and here:
    #
    #   entry in the conf.d          before        after
    #   `dirnamed.yaml/` (a dir)     RAISED        RAISED     <- unchanged
    #   `dirnamed.yml/`  (a dir)     ok, ignored   RAISED     <- reach grew
    #   `broken.yaml` (dead link)    RAISED        RAISED     <- unchanged
    #   `broken.yml`  (dead link)    ok, ignored   RAISED     <- reach grew
    #   `bad.yaml` (invalid UTF-8)   RAISED        RAISED     <- unchanged
    #   `bad.yml`  (invalid UTF-8)   ok, ignored   RAISED     <- reach grew
    #
    # The CLASS is pre-existing and filed (#1469 for the directory-shaped
    # entry, #1654 for the undecodable content); what grew is which spellings
    # reach it, and that follows from the entry being a real carrier now.
    # ⛔ Deliberately NOT "fixed" here by swallowing the error: #1469 says in
    # so many words that turning this into a quiet pass is the wrong repair —
    # the right one names the entry (`_lib_confd.unusable_config_paths`),
    # which is a separate change with its own blast radius.
    for f in sorted(config_path.iterdir()):
        if is_hidden_name(f.name) or not has_yaml_extension(f.name):
            continue
        content = f.read_text(encoding='utf-8')
        h = _sha256(content)
        files.append({
            'name': f.name,
            'hash': h,
            'content': content,
            'size': len(content),
        })
    return files


def _history_dir(config_dir):
    """Get or create history directory.

    #1789: this tool has no output flag — every path it writes is DERIVED
    from ``--config-dir`` (``<parent>/.da-history``), so that is the flag the
    operator has to fix and the one the message names.
    """
    hdir = Path(config_dir).parent / '.da-history'
    with output_write(hdir, flag="--config-dir", action="create directory"):
        hdir.mkdir(exist_ok=True)
    return hdir


def _load_history(config_dir):
    """Load existing history entries."""
    hdir = _history_dir(config_dir)
    history_file = hdir / 'history.json'
    if history_file.exists():
        # ⚠️ NOT GUARDED (#1789), on purpose: this is a READ, and the ticket's
        # axis is the WRITE direction — "the output path the operator named is
        # unusable ⇒ rc 2 naming the flag". An unreadable history.json is a
        # corrupt state directory, not a mistyped flag, and wrapping it would
        # print "cannot write …" for a read. It stays a traceback at rc=1,
        # unchanged, and it MASKS the guarded write at `_save_history` below
        # for any shape that makes history.json unreadable — which is why no
        # row drives that sink and only the static pin speaks for it.
        # ⛔ It is not in that pin's NOT_GUARDED list either: the list is
        # exit-locked against SINKS, and a read is not one, so an entry for
        # this line would be rejected as stale. This comment is the record.
        return json.loads(history_file.read_text(encoding='utf-8'))
    return []


def _save_history(config_dir, history):
    """Save history to disk."""
    hdir = _history_dir(config_dir)
    history_file = hdir / 'history.json'
    with output_write(history_file, flag="--config-dir", action="write"):
        history_file.write_text(format_json_report(history),
                                encoding='utf-8', newline='\n')


def cmd_snapshot(config_dir, message=None):
    """Take a configuration snapshot."""
    files = _scan_config_dir(config_dir)
    history = _load_history(config_dir)

    # Compute composite hash
    composite = _sha256('|'.join(f"{f['name']}:{f['hash']}" for f in files))

    # Detect changes from previous snapshot
    prev = history[-1] if history else None
    changes = []
    if prev:
        prev_files = {f['name']: f for f in prev['files']}
        curr_files = {f['name']: f for f in files}

        for name, curr in curr_files.items():
            if name not in prev_files:
                changes.append({'type': 'added', 'file': name})
            elif curr['hash'] != prev_files[name]['hash']:
                changes.append({'type': 'modified', 'file': name})
        for name in prev_files:
            if name not in curr_files:
                changes.append({'type': 'removed', 'file': name})

    # Skip if no changes
    if prev and prev['composite_hash'] == composite:
        print(_t('⊘ 配置未變更，跳過快照。', '⊘ No changes detected, snapshot skipped.'))
        return

    entry = {
        'id': len(history) + 1,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'composite_hash': composite,
        'message': message or '',
        'file_count': len(files),
        'files': [{'name': f['name'], 'hash': f['hash'], 'size': f['size']} for f in files],
        'changes': changes,
    }

    # Save snapshot content
    hdir = _history_dir(config_dir)
    snap_dir = hdir / f"snap-{entry['id']}"
    with output_write(snap_dir, flag="--config-dir", action="create directory"):
        snap_dir.mkdir(exist_ok=True)
    for f in files:
        fp = snap_dir / f['name']
        # The 0o600 is inside the block with the write: a snapshot may hold
        # sensitive config, so a chmod that fails is not a success (#1789).
        with output_write(fp, flag="--config-dir", action="write"):
            fp.write_text(f['content'], encoding='utf-8', newline='\n')
            os.chmod(fp, 0o600)  # Restrict snapshot files (may contain sensitive config)

    history.append(entry)
    _save_history(config_dir, history)

    print(_t(f"✓ 快照 #{entry['id']} 已建立", f"✓ Snapshot #{entry['id']} created"))
    print(f"  {_t('時間', 'Time')}: {entry['timestamp']}")
    print(f"  {_t('檔案數', 'Files')}: {entry['file_count']}")
    print(f"  Hash: {composite}")
    if changes:
        print(f"  {_t('變更', 'Changes')}:")
        for c in changes:
            icon = {'added': '+', 'modified': '~', 'removed': '-'}.get(c['type'], '?')
            print(f"    [{icon}] {c['file']}")
    else:
        print(f"  {_t('初始快照', 'Initial snapshot')}")


def cmd_log(config_dir, limit=None):
    """Display snapshot history."""
    history = _load_history(config_dir)
    if not history:
        print(_t('尚無快照記錄。使用 snapshot 命令建立。',
                  'No snapshots yet. Use the snapshot command to create one.'))
        return

    entries = history[-limit:] if limit else history
    print(_t(f"配置歷史（共 {len(history)} 筆，顯示 {len(entries)} 筆）",
             f"Config history ({len(history)} total, showing {len(entries)})"))
    print()

    for entry in reversed(entries):
        ts = entry['timestamp'][:19].replace('T', ' ')
        changes_str = ''
        if entry.get('changes'):
            parts = []
            added = sum(1 for c in entry['changes'] if c['type'] == 'added')
            modified = sum(1 for c in entry['changes'] if c['type'] == 'modified')
            removed = sum(1 for c in entry['changes'] if c['type'] == 'removed')
            if added:
                parts.append(f"+{added}")
            if modified:
                parts.append(f"~{modified}")
            if removed:
                parts.append(f"-{removed}")
            changes_str = f" [{', '.join(parts)}]"
        else:
            changes_str = _t(' [初始]', ' [initial]')

        msg = f" — {entry['message']}" if entry.get('message') else ''
        print(f"  #{entry['id']:3d}  {ts}  {entry['composite_hash']}{changes_str}{msg}")
        print(f"        {entry['file_count']} {_t('個檔案', 'files')}")


def cmd_show(config_dir, snapshot_id):
    """Show details of a specific snapshot."""
    history = _load_history(config_dir)
    entry = next((e for e in history if e['id'] == snapshot_id), None)
    if not entry:
        print(_t(f"錯誤：快照 #{snapshot_id} 不存在", f"Error: snapshot #{snapshot_id} not found"),
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    ts = entry['timestamp'][:19].replace('T', ' ')
    print(f"{_t('快照', 'Snapshot')} #{entry['id']}")
    print(f"  {_t('時間', 'Time')}:        {ts}")
    print(f"  Hash:        {entry['composite_hash']}")
    if entry.get('message'):
        print(f"  {_t('訊息', 'Message')}:     {entry['message']}")
    print(f"  {_t('檔案數', 'Files')}:      {entry['file_count']}")
    print()

    print(f"  {_t('檔案清單', 'File list')}:")
    for f in entry['files']:
        print(f"    {f['name']:30s}  {f['hash']}  ({f['size']} bytes)")

    if entry.get('changes'):
        print()
        print(f"  {_t('變更', 'Changes')}:")
        for c in entry['changes']:
            icon = {'added': '+', 'modified': '~', 'removed': '-'}.get(c['type'], '?')
            print(f"    [{icon}] {c['file']}")


def cmd_diff(config_dir, id_a, id_b):
    """Show diff between two snapshots."""
    history = _load_history(config_dir)
    entry_a = next((e for e in history if e['id'] == id_a), None)
    entry_b = next((e for e in history if e['id'] == id_b), None)

    if not entry_a or not entry_b:
        missing = id_a if not entry_a else id_b
        print(_t(f"錯誤：快照 #{missing} 不存在", f"Error: snapshot #{missing} not found"),
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    hdir = _history_dir(config_dir)
    snap_a = hdir / f"snap-{id_a}"
    snap_b = hdir / f"snap-{id_b}"

    files_a = {f['name']: f for f in entry_a['files']}
    files_b = {f['name']: f for f in entry_b['files']}

    all_names = sorted(set(list(files_a.keys()) + list(files_b.keys())))

    print(_t(f"快照 #{id_a} vs #{id_b} 差異", f"Snapshot #{id_a} vs #{id_b} diff"))
    print()

    has_diff = False
    for name in all_names:
        in_a = name in files_a
        in_b = name in files_b

        if in_a and not in_b:
            print(f"  [-] {name} ({_t('已移除', 'removed')})")
            has_diff = True
        elif not in_a and in_b:
            print(f"  [+] {name} ({_t('新增', 'added')})")
            has_diff = True
        elif files_a[name]['hash'] != files_b[name]['hash']:
            print(f"  [~] {name} ({_t('已修改', 'modified')})")
            has_diff = True
            # Show content diff if snapshot files exist
            file_a = snap_a / name
            file_b = snap_b / name
            if file_a.exists() and file_b.exists():
                lines_a = file_a.read_text(encoding='utf-8').splitlines()
                lines_b = file_b.read_text(encoding='utf-8').splitlines()
                # Simple line-by-line diff
                for i, (la, lb) in enumerate(zip(lines_a, lines_b)):
                    if la != lb:
                        print(f"      L{i+1}: - {la}")
                        print(f"      L{i+1}: + {lb}")
                # Extra lines
                if len(lines_b) > len(lines_a):
                    for i in range(len(lines_a), len(lines_b)):
                        print(f"      L{i+1}: + {lines_b[i]}")
                elif len(lines_a) > len(lines_b):
                    for i in range(len(lines_b), len(lines_a)):
                        print(f"      L{i+1}: - {lines_a[i]}")

    if not has_diff:
        print(_t('  ⊘ 無差異', '  ⊘ No differences'))


@exit_on_output_write_error
def main():
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=_t('Config Snapshot & History — 配置快照與歷史追蹤',
                       'Config Snapshot & History — track configuration changes over time'))
    parser.add_argument('--config-dir', required=True,
                        help=_t('conf.d 目錄路徑', 'Path to conf.d directory'))
    sub = parser.add_subparsers(dest='action')

    # snapshot
    snap_parser = sub.add_parser('snapshot',
                                 help=_t('建立配置快照', 'Take a config snapshot'))
    snap_parser.add_argument('-m', '--message', default='',
                             help=_t('快照訊息', 'Snapshot message'))

    # log
    log_parser = sub.add_parser('log',
                                help=_t('顯示快照歷史', 'Show snapshot history'))
    log_parser.add_argument('--limit', type=int, default=None,
                            help=_t('顯示最近 N 筆', 'Show last N entries'))

    # show
    show_parser = sub.add_parser('show',
                                 help=_t('顯示快照詳情', 'Show snapshot details'))
    show_parser.add_argument('id', type=int,
                             help=_t('快照 ID', 'Snapshot ID'))

    # diff
    diff_parser = sub.add_parser('diff',
                                 help=_t('比較兩個快照', 'Diff between two snapshots'))
    diff_parser.add_argument('id_a', type=int, help=_t('快照 A ID', 'Snapshot A ID'))
    diff_parser.add_argument('id_b', type=int, help=_t('快照 B ID', 'Snapshot B ID'))

    args = parser.parse_args()

    if not args.action:
        parser.print_help()
        sys.exit(EXIT_CALLER_ERROR)

    if args.action == 'snapshot':
        cmd_snapshot(args.config_dir, args.message)
    elif args.action == 'log':
        cmd_log(args.config_dir, args.limit)
    elif args.action == 'show':
        cmd_show(args.config_dir, args.id)
    elif args.action == 'diff':
        cmd_diff(args.config_dir, args.id_a, args.id_b)


if __name__ == '__main__':
    main()
