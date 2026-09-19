#!/usr/bin/env python3
"""generate_nav.py 的行為守衛。

本工具**不是閘門**：它印一份 nav 草稿，rc 永遠是 0（除非 `docs/` 不存在）。
「文件漏收進 nav」由 `mkdocs.yml` 的 `validation.nav.*` 在 build 時判定，經
`mkdocs_strict_check.sh` 轉成 rc（#1903）。⇒ 這裡沒有任何一格在驗 rc 承載的違規
語意，因為它不再承載。

⛔ 本檔最重要的兩格，守的是這支工具**實際燒掉的兩個缺陷**，而不是它宣稱的功能：

  1. **草稿的路徑座標系**。退役掉的那半邊之所以從來沒對過，是因為它拿 nav 條目
     （相對 `docs_dir`：`adr/001-….md`）去比對掃描結果（相對 repo root：
     `docs/adr/001-….md`），交集實測為 0。⇒ 草稿必須印 nav 自己的座標，否則
     每一行都要手工改過才能貼。
  2. **這支工具不准再讀 `mkdocs.yml`**。#1903 在 mkdocs.yml 加了
     `validation:` → `nav:`，而舊的剖析用 `line.strip() == 'nav:'` 比對，於是它
     在第 61 行就以為進了 nav 區塊、在下一個頂層鍵 break，真正的 nav（283 行）
     永遠讀不到——`109 in nav` 一夜變成 `0`，而**沒有任何東西轉紅**，因為全 repo
     只有 `make generate-nav` 一個呼叫點。⇒ 那一格用「給它兩份內容天差地遠的
     mkdocs.yml、以及完全不給」來釘住它的輸出不受其影響。

其餘各格是 front matter 解析與 section 分類，來源是 #1884。
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

import generate_nav as gn  # noqa: E402  (path via conftest)

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "tools" / "dx" / "generate_nav.py"

EXIT_OK = 0
EXIT_CALLER_ERROR = 2


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--repo-root", str(repo), *args],
        capture_output=True, text=True, timeout=60,
    )


def _fm(title: str, tags: str = "[]", lang: str = "zh") -> str:
    """一份最小但合法的 front matter（`---` 必須落在第 0 byte，見下方那一格）。"""
    return f'---\ntitle: "{title}"\ntags: {tags}\nlang: {lang}\n---\n# {title}\n'


def _tree(tmp_path: Path, *, docs: dict, mkdocs_yml: str | None = None) -> Path:
    """合成一棵最小 repo root。

    docs        — {相對 repo root 的路徑: 檔案內容}
    mkdocs_yml  — mkdocs.yml 的整份內容；`None` 表示**完全不建立這個檔**。
                  ⛔ 預設就是 `None`：這支工具不該讀它，測試也不該餵它。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    for rel, body in docs.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    if mkdocs_yml is not None:
        (tmp_path / "mkdocs.yml").write_text(mkdocs_yml, encoding="utf-8")
    return tmp_path


def _draft(repo: Path) -> str:
    """跑一次並回傳 stdout（rc 必須是 0）。"""
    proc = _run(repo)
    assert proc.returncode == EXIT_OK, (
        f"草稿模式不該失敗，rc={proc.returncode}\nstderr:\n{proc.stderr}"
    )
    return proc.stdout


# --- CLI 契約：已退役的旗標必須被拒絕，不是被靜默忽略 -----------------------


@pytest.mark.parametrize("flag", ["--update", "--check"])
def test_a_retired_flag_is_rejected_not_silently_ignored(flag):
    """⛔ 兩個旗標都曾經存在、都會誤導呼叫者，刪掉之後必須**報錯**。

    `--update` 宣稱會寫回 mkdocs.yml，但 `args.update` 從未被讀取：實測帶著它跑
    rc 0、mkdocs.yml 的 sha256 前後相同（#1884 刪除）。
    `--check` 宣稱在守「文件漏收進 nav」，但它比對的兩個集合不在同一個座標系，
    交集實測為 0 ⇒ 它把幾乎每份文件都報成 missing，那個 rc 沒有人能拿來行動。

    ⇒ 守的是「不留說謊的介面」，不是「永遠不准有這兩個名字」：真要實作寫回或
    真要在這裡做閘門，連同這一格一起改。
    """
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--repo-root", ".", flag],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == EXIT_CALLER_ERROR, (
        f"{flag} 應被 argparse 拒絕（rc {EXIT_CALLER_ERROR}），實得 rc={proc.returncode}。"
        f"\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert flag in proc.stderr, "argparse 的拒絕訊息應指名這個未知旗標"


