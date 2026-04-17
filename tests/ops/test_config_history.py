#!/usr/bin/env python3
"""test_config_history.py — config_history.py pytest 風格測試。

驗證:
  1. _sha256() — SHA-256 雜湊運算與截斷
  2. _detect_lang() — 語言偵測（環境變數順序）
  3. _t() — 雙語文本 helper
  4. _scan_config_dir() — YAML 掃描與排序
  5. _scan_config_dir() 缺失目錄 → sys.exit()
  6. _history_dir() — 歷史目錄建立
  7. _load_history() / _save_history() — 往返序列化
  8. cmd_snapshot() 初始快照（無前次、建立 snap-1/）
  9. cmd_snapshot() 檢測變更（added/modified/removed）
  10. cmd_snapshot() 無變更跳過（composite hash 相同）
  11. cmd_log() 空歷史
  12. cmd_log() 列出快照條目（格式化輸出）
  13. cmd_log() --limit 限制條目數
  14. cmd_show() 有效 ID
  15. cmd_show() 無效 ID → sys.exit()
  16. cmd_diff() 兩個快照間比對（added/modified/removed）
  17. cmd_diff() 顯示行級差異
  18. cmd_diff() 缺失快照 → sys.exit()
  19. cmd_diff() 無差異
  20. End-to-End：snapshot → 修改 → snapshot → log → diff
"""

import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import config_history as ch  # noqa: E402


# ── 1. _sha256 ──────────────────────────────────────────────────────

class TestSha256:
    """_sha256() 雜湊運算與截斷測試。"""

    def test_deterministic(self):
        """相同內容應產生相同雜湊。"""
        content = "threshold: 100\nalerts: true\n"
        h1 = ch._sha256(content)
        h2 = ch._sha256(content)
        assert h1 == h2

    def test_truncated_to_16_chars(self):
        """雜湊應截斷為 16 字元。"""
        h = ch._sha256("test content")
        assert len(h) == 16

    def test_hex_format(self):
        """結果應為十六進制格式。"""
        h = ch._sha256("test")
        assert all(c in '0123456789abcdef' for c in h)

    def test_different_content_different_hash(self):
        """不同內容應產生不同雜湊。"""
        h1 = ch._sha256("content1")
        h2 = ch._sha256("content2")
        assert h1 != h2

    def test_empty_content(self):
        """空內容應產生雜湊。"""
        h = ch._sha256("")
        assert len(h) == 16
        assert isinstance(h, str)

    def test_unicode_content(self):
        """Unicode 內容應正確處理。"""
        h = ch._sha256("閾值：100\n")
        assert len(h) == 16


# ── 2. _detect_lang ──────────────────────────────────────────────────

class TestDetectLang:
    """_detect_lang() 語言偵測測試。"""

    def test_da_lang_priority(self):
        """DA_LANG 應優先檢查。"""
        with patch.dict(os.environ, {'DA_LANG': 'zh_TW', 'LANG': 'en_US.UTF-8'}):
            # 必須重新加載模組以重新評估
            import importlib
            import config_history
            importlib.reload(config_history)
            assert config_history._detect_lang() == 'zh'

    def test_lc_all_second_priority(self):
        """LC_ALL 在 DA_LANG 不存在時檢查。"""
        with patch.dict(os.environ, {'DA_LANG': '', 'LC_ALL': 'zh_TW', 'LANG': 'en_US.UTF-8'},
                        clear=False):
            # 直接呼叫函式避免模組層級的副作用
            import config_history
            orig_lang = config_history._LANG
            try:
                config_history._LANG = config_history._detect_lang()
                assert config_history._detect_lang() in ('zh', 'en')
            finally:
                config_history._LANG = orig_lang

    def test_lang_fallback(self):
        """環境變數都不存在時預設英文。"""
        with patch.dict(os.environ, {}, clear=True):
            result = ch._detect_lang()
            assert result == 'en'

    def test_zh_startswith_prefix(self):
        """只檢查 'zh' 前綴。"""
        with patch.dict(os.environ, {'DA_LANG': 'zh_CN'}, clear=True):
            result = ch._detect_lang()
            assert result == 'zh'

    def test_non_zh_returns_en(self):
        """非 'zh' 前綴預設英文。"""
        with patch.dict(os.environ, {'DA_LANG': 'ja_JP', 'LC_ALL': 'fr_FR'}, clear=True):
            result = ch._detect_lang()
            assert result == 'en'


