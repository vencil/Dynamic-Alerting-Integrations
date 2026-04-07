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

def _detect_lang():
    """Detect CLI language."""
    for var in ('DA_LANG', 'LC_ALL', 'LANG'):
        val = os.environ.get(var, '')
        if val.startswith('zh'):
            return 'zh'
    return 'en'


_LANG = _detect_lang()


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
        sys.exit(1)

    for f in sorted(config_path.glob("*.yaml")):
        if f.name.startswith('.'):
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
    """Get or create history directory."""
    hdir = Path(config_dir).parent / '.da-history'
    hdir.mkdir(exist_ok=True)
    return hdir


def _load_history(config_dir):
    """Load existing history entries."""
    hdir = _history_dir(config_dir)
    history_file = hdir / 'history.json'
    if history_file.exists():
        return json.loads(history_file.read_text(encoding='utf-8'))
    return []


def _save_history(config_dir, history):
    """Save history to disk."""
    hdir = _history_dir(config_dir)
    history_file = hdir / 'history.json'
    history_file.write_text(json.dumps(history, indent=2, ensure_ascii=False),
                            encoding='utf-8')


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
    snap_dir.mkdir(exist_ok=True)
    for f in files:
        fp = snap_dir / f['name']
        fp.write_text(f['content'], encoding='utf-8')
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
        sys.exit(1)

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
        sys.exit(1)

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


def main():
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
        sys.exit(1)

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
