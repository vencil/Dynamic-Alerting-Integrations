#!/usr/bin/env python3
"""VS Code Git 整合開關 — 切換 .vscode/settings.json 中的 Git 設定。

用途：
  在 AI Agent (Cowork / Claude Code) session 開始時關閉 VS Code 的
  背景 Git 操作，避免 FUSE 掛載下的 phantom lock；session 結束或
  手動開發時再打開。

  VS Code 會即時 hot-reload settings.json，不需重啟。

用法：
  python scripts/session-guards/vscode_git_toggle.py off   # 關閉 Git（Agent 模式）
  python scripts/session-guards/vscode_git_toggle.py on    # 打開 Git（手動模式）
  python scripts/session-guards/vscode_git_toggle.py       # 顯示目前狀態

設計原則：
  - 只動 git.enabled / git.autoRepositoryDetection / git.autofetch
  - 保留 settings.json 裡其他所有設定不動
  - 檔案不存在時自動建立；.vscode/ 目錄不存在時自動建立
  - settings.json 已在 .gitignore 排除，不會進 repo
"""

import json
import os
import sys
from pathlib import Path

# Git 相關的 key 與「關閉」時的值
GIT_KEYS_OFF = {
    "git.enabled": False,
    "git.autoRepositoryDetection": False,
    "git.autofetch": False,
}

GIT_KEYS_ON = {
    "git.enabled": True,
    "git.autoRepositoryDetection": True,
    "git.autofetch": True,
}


def find_repo_root() -> Path:
    """從 cwd 向上找 .git/ 所在的 repo root。"""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    # fallback: 用腳本自己的位置推算 (scripts/session-guards/ → repo root)
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent


def load_settings(settings_path: Path) -> dict:
    """讀取現有 settings.json，不存在則回傳空 dict。"""
    if settings_path.exists():
        try:
            with open(settings_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_settings(settings_path: Path, data: dict) -> None:
    """寫入 settings.json，確保目錄存在且格式美觀。"""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.write("\n")


def get_status(settings: dict) -> str:
    """判斷目前 Git 開關狀態。"""
    git_enabled = settings.get("git.enabled", True)  # VS Code 預設 True
    if git_enabled:
        return "on"
    return "off"


def apply_toggle(settings_path: Path, action: str) -> None:
    """套用 on/off 切換。"""
    settings = load_settings(settings_path)

    if action == "off":
        settings.update(GIT_KEYS_OFF)
        save_settings(settings_path, settings)
        print("✅ VS Code Git 已關閉（Agent 模式）")
        print("   背景 fetch / status 不再觸碰 .git/")
    elif action == "on":
        settings.update(GIT_KEYS_ON)
        save_settings(settings_path, settings)
        print("✅ VS Code Git 已開啟（手動模式）")
    else:
        current = get_status(settings)
        label = "開啟（手動模式）" if current == "on" else "關閉（Agent 模式）"
        print(f"目前狀態：Git {label}")
        print(f"  git.enabled = {settings.get('git.enabled', '(預設 True)')}")
        print(f"  git.autofetch = {settings.get('git.autofetch', '(預設 True)')}")
        print()
        print("用法：")
        print("  python scripts/session-guards/vscode_git_toggle.py off  # 關閉")
        print("  python scripts/session-guards/vscode_git_toggle.py on   # 打開")


def main() -> None:
    repo_root = find_repo_root()
    settings_path = repo_root / ".vscode" / "settings.json"

    action = sys.argv[1].lower() if len(sys.argv) > 1 else "status"

    if action not in ("on", "off", "status"):
        print(f"未知指令: {action}", file=sys.stderr)
        print("用法: vscode_git_toggle.py [on|off]", file=sys.stderr)
        sys.exit(1)

    apply_toggle(settings_path, action)


if __name__ == "__main__":
    main()