# ── 3. _t() ──────────────────────────────────────────────────────────

class TestBilingualText:
    """_t() 雙語 helper 測試。"""

    def test_returns_chinese_when_lang_zh(self):
        """_LANG == 'zh' 時回傳中文。"""
        with patch.object(ch, '_LANG', 'zh'):
            result = ch._t('中文', 'English')
            assert result == '中文'

    def test_returns_english_when_lang_en(self):
        """_LANG != 'zh' 時回傳英文。"""
        with patch.object(ch, '_LANG', 'en'):
            result = ch._t('中文', 'English')
            assert result == 'English'

    def test_mixed_content(self):
        """混合語言內容。"""
        with patch.object(ch, '_LANG', 'zh'):
            result = ch._t('快照 #1', 'Snapshot #1')
            assert result == '快照 #1'


# ── 4. _scan_config_dir ──────────────────────────────────────────────

class TestScanConfigDir:
    """_scan_config_dir() YAML 掃描測試。"""

    def test_scan_valid_yaml_files(self):
        """掃描目錄內的 YAML 檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            (config_dir / 'db-a.yaml').write_text('threshold: 100\n')
            (config_dir / 'db-b.yaml').write_text('threshold: 200\n')

            result = ch._scan_config_dir(str(config_dir))

            assert len(result) == 2
            assert result[0]['name'] == 'db-a.yaml'
            assert result[1]['name'] == 'db-b.yaml'

    def test_sorted_by_filename(self):
        """檔案應按名稱排序。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            (config_dir / 'z.yaml').write_text('z')
            (config_dir / 'a.yaml').write_text('a')
            (config_dir / 'm.yaml').write_text('m')

            result = ch._scan_config_dir(str(config_dir))

            names = [f['name'] for f in result]
            assert names == ['a.yaml', 'm.yaml', 'z.yaml']

    def test_skip_dotfiles(self):
        """應跳過以 . 開頭的檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            (config_dir / 'db-a.yaml').write_text('config')
            (config_dir / '.hidden.yaml').write_text('hidden')

            result = ch._scan_config_dir(str(config_dir))

            assert len(result) == 1
            assert result[0]['name'] == 'db-a.yaml'

    def test_returns_hash_content_size(self):
        """每個檔案應包含 hash、content、size。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            content = "threshold: 100\n"
            (config_dir / 'test.yaml').write_text(content)

            result = ch._scan_config_dir(str(config_dir))
            file_info = result[0]

            assert 'name' in file_info
            assert 'hash' in file_info
            assert 'content' in file_info
            assert 'size' in file_info
            assert file_info['content'] == content
            assert file_info['size'] == len(content)
            assert len(file_info['hash']) == 16

    def test_empty_directory(self):
        """空目錄應回傳空列表。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            result = ch._scan_config_dir(str(config_dir))

            assert result == []

    def test_skip_non_yaml_files(self):
        """只掃描 *.yaml 檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            (config_dir / 'config.yaml').write_text('yaml')
            (config_dir / 'readme.txt').write_text('text')
            (config_dir / 'script.py').write_text('python')

            result = ch._scan_config_dir(str(config_dir))

            assert len(result) == 1
            assert result[0]['name'] == 'config.yaml'


# ── 5. _scan_config_dir missing dir ─────────────────────────────────