# --- 草稿的座標系與資料來源 ------------------------------------------------


def test_the_draft_emits_paths_relative_to_docs_dir_not_to_the_repo_root(tmp_path):
    """⛔ 草稿的路徑必須是 nav 自己的座標（相對 `docs_dir`）。

    這一格守的就是退役掉那半邊的死因。斷言**雙向**：正確形式在、repo-root 形式
    不在——只驗前半的話，一支把兩種都印出來的實作照樣綠。
    """
    repo = _tree(tmp_path, docs={"docs/adr/001-x.md": _fm("ADR One", tags="[adr]")})
    out = _draft(repo)
    assert "- ADR One: adr/001-x.md" in out, (
        f"草稿要能直接貼進 nav ⇒ 路徑相對 docs_dir\n{out}"
    )
    assert "docs/adr/001-x.md" not in out, (
        f"repo-root 相對路徑貼進 nav 會指向 docs/docs/…，不可出現\n{out}"
    )


def test_the_draft_does_not_depend_on_mkdocs_yml_at_all(tmp_path):
    """⛔ 餵三種 mkdocs.yml（含會絆倒舊剖析的那種）與完全不餵，輸出必須逐字元相同。

    #1903 在 mkdocs.yml 加了 `validation:` → `nav:`；舊剖析用
    `line.strip() == 'nav:'` 比對，於是在那個縮排的 `nav:` 就進入 nav 區塊、
    在下一個頂層鍵 break，真正的 nav 永遠讀不到。沒有任何閘門在跑這支工具，
    所以它靜默壞掉。⇒ 讓「它不讀那個檔」成為一件**被守住**的事，而不是一句散文。

    ⚠️ 反空轉：先斷言草稿真的有內容。少了它，一支對任何輸入都印空字串的實作，
    「三者相同」會平凡為真。
    """
    docs = {"docs/a.md": _fm("Doc A", tags="[architecture]")}
    variants = {
        "沒有 mkdocs.yml": None,
        "有 nav 區塊": "site_name: t\nnav:\n  - Doc A: a.md\n",
        "validation.nav 在前、真 nav 在後": (
            "site_name: t\nvalidation:\n  nav:\n    omitted_files: warn\n"
            "theme:\n  name: material\nnav:\n  - Doc A: a.md\n"
        ),
    }
    outs = {}
    for label, yml in variants.items():
        repo = _tree(tmp_path / label.replace(" ", "_"), docs=docs, mkdocs_yml=yml)
        outs[label] = _draft(repo)

    first = next(iter(outs.values()))
    assert "- Doc A: a.md" in first, f"反空轉：草稿必須真的有內容\n{first}"
    for label, out in outs.items():
        assert out == first, (
            f"mkdocs.yml 的內容改變了輸出（變體「{label}」）——這支工具不該讀它\n"
            f"--- {label} ---\n{out}\n--- 基準 ---\n{first}"
        )


def test_bridged_rule_packs_are_drafted_at_their_nav_path(tmp_path):
    """`rule-packs/*.md` 由 mkdocs hook 橋進站台，其 nav 路徑就是 repo 相對路徑。

    ⚠️ `docs/rule-packs/` 在磁碟上不存在（實測 `ls` 不到、git 也沒有），是
    `scripts/mkdocs/rule_packs_bridge.py` 在 build 時生出來的。⇒ 掃描面必須另外
    涵蓋它，否則草稿會漏掉整個 Rule Pack 區段。

    ⚠️ 反空轉：同時斷言 repo root 底下**沒有被橋接**的 `.md` 不進草稿——它們不可能
    成為 nav 條目，印出來只會誤導。
    """
    repo = _tree(tmp_path, docs={"docs/a.md": _fm("Doc A")})
    (repo / "rule-packs").mkdir()
    (repo / "rule-packs" / "README.md").write_text(_fm("Rule Packs"), encoding="utf-8")
    (repo / "CLAUDE.md").write_text(_fm("Not Bridged"), encoding="utf-8")

    out = _draft(repo)
    assert "- Rule Packs: rule-packs/README.md" in out, f"橋接的檔案要進草稿\n{out}"
    assert "Not Bridged" not in out, (
        f"repo root 的 .md 沒有被橋接，不可能是 nav 條目，不該進草稿\n{out}"
    )


