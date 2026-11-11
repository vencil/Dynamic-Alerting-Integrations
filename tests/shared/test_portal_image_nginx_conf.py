"""test_portal_image_nginx_conf.py — da-portal image 的 nginx.conf

驗證 components/da-portal/nginx.conf 是純靜態設定（不轉送任何請求、不碰身分
header；ADR-027 §5、§9 第 3 項），以及安全標頭、健康檢查、SPA fallback 與快取。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Repository root directory."""
    return Path(__file__).parent.parent.parent


@pytest.fixture(scope="session")
def nginx_conf_path(repo_root: Path) -> Path:
    """Path to nginx.conf file."""
    return repo_root / "components" / "da-portal" / "nginx.conf"


@pytest.fixture(scope="session")
def nginx_conf_content(nginx_conf_path: Path) -> str:
    """Loaded nginx.conf content."""
    assert nginx_conf_path.exists(), f"nginx.conf not found at {nginx_conf_path}"
    with open(nginx_conf_path, encoding="utf-8") as f:
        return f.read()


# ──────────────────────────────────────────────────────────────────────────────
# 檔案存在性驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestNginxConfExists:
    """驗證 nginx.conf 檔案存在。"""

    def test_nginx_conf_file_exists(self, nginx_conf_path: Path) -> None:
        """驗證 nginx.conf 檔案存在於預期路徑。"""
        assert nginx_conf_path.exists(), (
            f"nginx.conf 應存在於 {nginx_conf_path}"
        )
        assert nginx_conf_path.is_file(), (
            f"{nginx_conf_path} 應為檔案"
        )

    def test_nginx_conf_not_empty(self, nginx_conf_content: str) -> None:
        """驗證 nginx.conf 不為空。"""
        assert len(nginx_conf_content) > 0, (
            "nginx.conf 應包含設定內容"
        )

    def test_nginx_conf_is_valid_format(self, nginx_conf_content: str) -> None:
        """驗證 nginx.conf 包含基本的 nginx 指令語法。"""
        # Check for server block
        assert "server {" in nginx_conf_content or "server{" in nginx_conf_content, (
            "nginx.conf 應包含 server 區塊"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 不轉送任何東西（ADR-027 §5、§9 第 3 項）
# ──────────────────────────────────────────────────────────────────────────────

class TestNoForwarding:
    """image 的 nginx.conf 是純靜態設定：沒有 proxy_pass、不碰身分 header。

    登入後的 portal 在 tenant-api 的 pod 裡、用 chart 自帶的設定；da-portal
    chart 也掛自己的 conf.d 蓋過它。這份檔案是 `docker run` 用到的，它轉送
    任何請求都會讓「身分只經 oauth2-proxy → UDS 一跳」不成立。
    """

    def test_no_proxy_directive(self, nginx_conf_content: str) -> None:
        # 去掉註解再比，避免說明文字裡的字樣誤判。
        code = "\n".join(l.split("#", 1)[0] for l in nginx_conf_content.splitlines())
        assert "server {" in code  # 讀到的是設定本身
        for directive in ("proxy_pass", "fastcgi_pass", "uwsgi_pass", "grpc_pass",
                          "scgi_pass", "proxy_set_header", "js_import", "js_set"):
            assert directive not in code, directive

    def test_no_identity_header(self, nginx_conf_content: str) -> None:
        code = "\n".join(l.split("#", 1)[0] for l in nginx_conf_content.splitlines())
        assert "x_forwarded_" not in code.lower()
        assert "x-forwarded-" not in code.lower()


# ──────────────────────────────────────────────────────────────────────────────
# 安全標頭驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestSecurityHeaders:
    """驗證安全相關的 HTTP 標頭。"""

    def test_x_frame_options_header(self, nginx_conf_content: str) -> None:
        """驗證 X-Frame-Options 標頭存在。"""
        assert re.search(
            r"add_header\s+X-Frame-Options",
            nginx_conf_content,
            re.IGNORECASE
        ), (
            "nginx.conf 應設定 X-Frame-Options 標頭"
        )

    def test_x_content_type_options_header(self, nginx_conf_content: str) -> None:
        """驗證 X-Content-Type-Options 標頭存在。"""
        assert re.search(
            r"add_header\s+X-Content-Type-Options",
            nginx_conf_content,
            re.IGNORECASE
        ), (
            "nginx.conf 應設定 X-Content-Type-Options 標頭"
        )

    def test_content_security_policy_header(self, nginx_conf_content: str) -> None:
        """驗證 Content-Security-Policy 標頭存在。"""
        assert re.search(
            r"add_header\s+(?:Content-Security-Policy|X-Content-Security-Policy)",
            nginx_conf_content,
            re.IGNORECASE
        ), (
            "nginx.conf 應設定 Content-Security-Policy 標頭"
        )

    def test_hsts_header(self, nginx_conf_content: str) -> None:
        """驗證 HSTS（Strict-Transport-Security）標頭存在。"""
        assert re.search(
            r"add_header\s+Strict-Transport-Security",
            nginx_conf_content,
            re.IGNORECASE
        ), (
            "nginx.conf 應設定 Strict-Transport-Security 標頭"
        )

    def test_referrer_policy_header(self, nginx_conf_content: str) -> None:
        """驗證 Referrer-Policy 標頭存在。"""
        assert re.search(
            r"add_header\s+Referrer-Policy",
            nginx_conf_content,
            re.IGNORECASE
        ), (
            "nginx.conf 應設定 Referrer-Policy 標頭"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 健康檢查端點驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestHealthzEndpoint:
    """驗證 /healthz 健康檢查端點。"""

    def test_healthz_location_block(self, nginx_conf_content: str) -> None:
        """驗證 /healthz location 區塊存在。"""
        assert re.search(
            r"location\s*(?:=\s*)?/healthz",
            nginx_conf_content,
            re.IGNORECASE
        ), (
            "nginx.conf 應定義 /healthz location 區塊"
        )

    def test_healthz_returns_200(self, nginx_conf_content: str) -> None:
        """驗證 /healthz 返回 HTTP 200。"""
        match = re.search(
            r"location\s*(?:=\s*)?/healthz\s*\{([^}]+)\}",
            nginx_conf_content,
            re.DOTALL
        )
        assert match is not None, (
            "無法解析 /healthz location 區塊"
        )

        healthz_block = match.group(1)
        assert re.search(r"return\s+200", healthz_block), (
            "/healthz 應返回 200 狀態碼"
        )

    def test_healthz_access_log_disabled(self, nginx_conf_content: str) -> None:
        """驗證 /healthz 禁用存取日誌。"""
        match = re.search(
            r"location\s*(?:=\s*)?/healthz\s*\{([^}]+)\}",
            nginx_conf_content,
            re.DOTALL
        )
        assert match is not None

        healthz_block = match.group(1)
        assert "access_log off" in healthz_block or "access_log /dev/null" in healthz_block, (
            "/healthz 應禁用 access_log"
        )


# ──────────────────────────────────────────────────────────────────────────────
# SPA 後備路由驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestSpaFallback:
    """驗證單頁應用（SPA）後備路由。"""

    def test_try_files_fallback_exists(self, nginx_conf_content: str) -> None:
        """驗證 try_files 指令存在用於 SPA 路由。"""
        assert "try_files" in nginx_conf_content, (
            "nginx.conf 應包含 try_files 指令以支援 SPA 路由"
        )

    def test_try_files_fallback_to_index(self, nginx_conf_content: str) -> None:
        """驗證 try_files 後備至 index.html。"""
        assert re.search(
            r"try_files\s+[^;]*index\.html",
            nginx_conf_content
        ), (
            "try_files 應後備至 index.html 以支援 SPA 路由"
        )

    def test_root_directory_configured(self, nginx_conf_content: str) -> None:
        """驗證根目錄配置為靜態檔案位置。"""
        assert "root" in nginx_conf_content or "server_name" in nginx_conf_content, (
            "nginx.conf 應設定 root 目錄或 server_name"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 靜態資源快取驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestStaticCaching:
    """驗證靜態資產的快取規則。"""

    def test_static_asset_location_exists(self, nginx_conf_content: str) -> None:
        """驗證靜態資產 location 區塊存在。"""
        # Look for location block that handles static files
        assert re.search(
            r"location\s*~\*",
            nginx_conf_content
        ), (
            "nginx.conf 應有 location ~* 區塊用於靜態資產"
        )

    def test_js_css_assets_cached(self, nginx_conf_content: str) -> None:
        """驗證 JavaScript 和 CSS 檔案被快取。"""
        assert re.search(
            r"js\|css",
            nginx_conf_content
        ), (
            "nginx.conf 應為 .js 和 .css 檔案設定快取"
        )

    def test_image_assets_cached(self, nginx_conf_content: str) -> None:
        """驗證圖像檔案被快取。"""
        assert re.search(
            r"png\|svg\|ico",
            nginx_conf_content
        ), (
            "nginx.conf 應為圖像檔案設定快取"
        )

    def test_expires_directive_configured(self, nginx_conf_content: str) -> None:
        """驗證 expires 指令被配置。"""
        assert re.search(
            r"expires\s+[\d\w]+",
            nginx_conf_content
        ), (
            "nginx.conf 應設定 expires 指令用於快取控制"
        )

    def test_cache_control_header_set(self, nginx_conf_content: str) -> None:
        """驗證 Cache-Control 標頭被設定。"""
        assert re.search(
            r"Cache-Control",
            nginx_conf_content,
            re.IGNORECASE
        ), (
            "nginx.conf 應設定 Cache-Control 標頭"
        )


# ──────────────────────────────────────────────────────────────────────────────
# JSON/YAML 檔案快取驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestJsonNoCachePolicy:
    """驗證 JSON/YAML 檔案不被快取。"""

    def test_json_yaml_location_exists(self, nginx_conf_content: str) -> None:
        """驗證 JSON/YAML 檔案 location 區塊存在。"""
        assert re.search(
            r"json\|yaml",
            nginx_conf_content,
            re.IGNORECASE
        ), (
            "nginx.conf 應處理 .json 或 .yaml 檔案"
        )

    def test_json_no_cache_directive(self, nginx_conf_content: str) -> None:
        """驗證 JSON 檔案被標記為 no-cache。"""
        # Look for cache control on JSON/YAML files
        match = re.search(
            r"location\s*~\*\s*\\\.(?:json|yaml)",
            nginx_conf_content,
            re.IGNORECASE
        )

        if match or re.search(r"\.json.*no-cache", nginx_conf_content, re.IGNORECASE):
            # If there's explicit JSON/YAML handling, check for no-cache
            assert re.search(
                r"no-cache|max-age=0|expires\s*-1",
                nginx_conf_content,
                re.IGNORECASE
            ), (
                "JSON/YAML 檔案應設為 no-cache 或 max-age=0"
            )
        else:
            # At least no-cache should be mentioned somewhere for dynamic content
            assert re.search(
                r"no-cache",
                nginx_conf_content,
                re.IGNORECASE
            ), (
                "nginx.conf 應包含 no-cache 指令用於動態內容"
            )

    def test_immutable_cache_for_assets(self, nginx_conf_content: str) -> None:
        """驗證版本化資產設為 immutable。"""
        # Check if immutable cache is mentioned for versioned assets
        if "immutable" in nginx_conf_content:
            # If immutable is used, verify it's in a static assets context
            assert re.search(
                r"immutable.*(?:js|css|png|svg|woff)",
                nginx_conf_content,
                re.IGNORECASE
            ) or re.search(
                r"(?:js|css|png|svg|woff).*immutable",
                nginx_conf_content,
                re.IGNORECASE | re.DOTALL
            ), (
                "immutable 快取應用於靜態資產"
            )