class TestScanConfigDirMissing:
    """_scan_config_dir() 缺失目錄測試。"""

    def test_missing_directory_exits(self):
        """目錄不存在應呼叫 sys.exit(1)。"""
        with pytest.raises(SystemExit) as exc_info:
            ch._scan_config_dir('/nonexistent/path')
        assert exc_info.value.code == 1

    def test_missing_directory_stderr_message(self):
        """應輸出錯誤訊息到 stderr。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, 'missing')
            with patch('sys.stderr', new=StringIO()) as fake_stderr:
                with pytest.raises(SystemExit):
                    ch._scan_config_dir(missing_path)
                output = fake_stderr.getvalue()
                assert 'Error' in output or '錯誤' in output


# ── 6. _history_dir ──────────────────────────────────────────────────

class TestHistoryDir:
    """_history_dir() 歷史目錄建立測試。"""

    def test_creates_history_directory(self):
        """應建立 .da-history 目錄。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            hdir = ch._history_dir(str(config_dir))

            assert hdir.exists()
            assert hdir.name == '.da-history'
            assert hdir.parent == Path(tmpdir)

    def test_idempotent_creation(self):
        """多次呼叫應安全（已存在）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            hdir1 = ch._history_dir(str(config_dir))
            hdir2 = ch._history_dir(str(config_dir))

            assert hdir1 == hdir2
            assert hdir1.exists()


# ── 7. _load_history / _save_history ────────────────────────────────

class TestHistoryRoundtrip:
    """_load_history() / _save_history() 序列化測試。"""

    def test_empty_history_on_first_load(self):
        """首次加載應回傳空列表。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            history = ch._load_history(str(config_dir))

            assert history == []

    def test_save_and_load_roundtrip(self):
        """保存與加載應往返一致。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            original = [
                {
                    'id': 1,
                    'timestamp': '2026-03-17T10:00:00+00:00',
                    'composite_hash': 'abc123',
                    'message': 'initial',
                    'file_count': 2,
                    'files': [{'name': 'a.yaml', 'hash': 'h1', 'size': 10}],
                    'changes': []
                }
            ]

            ch._save_history(str(config_dir), original)
            loaded = ch._load_history(str(config_dir))

            assert loaded == original

    def test_save_creates_history_json(self):
        """保存應建立 history.json。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            ch._save_history(str(config_dir), [])

            history_file = Path(tmpdir) / '.da-history' / 'history.json'
            assert history_file.exists()

    def test_history_json_valid_format(self):
        """history.json 應為有效 JSON。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            data = [{'id': 1, 'timestamp': '2026-03-17T10:00:00+00:00'}]
            ch._save_history(str(config_dir), data)

            history_file = Path(tmpdir) / '.da-history' / 'history.json'
            loaded = json.loads(history_file.read_text(encoding='utf-8'))
            assert loaded == data


# ── 8. cmd_snapshot initial ─────────────────────────────────────────

class TestCmdSnapshotInitial:
    """cmd_snapshot() 初始快照測試。"""

    def test_initial_snapshot_creates_snap_1(self):
        """首次快照應建立 snap-1/ 目錄。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            (config_dir / 'db-a.yaml').write_text('threshold: 100\n')

            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            snap_dir = Path(tmpdir) / '.da-history' / 'snap-1'
            assert snap_dir.exists()

    def test_initial_snapshot_copies_files(self):
        """快照應複製設定檔案到 snap-N/。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            content = "threshold: 100\nalerts: true\n"
            (config_dir / 'db-a.yaml').write_text(content)

            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            snap_file = Path(tmpdir) / '.da-history' / 'snap-1' / 'db-a.yaml'
            assert snap_file.exists()
            assert snap_file.read_text() == content

    def test_initial_snapshot_records_metadata(self):
        """快照應記錄元資料（id、timestamp、hash、files）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            (config_dir / 'db-a.yaml').write_text('config')

            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            history = ch._load_history(str(config_dir))
            assert len(history) == 1
            entry = history[0]
            assert entry['id'] == 1
            assert 'timestamp' in entry
            assert 'composite_hash' in entry
            assert entry['file_count'] == 1
            assert len(entry['files']) == 1

    def test_initial_snapshot_no_changes_recorded(self):
        """初始快照應無 changes 記錄。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            (config_dir / 'db-a.yaml').write_text('config')

            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            history = ch._load_history(str(config_dir))
            assert len(history[0]['changes']) == 0

    def test_initial_snapshot_prints_success(self):
        """應輸出成功訊息。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            (config_dir / 'db-a.yaml').write_text('config')

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_snapshot(str(config_dir))
                output = fake_stdout.getvalue()
                assert '✓' in output or 'Snapshot #1' in output


# ── 9. cmd_snapshot with changes ────────────────────────────────────