def test_excluded_and_english_docs_stay_out_of_the_draft(tmp_path):
    """`EXCLUDE_PATTERNS` 與 `.en.md` 不進草稿。

    ⚠️ 反空轉：同一棵樹裡放一份**該**出現的文件。少了它，一支掃描面歸零的實作
    （什麼都不印）也會讓前兩個斷言通過。
    """
    repo = _tree(
        tmp_path,
        docs={
            "docs/internal/notes.md": _fm("Internal"),
            "docs/guide.en.md": _fm("English"),
            "docs/included.md": _fm("Included"),
        },
    )
    out = _draft(repo)
    assert "included.md" in out, f"反空轉：這一份必須被印出來\n{out}"
    assert "internal/notes.md" not in out, f"internal/ 應被排除\n{out}"
    assert "guide.en.md" not in out, f".en.md 應被跳過\n{out}"


def test_a_doc_without_front_matter_falls_back_to_its_filename(tmp_path):
    """端到端：沒有 front matter 的文件，草稿裡的標題是檔名 stem。"""
    repo = _tree(tmp_path, docs={"docs/no-front-matter.md": "# 只有一個標題\n"})
    out = _draft(repo)
    assert "- no-front-matter: no-front-matter.md" in out, f"標題應退回 stem\n{out}"


