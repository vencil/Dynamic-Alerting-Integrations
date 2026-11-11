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
from _lib_exitcodes import EXIT_CALLER_ERROR, CallerError, encode_json  # noqa: E402
from _lib_python import detect_cli_lang  # noqa: E402
from _lib_confd import warn_nested  # noqa: E402
import _lib_parse_cache  # noqa: E402  (#3206: da-guard's listing)
from _lib_io import exit_on_output_write_error, output_write, write_operator_output  # noqa: E402  (#1789, #3107)
from _lib_label import safe_label  # noqa: E402
from _lib_paths import classify_path  # noqa: E402  (#3276)
import _lib_input  # noqa: E402  (#3751)

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
    """Scan config directory, return sorted list of (filename, content, hash).

    ⛔ #3751: *config_dir* is the one `main` judged
    (`_lib_parse_cache.declared_trees`), read through the run's reader of
    it — never judged again here; a directory gone since fails closed
    (ParseTreeError)."""
    files = []
    config_path = Path(config_dir)
    # Flat by design (the snapshot keys files by name); `warn_nested` names
    # what that leaves out. #3206: which files there are is da-guard's
    # listing, the run's one index of the tree — a tree Go names paths on
    # ends the run there, every such path named, exit 2.
    reader = _lib_parse_cache.reader_for(config_path)
    listing = reader.listing()
    warn_nested(config_path, tool="config_history", listing=listing)
    for f in [config_path / e.path for e in listing.config_files() if e.depth == 0]:
        # #3751: a file gone since the listing is refused, named — never a
        # traceback (`_lib_input.read_bytes`, the one read of a selected file).
        content = _lib_input.read_bytes(f, flag="--config-dir", lang=_LANG).decode('utf-8')
        h = _sha256(content)
        files.append({
            'name': f.name,
            'hash': h,
            'content': content,
            'size': len(content),
        })
    return files


def _history_path(config_dir):
    """Where the history of *config_dir* is (``<parent>/.da-history``),
    created or not."""
    return Path(config_dir).parent / '.da-history'


def _history_dir(config_dir):
    """Get or create history directory — for a write (``snapshot``) only:
    a read never creates it (#3751).

    #1789: this tool has no output flag — every path it writes is DERIVED
    from ``--config-dir`` (``<parent>/.da-history``), so that is the flag the
    operator has to fix and the one the message names.
    """
    hdir = _history_path(config_dir)
    with output_write(hdir, flag="--config-dir", action="create directory"):
        hdir.mkdir(exist_ok=True)
    return hdir