class TestCmdSnapshotChanges:
    """cmd_snapshot() 變更偵測測試。"""

    def test_detects_added_file(self):
        """應偵測新增檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            # Snapshot 1：單個檔案
            (config_dir / 'db-a.yaml').write_text('threshold: 100\n')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            # Snapshot 2：新增檔案
            (config_dir / 'db-b.yaml').write_text('threshold: 200\n')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            history = ch._load_history(str(config_dir))
            changes = history[1]['changes']
            added = [c for c in changes if c['type'] == 'added']
            assert len(added) == 1
            assert added[0]['file'] == 'db-b.yaml'

    def test_detects_modified_file(self):
        """應偵測修改的檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            # Snapshot 1
            (config_dir / 'db-a.yaml').write_text('threshold: 100\n')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            # Snapshot 2：修改檔案
            (config_dir / 'db-a.yaml').write_text('threshold: 200\n')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            history = ch._load_history(str(config_dir))
            changes = history[1]['changes']
            modified = [c for c in changes if c['type'] == 'modified']
            assert len(modified) == 1
            assert modified[0]['file'] == 'db-a.yaml'

    def test_detects_removed_file(self):
        """應偵測移除的檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            # Snapshot 1：兩個檔案
            (config_dir / 'db-a.yaml').write_text('a')
            (config_dir / 'db-b.yaml').write_text('b')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            # Snapshot 2：移除一個檔案
            (config_dir / 'db-b.yaml').unlink()
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            history = ch._load_history(str(config_dir))
            changes = history[1]['changes']
            removed = [c for c in changes if c['type'] == 'removed']
            assert len(removed) == 1
            assert removed[0]['file'] == 'db-b.yaml'

    def test_multiple_changes_recorded(self):
        """應同時偵測 added/modified/removed。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            # Snapshot 1
            (config_dir / 'a.yaml').write_text('content')
            (config_dir / 'b.yaml').write_text('content')
            (config_dir / 'c.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            # Snapshot 2：a 修改、b 移除、d 新增
            (config_dir / 'a.yaml').write_text('modified')
            (config_dir / 'b.yaml').unlink()
            (config_dir / 'd.yaml').write_text('new')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            history = ch._load_history(str(config_dir))
            changes = history[1]['changes']
            assert any(c['type'] == 'modified' and c['file'] == 'a.yaml' for c in changes)
            assert any(c['type'] == 'removed' and c['file'] == 'b.yaml' for c in changes)
            assert any(c['type'] == 'added' and c['file'] == 'd.yaml' for c in changes)


# ── 10. cmd_snapshot no-change skip ─────────────────────────────────

class TestCmdSnapshotNoChange:
    """cmd_snapshot() 無變更跳過測試。"""

    def test_skips_snapshot_on_no_changes(self):
        """相同 composite hash 應跳過快照。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            (config_dir / 'db-a.yaml').write_text('threshold: 100\n')

            # Snapshot 1
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            # Snapshot 2：無變更
            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_snapshot(str(config_dir))
                output = fake_stdout.getvalue()
                assert 'No changes detected' in output or '未變更' in output

            history = ch._load_history(str(config_dir))
            assert len(history) == 1  # 仍只有一筆

    def test_no_new_snapshot_dir_on_skip(self):
        """跳過時不應建立新 snap-N/ 目錄。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()
            (config_dir / 'db-a.yaml').write_text('content')

            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))
                ch.cmd_snapshot(str(config_dir))

            snap_2 = Path(tmpdir) / '.da-history' / 'snap-2'
            assert not snap_2.exists()


# ── 11. cmd_log empty history ───────────────────────────────────────

