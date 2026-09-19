#!/usr/bin/env python3
"""generate_nav.py 的行為守衛。

分兩層，來源不同、判準也不同：

**第一層 — CLI 契約**（刪 `--update` 那一役量的，見下）。
**第二層 — 內容驗證**（#1884 要的那組：nav 比對結果、front matter 解析、section
分類）。⚠️ 第一層只問 rc，而 rc 只承載「有沒有 missing」；一支 nav 比對整個算錯、
但剛好沒有 missing 的實作，第一層全綠。⇒ 內容驗證不是第一層的加強版，是另一個問題。

第一層釘住的是：

  1. `--update` 已不存在。它曾經被宣告、被 `--help` 與三處散文宣傳，卻從來沒有實作
     （`args.update` 從未被讀取）——跑它與不帶旗標完全同義，是一個會說謊的介面。
  2. `--check` 的兩個方向。⚠️ 沒有這一半，第 1 格會是平凡為真：一支整個壞掉、對任何
     argv 都回 rc 2 的實作，照樣能讓「`--update` 被拒絕」通過。

  3. `--check` 對 `extra`（nav 列了、檔案不在）回 rc 0。⚠️ 這一格是**現況存證，不是
     規格**：這一役的 docstring 把這個行為寫進散文，寫下而不守就是下一句會腐爛的散文；
     而 #1884 認定它是缺口。⇒ 釘住是為了「有人改動它時會有東西喊」。讀到它紅不要當成
     回歸，去看 #1884 是不是把 `extra` 改成觸發 rc 1 了。

第二層（#1884）釘住的是**報告內容**而不是 rc：missing / extra 各自列了哪些路徑、
front matter 怎麼被解析、tags 怎麼被分到 section。每一格都先斷言合成樹真的造出它
宣稱的狀態，再問結論——否則「乾淨 ⇒ 沒報任何東西」在一支掃描面歸零的實作上也是綠的。
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
EXIT_VIOLATION = 1
EXIT_CALLER_ERROR = 2


def _repo(tmp_path: Path, *, nav_lists_the_doc: bool) -> Path:
    """合成一個最小 repo root：一份帶 front matter 的文件 + 一份 mkdocs.yml。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "orphan.md").write_text(
        '---\ntitle: "Orphan"\ntags: [dx]\nlang: zh\n---\n# Orphan\n', encoding="utf-8")
    nav = "  - Orphan: docs/orphan.md\n" if nav_lists_the_doc else "  - Home: index.md\n"
    (tmp_path / "mkdocs.yml").write_text(f"site_name: t\nnav:\n{nav}", encoding="utf-8")
    return tmp_path


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--repo-root", str(repo), *args],
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("listed, expected", [
    (False, EXIT_VIOLATION),   # 文件在、nav 沒列 ⇒ 違規
    (True, EXIT_OK),           # nav 列了 ⇒ 乾淨
])
def test_check_reports_both_directions(tmp_path, listed, expected):
    """`--check` 兩個方向都要動，否則「永遠回 0」與「永遠回 1」都能過。"""
    repo = _repo(tmp_path, nav_lists_the_doc=listed)
    proc = _run(repo, "--check")
    assert proc.returncode == expected, (
        f"--check 對 nav_lists_the_doc={listed} 回 rc={proc.returncode}，"
        f"期待 {expected}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_the_dead_update_flag_is_gone(tmp_path):
    """⛔ `--update` 必須被拒絕，而不是被靜默忽略。

    刪除前的實測：帶 `--update` 跑完 rc 0，而 mkdocs.yml 的 sha256 前後相同——它什麼
    也沒做，卻讓呼叫者以為 nav 已經被寫回。⇒ 這一格守的是「不留說謊的介面」，不是
    「永遠不准有 --update」：真要實作寫回，連同這一格一起改。
    """
    repo = _repo(tmp_path, nav_lists_the_doc=True)
    proc = _run(repo, "--update")
    assert proc.returncode == EXIT_CALLER_ERROR, (
        f"--update 應被 argparse 拒絕（rc {EXIT_CALLER_ERROR}），實得 rc={proc.returncode}。"
        f"若它又被加回來，請確認 args.update 真的有被讀取。\nstderr:\n{proc.stderr}"
    )
    assert "--update" in proc.stderr, "argparse 的拒絕訊息應指名這個未知旗標"


def test_check_does_not_fail_on_extra_entries_today(tmp_path):
    """現況存證：nav 列了、檔案不在（`extra`）時 `--check` 仍回 rc 0，只把它印出來。

    ⚠️ 不是在主張「應該如此」。實測（`--repo-root` 合成 repo，nav 列 real.md + ghost.md）：
    rc 0，stdout 有 `In nav but not found: 1`。沒有這一格，第一格的 `missing` 斷言
    即使在「對任何差異都回 rc 1」的實作下也照樣全綠。缺口本身記在 #1884。
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "real.md").write_text(
        '---\ntitle: "Real"\ntags: [dx]\nlang: zh\n---\n# Real\n', encoding="utf-8")
    # real.md 有被列 ⇒ 沒有 missing；ghost.md 被列但檔案不存在 ⇒ 只有 extra
    (tmp_path / "mkdocs.yml").write_text(
        "site_name: t\nnav:\n  - Real: docs/real.md\n  - Ghost: docs/ghost.md\n",
        encoding="utf-8")
    proc = _run(tmp_path, "--check")
    assert proc.returncode == EXIT_OK, (
        f"`extra` 不該觸發 rc 1（現況）。實得 rc={proc.returncode}。若 #1884 已把 extra "
        f"改成違規，請改這一格而不是刪它。\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "ghost.md" in proc.stdout, (
        "rc 0 還不夠：它必須真的看到那筆 extra 並印出來，否則一支完全忽略 nav 的實作也會綠。"
        f"\nstdout:\n{proc.stdout}"
    )


def test_the_check_help_admits_the_gap_it_does_not_cover(tmp_path):
    """⛔ `--check` 的 `--help` 必須自己說出「另一個方向沒被守」。

    上一格釘的是**行為**，這一格釘的是**介面上的說法**。兩者分開是因為它們會各自
    腐爛：`help='CI mode: exit 1 if docs missing from nav'` 這種寫法沒有說謊，但
    讀者拿著「我刪了一份文件、忘了改 nav」這個場景去讀它，學不到自己沒被保護——
    而這正是 #1884 驗收條件 2 要求寫明的那件事。

    ⚠️ 斷言錨在 `#1884` 這個**穩定 token** 而不是句子，措辭可以改。若有人把 `extra`
    改成觸發 rc 1，這個 caveat 就該連同 token 一起刪掉，這一格會紅並把他帶到上一格。

    ⚠️ 切片必須收在下一個選項的標題處：argparse 會把長 help 折行，只找「`#1884`
    有沒有出現在整段 --help」會讓它掛在任何一個選項底下都算過。
    """
    out = _run(tmp_path, "--help").stdout   # argparse 在碰 --repo-root 之前就印完退出
    assert "--check" in out and "--repo-root" in out, (
        f"前置條件：--help 必須同時列出這兩個選項才切得出區塊\n{out}"
    )
    block = out[out.index("  --check"):out.index("  --repo-root")]
    assert "#1884" in block, (
        "`--check` 的 help 沒有指出 `extra` 方向不觸發 rc 1。"
        f"\n--check 區塊:\n{block}"
    )


# ===========================================================================
# 第二層：內容驗證（#1884）
#
# ⛔ rc 只承載「有沒有 missing」。以下各格問的是**報告內容**：哪些路徑被算成
# missing、哪些被算成 extra、front matter 被解析成什麼、tags 被分到哪個 section。
# 一支把這些全算錯、但剛好沒有 missing 的實作，第一層那三格全綠。
# ===========================================================================


def _fm(title: str, tags: str = "[]", lang: str = "zh") -> str:
    """一份最小但合法的 front matter（`---` 必須落在第 0 byte，見下方那一格）。"""
    return f'---\ntitle: "{title}"\ntags: {tags}\nlang: {lang}\n---\n# {title}\n'


def _tree(tmp_path: Path, *, docs: dict, nav: list, trailing_yaml: str = "") -> Path:
    """合成一棵最小 repo root：`docs/` 底下的檔案 + 一份 mkdocs.yml。

    docs          — {相對 repo root 的路徑: 檔案內容}
    nav           — mkdocs.yml `nav:` 區段底下的行（自帶縮排）
    trailing_yaml — 接在 nav 之後的頂層 YAML，用來測「nav 區段在哪裡結束」
    """
    (tmp_path / "docs").mkdir(exist_ok=True)
    for rel, body in docs.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    nav_block = "".join(f"{line}\n" for line in nav)
    (tmp_path / "mkdocs.yml").write_text(
        f"site_name: t\nnav:\n{nav_block}{trailing_yaml}", encoding="utf-8")
    return tmp_path


def _report(repo: Path) -> str:
    """跑一次報告模式（不帶 `--check`）並回傳 stdout。"""
    proc = _run(repo)
    assert proc.returncode == EXIT_OK, (
        f"報告模式不該失敗，rc={proc.returncode}\nstderr:\n{proc.stderr}"
    )
    return proc.stdout


# --- nav 比對結果本身 ------------------------------------------------------


def test_missing_and_extra_are_reported_by_path_not_just_counted(tmp_path):
    """missing 與 extra 各自列出**哪些路徑**，而且同一份報告裡兩邊都要對。

    ⚠️ 只驗「Missing from nav: 1」那個數字買不到內容正確性——一支把 missing 與
    extra 算反的實作，兩個數字都還是 1。所以這一格同時斷言第三件事：**已列在 nav
    裡的那份文件不出現在任何一邊**。
    """
    repo = _tree(
        tmp_path,
        docs={
            "docs/listed.md": _fm("Listed"),
            "docs/orphan.md": _fm("Orphan"),
        },
        nav=["  - Listed: docs/listed.md", "  - Ghost: docs/ghost.md"],
    )
    assert not (repo / "docs" / "ghost.md").exists(), "前置條件：ghost 必須真的不存在"

    out = _report(repo)
    assert "docs/orphan.md" in out, f"檔案在、nav 沒列 ⇒ 應被列為 missing\n{out}"
    assert "In nav but not found: 1" in out and "docs/ghost.md" in out, (
        f"nav 列了、檔案不在 ⇒ 應被列為 extra\n{out}"
    )
    assert "docs/listed.md" not in out, (
        f"兩邊都對上的文件不該出現在任何一張清單裡\n{out}"
    )


def test_excluded_and_english_docs_are_not_reported_missing(tmp_path):
    """`EXCLUDE_PATTERNS` 與 `.en.md` 不進 nav 比對。

    ⚠️ 反空轉：同一棵樹裡放一份**該**被報 missing 的文件。少了它，一支掃描面
    歸零的實作（什麼都不報）也會讓前兩個斷言通過。
    """
    repo = _tree(
        tmp_path,
        docs={
            "docs/internal/notes.md": _fm("Internal"),
            "docs/guide.en.md": _fm("English"),
            "docs/included.md": _fm("Included"),
        },
        nav=["  - Placeholder: docs/placeholder.md"],
    )
    out = _report(repo)
    assert "docs/included.md" in out, f"反空轉：這一份必須被報出來\n{out}"
    assert "docs/internal/notes.md" not in out, f"internal/ 應被排除\n{out}"
    assert "docs/guide.en.md" not in out, f".en.md 應被跳過\n{out}"


def test_a_md_path_below_the_nav_section_does_not_count_as_listed(tmp_path):
    """`nav:` 區段在下一個頂層 key 處結束——之後的 `.md` 不算被列入。

    ⇒ 這一格的合成樹裡，`docs/theme-ref.md` 的路徑**字面上出現在 mkdocs.yml**，
    但它在 `theme:` 底下。若實作只是整檔 grep `.md`，它會被誤判成已列入、於是
    不報 missing；正確行為是報 missing。
    """
    repo = _tree(
        tmp_path,
        docs={"docs/theme-ref.md": _fm("ThemeRef")},
        nav=["  - Home: docs/home.md"],
        trailing_yaml="theme:\n  name: material\n  logo: docs/theme-ref.md\n",
    )
    raw = (repo / "mkdocs.yml").read_text(encoding="utf-8")
    assert "docs/theme-ref.md" in raw, "前置條件：這個路徑必須字面出現在 mkdocs.yml 裡"

    out = _report(repo)
    assert "Missing from nav: 1" in out and "docs/theme-ref.md" in out, (
        f"nav 區段外的 .md 不該被當成已列入\n{out}"
    )


# --- front matter 解析 -----------------------------------------------------


def test_front_matter_must_start_at_the_very_first_byte(tmp_path):
    """開頭多一個空行，整份 front matter 就不被認（正規表達式錨在 `^---`）。

    ⚠️ 這不是缺陷而是現況，值得釘住的理由是**它會靜默降級**：解析失敗不報錯，
    title 直接退回檔名 stem，報告看起來仍然正常。
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


def test_a_doc_without_front_matter_falls_back_to_its_filename(tmp_path):
    """端到端：沒有 front matter 的文件，報告裡的標題是檔名 stem。"""
    repo = _tree(
        tmp_path,
        docs={"docs/no-front-matter.md": "# 只有一個標題\n"},
        nav=[],
    )
    out = _report(repo)
    assert "- no-front-matter: docs/no-front-matter.md" in out, (
        f"標題應退回 stem\n{out}"
    )


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
    """報告裡 `  {heading}:` 底下、**到下一個 section 標題為止**的那一段。

    ⛔ 不可以用 `report[report.index(heading):]`：那會一路延伸到輸出結尾，於是
    「兩個標題都還在、但文件全被塞進後面那個 section」也會通過。實測過那個假報告
    （`快速入門:` 空著、兩份文件都在 `核心架構:` 底下），未加邊界的版本全綠。
    """
    heads = [m.start() for m in re.finditer(r"^  \S+:$", report, re.M)]
    start = report.index(f"  {heading}:")
    after = [h for h in heads if h > start]
    return report[start:after[0]] if after else report[start:]


def test_missing_docs_are_grouped_under_their_section_heading(tmp_path):
    """端到端：missing 的文件依 tags 被列在各自的 section 標題底下。

    ⚠️ 斷言**雙向**：每份文件要在自己的 section 區塊裡，而且**不在**另一個區塊裡。
    只驗前半的話，一支把兩份都歸到同一個 section 的實作照樣綠。
    """
    repo = _tree(
        tmp_path,
        docs={
            "docs/arch.md": _fm("Arch Doc", tags="[architecture]"),
            "docs/start.md": _fm("Start Doc", tags="[getting-started]"),
        },
        nav=[],
    )
    out = _report(repo)
    assert "Missing from nav: 2" in out, f"前置條件：兩份都該是 missing\n{out}"

    arch_block = _section_block(out, "核心架構")
    start_block = _section_block(out, "快速入門")
    assert "- Arch Doc: docs/arch.md" in arch_block, f"arch 應在核心架構底下\n{out}"
    assert "- Start Doc: docs/start.md" in start_block, f"start 應在快速入門底下\n{out}"
    assert "- Start Doc" not in arch_block, f"start 不該出現在核心架構區塊\n{out}"
    assert "- Arch Doc" not in start_block, f"arch 不該出現在快速入門區塊\n{out}"