def test_a_missing_docs_dir_is_a_caller_error(tmp_path):
    """`docs/` 不存在 ⇒ rc 2 並印到 stderr，而不是印一份空草稿說一切正常。"""
    proc = _run(tmp_path)
    assert proc.returncode == EXIT_CALLER_ERROR, (
        f"實得 rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "docs directory not found" in proc.stderr


# --- front matter 解析 -----------------------------------------------------


def test_front_matter_must_start_at_the_very_first_byte(tmp_path):
    """開頭多一個空行，整份 front matter 就不被認（正規表達式錨在 `^---`）。

    ⚠️ 這不是缺陷而是現況，值得釘住的理由是**它會靜默降級**：解析失敗不報錯，
    title 直接退回檔名 stem，草稿看起來仍然正常。
    """
    good = tmp_path / "good.md"
    bad = tmp_path / "bad.md"
    good.write_text(_fm("Real Title"), encoding="utf-8")
    bad.write_text("\n" + _fm("Real Title"), encoding="utf-8")

    assert gn.extract_front_matter(good) == {
        "title": '"Real Title"', "tags": "[]", "lang": "zh"}
    assert gn.extract_front_matter(bad) == {}, "前面有空行 ⇒ 整份不認"


def test_a_colon_inside_a_value_survives_the_line_wise_split(tmp_path):
    """解析是逐行 `split(':', 1)`，所以值裡的冒號不會被吃掉。"""
    f = tmp_path / "x.md"
    f.write_text('---\ntitle: "A: B"\ntags: [x]\n---\n', encoding="utf-8")
    assert gn.extract_front_matter(f)["title"] == '"A: B"'


def test_a_line_without_a_colon_is_skipped_not_fatal(tmp_path):
    """front matter 裡的裸行被跳過，其餘鍵照常解析——不是整份放棄。"""
    f = tmp_path / "x.md"
    f.write_text('---\ntitle: "A"\njust-a-bare-line\ntags: [y]\n---\n', encoding="utf-8")
    assert gn.extract_front_matter(f) == {"title": '"A"', "tags": "[y]"}


def test_an_unreadable_or_missing_file_parses_to_empty(tmp_path):
    """讀不到的檔回空 dict 而不是丟例外——`scan_docs` 靠這個不中斷。"""
    assert gn.extract_front_matter(tmp_path / "nope.md") == {}


# --- classify_section ------------------------------------------------------


@pytest.mark.parametrize("tags, expected", [
    ("[getting-started]", "快速入門"),
    ("[architecture]", "核心架構"),
    ("[integration]", "整合指南"),
    ("[migration]", "遷移"),
    ("[governance]", "治理與安全"),
    ("[scenario]", "場景"),
    ("[reference]", "參考"),
    ("[adr]", "參考"),
])
def test_every_section_map_key_routes_to_its_own_section(tags, expected):
    """`SECTION_MAP` 的每一條都要被走過一次。

    ⚠️ 少了這個全覆蓋，只驗一兩條的話，一支把其中某條對應改錯的實作會漏掉。
    """
    assert gn.classify_section(tags) == expected


@pytest.mark.parametrize("tags", ["[architecture, reference]", "[reference, architecture]"])
def test_section_map_order_decides_the_winner_not_the_string_order(tags):
    """⛔ 多個 key 同時命中時，贏家由 `SECTION_MAP` 的**迭代順序**決定。

    兩種字串順序都必須回「核心架構」——因為 `architecture` 在 map 裡排在
    `reference` 前面。⇒ 這一格守的是一個**沒有寫在任何地方的隱性契約**：重排
    `SECTION_MAP` 的字面順序會改變分類結果。它紅掉代表那個順序被動了。
    """
    assert gn.classify_section(tags) == "核心架構"


def test_tag_matching_is_case_insensitive():
    assert gn.classify_section("[ARCHITECTURE]") == "核心架構"


@pytest.mark.parametrize("tags", ["[nonsense]", "[]", ""])
def test_unknown_or_empty_tags_fall_back_to_the_reference_section(tags):
    """認不得就歸「參考」。⚠️ 與 `[reference]` / `[adr]` 的結果相同，所以只看
    輸出分不出「分類正確」與「全部掉進預設」——上面那個全覆蓋格才是分辨器。"""
    assert gn.classify_section(tags) == "參考"


def _section_block(report: str, heading: str) -> str:
    """草稿裡 `  {heading}:` 底下、**到下一個 section 標題為止**的那一段。

    ⛔ 不可以用 `report[report.index(heading):]`：那會一路延伸到輸出結尾，於是
    「兩個標題都還在、但文件全被塞進後面那個 section」也會通過。實測過那個假報告
    （`快速入門:` 空著、兩份文件都在 `核心架構:` 底下），未加邊界的版本全綠。
    """
    heads = [m.start() for m in re.finditer(r"^  \S+:$", report, re.M)]
    start = report.index(f"  {heading}:")
    after = [h for h in heads if h > start]
    return report[start:after[0]] if after else report[start:]


def test_docs_are_grouped_under_their_section_heading(tmp_path):
    """端到端：文件依 tags 被列在各自的 section 標題底下。

    ⚠️ 斷言**雙向**：每份文件要在自己的 section 區塊裡，而且**不在**另一個區塊裡。
    只驗前半的話，一支把兩份都歸到同一個 section 的實作照樣綠。
    """
    repo = _tree(
        tmp_path,
        docs={
            "docs/arch.md": _fm("Arch Doc", tags="[architecture]"),
            "docs/start.md": _fm("Start Doc", tags="[getting-started]"),
        },
    )
    out = _draft(repo)
    assert "Scanned 2 docs" in out, f"前置條件：兩份都該被掃到\n{out}"

    arch_block = _section_block(out, "核心架構")
    start_block = _section_block(out, "快速入門")
    assert "- Arch Doc: arch.md" in arch_block, f"arch 應在核心架構底下\n{out}"
    assert "- Start Doc: start.md" in start_block, f"start 應在快速入門底下\n{out}"
    assert "- Start Doc" not in arch_block, f"start 不該出現在核心架構區塊\n{out}"
    assert "- Arch Doc" not in start_block, f"arch 不該出現在快速入門區塊\n{out}"