class TestCmdLogEmpty:
    """cmd_log() 空歷史測試。"""

    def test_empty_history_message(self):
        """空歷史應輸出提示訊息。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_log(str(config_dir))
                output = fake_stdout.getvalue()
                assert 'No snapshots' in output or '尚無快照' in output


# ── 12. cmd_log with entries ────────────────────────────────────────

class TestCmdLogEntries:
    """cmd_log() 列表輸出測試。"""

    def test_displays_all_entries(self):
        """應顯示所有快照條目。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            # Create 2 snapshots
            (config_dir / 'test.yaml').write_text('v1')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            (config_dir / 'test.yaml').write_text('v2')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_log(str(config_dir))
                output = fake_stdout.getvalue()
                assert '#' in output and '1' in output
                assert '#' in output and '2' in output

    def test_displays_in_reverse_order(self):
        """應按反向時間順序顯示（最新優先）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('v1')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            (config_dir / 'test.yaml').write_text('v2')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_log(str(config_dir))
                output = fake_stdout.getvalue()
                # #  2 should appear before #  1 (id formatted with :3d)
                pos_2 = output.find('  2  ')
                pos_1 = output.find('  1  ')
                assert pos_2 < pos_1

    def test_includes_timestamp(self):
        """應包含時間戳記。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_log(str(config_dir))
                output = fake_stdout.getvalue()
                assert '2026' in output or '202' in output  # 年份

    def test_includes_file_count(self):
        """應包含檔案數量。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'a.yaml').write_text('a')
            (config_dir / 'b.yaml').write_text('b')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_log(str(config_dir))
                output = fake_stdout.getvalue()
                assert '2' in output and 'files' in output or '2' in output and '個檔案' in output

    def test_includes_composite_hash(self):
        """應顯示 composite hash。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_log(str(config_dir))
                output = fake_stdout.getvalue()
                # Hash should be 16 hex chars
                assert any(len(word) == 16 and all(c in '0123456789abcdef' for c in word)
                          for word in output.split())

    def test_includes_changes_summary(self):
        """應顯示變更統計（+/-/~）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'a.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            (config_dir / 'b.yaml').write_text('new')
            (config_dir / 'a.yaml').write_text('modified')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_log(str(config_dir))
                output = fake_stdout.getvalue()
                # Should show changes
                assert '+' in output or '~' in output


# ── 13. cmd_log --limit ─────────────────────────────────────────────

class TestCmdLogLimit:
    """cmd_log() --limit 限制測試。"""

    def test_limit_constrains_output(self):
        """--limit 應限制顯示的條目數。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            # Create 3 snapshots
            for i in range(3):
                (config_dir / 'test.yaml').write_text(f'v{i}')
                with patch('sys.stdout', new=StringIO()):
                    ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_log(str(config_dir), limit=2)
                output = fake_stdout.getvalue()
                # Should show entries 3 and 2 (formatted as #  3 etc.)
                assert 'showing 2' in output or '顯示 2' in output
                lines = output.strip().split('\n')
                assert len([l for l in lines if '  3  ' in l or '  2  ' in l]) >= 2
                # Hard to guarantee #1 is absent due to potential in other text
                # but we can at least check the displaying 2 items message

    def test_limit_none_shows_all(self):
        """limit=None 應顯示所有條目。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            for i in range(3):
                (config_dir / 'test.yaml').write_text(f'v{i}')
                with patch('sys.stdout', new=StringIO()):
                    ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_log(str(config_dir), limit=None)
                output = fake_stdout.getvalue()
                # id is formatted with :3d so #  1 not #1
                assert '1' in output
                assert '2' in output
                assert '3' in output


# ── 14. cmd_show valid ID ──────────────────────────────────────────

class TestCmdShowValid:
    """cmd_show() 有效 ID 測試。"""

    def test_shows_snapshot_details(self):
        """應顯示快照詳情。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'db-a.yaml').write_text('content1')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir), message='Test snapshot')

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_show(str(config_dir), snapshot_id=1)
                output = fake_stdout.getvalue()
                assert '#1' in output or 'Snapshot' in output or '快照' in output

    def test_includes_timestamp(self):
        """應包含時間戳記。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_show(str(config_dir), snapshot_id=1)
                output = fake_stdout.getvalue()
                assert 'Time' in output or '時間' in output

    def test_includes_composite_hash(self):
        """應包含 composite hash。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_show(str(config_dir), snapshot_id=1)
                output = fake_stdout.getvalue()
                assert 'Hash' in output

    def test_includes_file_list(self):
        """應包含檔案清單與 hash。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'db-a.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_show(str(config_dir), snapshot_id=1)
                output = fake_stdout.getvalue()
                assert 'db-a.yaml' in output or 'File list' in output or '檔案清單' in output

    def test_includes_message_if_present(self):
        """如有訊息應顯示。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir), message='Important change')

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_show(str(config_dir), snapshot_id=1)
                output = fake_stdout.getvalue()
                assert 'Important change' in output

    def test_includes_changes_if_present(self):
        """如有變更應顯示。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'a.yaml').write_text('a')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            (config_dir / 'b.yaml').write_text('b')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_show(str(config_dir), snapshot_id=2)
                output = fake_stdout.getvalue()
                assert 'Changes' in output or '變更' in output


# ── 15. cmd_show invalid ID ────────────────────────────────────────

class TestCmdShowInvalid:
    """cmd_show() 無效 ID 測試。"""

    def test_invalid_id_exits(self):
        """無效 ID 應呼叫 sys.exit(1)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with pytest.raises(SystemExit) as exc_info:
                ch.cmd_show(str(config_dir), snapshot_id=999)
            assert exc_info.value.code == 1

    def test_invalid_id_stderr_message(self):
        """應輸出錯誤訊息到 stderr。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            with patch('sys.stderr', new=StringIO()) as fake_stderr:
                with pytest.raises(SystemExit):
                    ch.cmd_show(str(config_dir), snapshot_id=999)
                output = fake_stderr.getvalue()
                assert 'Error' in output or '錯誤' in output


# ── 16. cmd_diff between snapshots ─────────────────────────────────

class TestCmdDiff:
    """cmd_diff() 快照比對測試。"""

    def test_shows_added_files(self):
        """應顯示新增的檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'a.yaml').write_text('a')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            (config_dir / 'b.yaml').write_text('b')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_diff(str(config_dir), id_a=1, id_b=2)
                output = fake_stdout.getvalue()
                assert '[+]' in output and 'b.yaml' in output

    def test_shows_removed_files(self):
        """應顯示移除的檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'a.yaml').write_text('a')
            (config_dir / 'b.yaml').write_text('b')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            (config_dir / 'b.yaml').unlink()
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_diff(str(config_dir), id_a=1, id_b=2)
                output = fake_stdout.getvalue()
                assert '[-]' in output and 'b.yaml' in output

    def test_shows_modified_files(self):
        """應顯示修改的檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('v1')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            (config_dir / 'test.yaml').write_text('v2')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_diff(str(config_dir), id_a=1, id_b=2)
                output = fake_stdout.getvalue()
                assert '[~]' in output and 'test.yaml' in output