def _load_history(config_dir):
    """Load existing history entries ([] when there is none). Creates
    nothing (#3751: a read-only subcommand made ``.da-history`` beside any
    path it was given)."""
    history_file = _history_path(config_dir) / 'history.json'
    if history_file.exists():
        # ⚠️ NOT GUARDED (#1789), on purpose: this is a READ, and the ticket's
        # axis is the WRITE direction — "the output path the operator named is
        # unusable ⇒ rc 2 naming the flag". An unreadable history.json is a
        # corrupt state directory, not a mistyped flag, and wrapping it would
        # print "cannot write …" for a read. It stays an uncaught traceback —
        # which since #3090 ends at rc=2 via _lib_exitcodes' crash hook
        # ("ERROR: unexpected internal error …"), no longer the interpreter's
        # rc=1 — and it MASKS the guarded write at `_save_history` below
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
        history_file.write_text(encode_json(history),
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

    # #3216: encoded before anything is written — a history.json holding a
    # value JSON cannot carry (NaN read back by json.loads) refuses the run
    # with no snap-N/ left behind and history.json as it was.
    encode_json([*history, entry])

    # Save snapshot content
    hdir = _history_dir(config_dir)
    snap_dir = hdir / f"snap-{entry['id']}"
    with output_write(snap_dir, flag="--config-dir", action="create directory"):
        snap_dir.mkdir(exist_ok=True)
    for f in files:
        fp = snap_dir / f['name']
        # A snapshot may hold sensitive config: 0o600, set by the one writer
        # of operator-named output (#3107) — the kernel opens it, and the mode
        # is applied to a regular file this euid owns, never through a symlink.
        with output_write(fp, flag="--config-dir", action="write"):
            write_operator_output(str(fp), f['content'], flag="--config-dir")

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

    hdir = _history_path(config_dir)
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


def _judge_history(config_dir):
    """The one judgement of a read-only subcommand (``log`` / ``show`` /
    ``diff``, #3751): they read the snapshot history beside --config-dir
    (``<parent>/.da-history``), never --config-dir itself — so a conf.d
    deleted since its last snapshot still shows its history (a recovery
    path). Raises CallerError for:

    * ``''`` (#3406: never the current directory) and a --config-dir that
      is there but no usable directory;
    * a history that is there but no usable directory (a dangling link
      included — never read as "no history");
    * neither there: both named — it printed "no snapshots yet", rc 0, and
      created ``.da-history`` beside any path it was given.

    ⚠️ The history belongs to the parent, not to the name: a --config-dir
    that is not there (a typo of a sibling) reads the history beside it.
    That is said on stderr, both named, never in silence."""
    conf = classify_path(config_dir, "dir", lang=_LANG, flag="--config-dir")
    # A dangling link is "missing" to stat, yet something is there: never read
    # as a conf.d deleted since its snapshot (#3751 review).
    if conf is not None and (config_dir == "" or not conf.missing
                             or os.path.lexists(config_dir)):
        raise CallerError(conf.message, conf.reason if not conf.missing
                          else "dir_not_accessible")
    hdir = _history_path(config_dir)
    shown_conf, shown_hdir = safe_label(config_dir), safe_label(str(hdir))
    hist = classify_path(str(hdir), "dir", lang=_LANG)
    if hist is not None and (not hist.missing or os.path.lexists(hdir)):
        raise CallerError(_t(f"--config-dir {shown_conf}: 快照歷史 {hist.message}",
                             f"--config-dir {shown_conf}: snapshot history {hist.message}"),
                          hist.reason if not hist.missing else "dir_not_accessible")
    if conf is None:
        return
    if hist is not None:
        raise CallerError(_t(f"{conf.message}，且 {shown_hdir} 沒有快照歷史",
                             f"{conf.message}, and no snapshot history at {shown_hdir}"),
                          conf.reason)
    print(_t(f"警告：{conf.message}；讀取它旁邊的快照歷史 {shown_hdir}",
             f"WARN: {conf.message}; reading the snapshot history beside it, {shown_hdir}"),
          file=sys.stderr)


@exit_on_output_write_error
@_lib_parse_cache.exit_on_parse_tree_error  # #3206: the listing is da-guard's — rc 2, named
@_lib_parse_cache.session()  # #3751: the reader `declared_trees` judged is the one read
def main():
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=_t('Config Snapshot & History — 配置快照與歷史追蹤',
                       'Config Snapshot & History — track configuration changes over time'))
    # #3751: read by `snapshot` alone (declared: judged there, once); the
    # read-only subcommands read the history beside it (`_judge_history`).
    _lib_input.add_input(parser, '--config-dir', select=_lib_input.DA_GUARD_LISTS,
                         required=True, help=_t('conf.d 目錄路徑', 'Path to conf.d directory'))
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

    try:
        if args.action == 'snapshot':
            [config_dir] = _lib_parse_cache.declared_trees(args, 'config_dir', lang=_LANG)
        else:
            config_dir = args.config_dir
            _judge_history(config_dir)
    except CallerError as e:
        print(_t(f"錯誤：目錄無法使用 {e}", f"Error: {e}"), file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if args.action == 'snapshot':
        cmd_snapshot(config_dir, args.message)
    elif args.action == 'log':
        cmd_log(config_dir, args.limit)
    elif args.action == 'show':
        cmd_show(config_dir, args.id)
    elif args.action == 'diff':
        cmd_diff(config_dir, args.id_a, args.id_b)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