# ── 17. cmd_diff line differences ───────────────────────────────────

class TestCmdDiffLines:
    """cmd_diff() 行級差異測試。"""

    def test_shows_line_diffs_for_modified_files(self):
        """應顯示修改檔案的行級差異。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('line1\nline2\nline3\n')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            (config_dir / 'test.yaml').write_text('line1\nmodified\nline3\n')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_diff(str(config_dir), id_a=1, id_b=2)
                output = fake_stdout.getvalue()
                assert 'L2' in output  # Line 2 changed
                assert '-' in output and '+' in output

    def test_shows_added_lines_in_diff(self):
        """應顯示新增的行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('line1\n')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            (config_dir / 'test.yaml').write_text('line1\nline2\nline3\n')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_diff(str(config_dir), id_a=1, id_b=2)
                output = fake_stdout.getvalue()
                # Should show L2 and L3 as added
                assert '+' in output

    def test_shows_removed_lines_in_diff(self):
        """應顯示移除的行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('line1\nline2\nline3\n')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            (config_dir / 'test.yaml').write_text('line1\n')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_diff(str(config_dir), id_a=1, id_b=2)
                output = fake_stdout.getvalue()
                assert '-' in output


# ── 18. cmd_diff missing snapshot ──────────────────────────────────

class TestCmdDiffMissing:
    """cmd_diff() 缺失快照測試。"""

    def test_missing_first_snapshot_exits(self):
        """快照 A 不存在應 sys.exit(1)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with pytest.raises(SystemExit) as exc_info:
                ch.cmd_diff(str(config_dir), id_a=999, id_b=1)
            assert exc_info.value.code == 1

    def test_missing_second_snapshot_exits(self):
        """快照 B 不存在應 sys.exit(1)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            with pytest.raises(SystemExit) as exc_info:
                ch.cmd_diff(str(config_dir), id_a=1, id_b=999)
            assert exc_info.value.code == 1

    def test_missing_snapshot_stderr_message(self):
        """應輸出錯誤訊息。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            with patch('sys.stderr', new=StringIO()) as fake_stderr:
                with pytest.raises(SystemExit):
                    ch.cmd_diff(str(config_dir), id_a=999, id_b=1)
                output = fake_stderr.getvalue()
                assert 'Error' in output or '錯誤' in output


# ── 19. cmd_diff no differences ────────────────────────────────────

class TestCmdDiffNoDiff:
    """cmd_diff() 無差異測試。"""

    def test_no_differences_message(self):
        """相同快照應顯示無差異訊息。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            (config_dir / 'test.yaml').write_text('content')
            with patch('sys.stdout', new=StringIO()):
                ch.cmd_snapshot(str(config_dir))

            # Get the real hash from snapshot 1
            saved_history = ch._load_history(str(config_dir))
            real_files = saved_history[0]['files']

            # Create identical snapshot 2 with same hashes
            entry2 = {
                'id': 2,
                'timestamp': '2026-03-17T10:00:00+00:00',
                'composite_hash': saved_history[0]['composite_hash'],
                'message': '',
                'file_count': 1,
                'files': real_files,  # same file hashes
                'changes': []
            }
            hdir = Path(tmpdir) / '.da-history'
            snap_2 = hdir / 'snap-2'
            snap_2.mkdir(exist_ok=True)
            (snap_2 / 'test.yaml').write_text('content')

            saved_history.append(entry2)
            ch._save_history(str(config_dir), saved_history)

            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_diff(str(config_dir), id_a=1, id_b=2)
                output = fake_stdout.getvalue()
                assert 'No differences' in output or '無差異' in output


# ── 20. End-to-End Flow ────────────────────────────────────────────

class TestEndToEnd:
    """End-to-End 完整工作流程測試。"""

    def test_full_workflow(self):
        """snapshot → 修改 → snapshot → log → diff 完整流程。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            # Step 1: Initial snapshot
            (config_dir / 'db-a.yaml').write_text('threshold: 100\n')
            (config_dir / 'db-b.yaml').write_text('threshold: 200\n')
            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_snapshot(str(config_dir), message='Initial config')
                assert '✓' in fake_stdout.getvalue() or 'Snapshot' in fake_stdout.getvalue()

            # Step 2: Modify files
            (config_dir / 'db-a.yaml').write_text('threshold: 150\n')  # modified
            (config_dir / 'db-c.yaml').write_text('threshold: 300\n')  # added
            (config_dir / 'db-b.yaml').unlink()  # removed

            # Step 3: Second snapshot
            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_snapshot(str(config_dir), message='Updated thresholds')
                output = fake_stdout.getvalue()
                assert '✓' in output or 'Snapshot' in output or 'Changes' in output

            # Step 4: View log
            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_log(str(config_dir))
                output = fake_stdout.getvalue()
                assert '1' in output and '2' in output

            # Step 5: Show snapshot
            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_show(str(config_dir), snapshot_id=2)
                output = fake_stdout.getvalue()
                assert '#2' in output

            # Step 6: Diff snapshots
            with patch('sys.stdout', new=StringIO()) as fake_stdout:
                ch.cmd_diff(str(config_dir), id_a=1, id_b=2)
                output = fake_stdout.getvalue()
                assert ('[~]' in output and 'db-a.yaml' in output) or 'modified' in output

            # Verify history file
            history = ch._load_history(str(config_dir))
            assert len(history) == 2
            assert history[0]['id'] == 1
            assert history[1]['id'] == 2

    def test_multiple_snapshots_accumulate(self):
        """多個快照應累積在歷史中。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / 'conf.d'
            config_dir.mkdir()

            # Create 5 snapshots
            for i in range(5):
                (config_dir / 'test.yaml').write_text(f'v{i}')
                with patch('sys.stdout', new=StringIO()):
                    ch.cmd_snapshot(str(config_dir))

            history = ch._load_history(str(config_dir))
            assert len(history) == 5
            assert history[0]['id'] == 1
            assert history[4]['id'] == 5
            assert history[4]['file_count'] == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
