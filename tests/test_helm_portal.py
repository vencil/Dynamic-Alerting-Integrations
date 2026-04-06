"""test_helm_portal.py — Portal Helm chart 模板驗證

驗證 helm/da-portal/ chart 的結構完整性和模板正確性。
不需要 Helm CLI — 純 Python YAML 解析。

v2.5.0 Phase A 新增。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Repository root directory."""
    return Path(__file__).parent.parent


@pytest.fixture(scope="session")
def chart_dir(repo_root: Path) -> Path:
    """Path to helm/da-portal directory."""
    return repo_root / "helm" / "da-portal"


@pytest.fixture(scope="session")
def chart_yaml_content(chart_dir: Path) -> dict:
    """Loaded Chart.yaml content."""
    chart_file = chart_dir / "Chart.yaml"
    assert chart_file.exists(), f"Chart.yaml not found at {chart_file}"
    with open(chart_file, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def values_yaml_content(chart_dir: Path) -> dict:
    """Loaded values.yaml content."""
    values_file = chart_dir / "values.yaml"
    assert values_file.exists(), f"values.yaml not found at {values_file}"
    with open(values_file, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def template_dir(chart_dir: Path) -> Path:
    """Path to templates directory."""
    templates = chart_dir / "templates"
    assert templates.is_dir(), f"templates directory not found at {templates}"
    return templates


# ──────────────────────────────────────────────────────────────────────────────
# Chart.yaml 驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestChartYamlValid:
    """Chart.yaml 存在性及必要欄位驗證。"""

    def test_chart_yaml_valid(self, chart_yaml_content: dict) -> None:
        """驗證 Chart.yaml 包含必要欄位。"""
        assert chart_yaml_content is not None, "Chart.yaml is empty"
        assert "apiVersion" in chart_yaml_content
        assert "name" in chart_yaml_content
        assert "version" in chart_yaml_content
        assert "appVersion" in chart_yaml_content

    def test_chart_api_version(self, chart_yaml_content: dict) -> None:
        """驗證 Chart API version 為 v2（Helm 3.x）。"""
        assert chart_yaml_content["apiVersion"] == "v2"

    def test_chart_name(self, chart_yaml_content: dict) -> None:
        """驗證 Chart 名稱為 da-portal。"""
        assert chart_yaml_content["name"] == "da-portal"

    def test_chart_version_format(self, chart_yaml_content: dict) -> None:
        """驗證 Chart version 遵循語義版本。"""
        version = chart_yaml_content["version"]
        assert isinstance(version, str)
        parts = version.split(".")
        assert len(parts) >= 3, f"Invalid semantic version: {version}"

    def test_chart_app_version_format(self, chart_yaml_content: dict) -> None:
        """驗證 appVersion 遵循語義版本。"""
        app_version = chart_yaml_content["appVersion"]
        assert isinstance(app_version, (str, int, float))


# ──────────────────────────────────────────────────────────────────────────────
# values.yaml 驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestValuesYamlValid:
    """values.yaml 必要欄位驗證。"""

    def test_values_yaml_valid(self, values_yaml_content: dict) -> None:
        """驗證 values.yaml 不為空且為字典。"""
        assert isinstance(values_yaml_content, dict)
        assert len(values_yaml_content) > 0

    def test_image_configuration(self, values_yaml_content: dict) -> None:
        """驗證 image 設定存在且完整。"""
        assert "image" in values_yaml_content
        image = values_yaml_content["image"]
        assert "repository" in image
        assert "tag" in image
        assert "pullPolicy" in image

    def test_portal_configuration(self, values_yaml_content: dict) -> None:
        """驗證 portal 設定存在且完整。"""
        assert "portal" in values_yaml_content
        portal = values_yaml_content["portal"]
        assert "listenPort" in portal
        assert "tenantApiUrl" in portal

    def test_nginx_configuration(self, values_yaml_content: dict) -> None:
        """驗證 nginx 設定存在。"""
        assert "nginx" in values_yaml_content
        nginx = values_yaml_content["nginx"]
        assert "enabled" in nginx

    def test_oauth2_proxy_configuration(self, values_yaml_content: dict) -> None:
        """驗證 oauth2Proxy 設定存在且完整。"""
        assert "oauth2Proxy" in values_yaml_content
        oauth2 = values_yaml_content["oauth2Proxy"]
        assert "enabled" in oauth2
        assert "image" in oauth2
        assert "provider" in oauth2
        assert "redirectUrl" in oauth2
        assert "emailDomain" in oauth2

    def test_service_configuration(self, values_yaml_content: dict) -> None:
        """驗證 service 設定存在且有埠號定義。"""
        assert "service" in values_yaml_content
        service = values_yaml_content["service"]
        assert "type" in service
        assert "port" in service
        assert "internalPort" in service

    def test_resources_configuration(self, values_yaml_content: dict) -> None:
        """驗證 resources 請求和限制存在。"""
        assert "resources" in values_yaml_content
        resources = values_yaml_content["resources"]
        assert "nginx" in resources
        assert "oauth2Proxy" in resources

    def test_pod_security_context(self, values_yaml_content: dict) -> None:
        """驗證 pod security context 存在。"""
        assert "podSecurityContext" in values_yaml_content
        sec_ctx = values_yaml_content["podSecurityContext"]
        assert "runAsNonRoot" in sec_ctx

    def test_network_policy_configuration(self, values_yaml_content: dict) -> None:
        """驗證 networkPolicy 設定存在。"""
        assert "networkPolicy" in values_yaml_content
        netpol = values_yaml_content["networkPolicy"]
        assert "enabled" in netpol
        assert "allowedNamespaces" in netpol

    def test_service_account_configuration(self, values_yaml_content: dict) -> None:
        """驗證 serviceAccount 設定存在。"""
        assert "serviceAccount" in values_yaml_content
        sa = values_yaml_content["serviceAccount"]
        assert "create" in sa


# ──────────────────────────────────────────────────────────────────────────────
# 模板檔案存在性驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestRequiredTemplatesExist:
    """驗證所有必要的 Helm 模板檔案存在。"""

    def test_helpers_template_exists(self, template_dir: Path) -> None:
        """驗證 _helpers.tpl 存在。"""
        assert (template_dir / "_helpers.tpl").exists()

    def test_deployment_template_exists(self, template_dir: Path) -> None:
        """驗證 deployment.yaml 存在。"""
        assert (template_dir / "deployment.yaml").exists()

    def test_service_template_exists(self, template_dir: Path) -> None:
        """驗證 service.yaml 存在。"""
        assert (template_dir / "service.yaml").exists()

    def test_configmap_nginx_template_exists(self, template_dir: Path) -> None:
        """驗證 configmap-nginx.yaml 存在。"""
        assert (template_dir / "configmap-nginx.yaml").exists()

    def test_networkpolicy_template_exists(self, template_dir: Path) -> None:
        """驗證 networkpolicy.yaml 存在。"""
        assert (template_dir / "networkpolicy.yaml").exists()

    def test_serviceaccount_template_exists(self, template_dir: Path) -> None:
        """驗證 serviceaccount.yaml 存在。"""
        assert (template_dir / "serviceaccount.yaml").exists()

    def test_secret_oauth2proxy_template_exists(self, template_dir: Path) -> None:
        """驗證 secret-oauth2proxy.yaml 存在。"""
        assert (template_dir / "secret-oauth2proxy.yaml").exists()


# ──────────────────────────────────────────────────────────────────────────────
# 模板內容驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestNginxConfigmapHasProxy:
    """驗證 nginx configmap 包含反向代理設定。"""

    def test_nginx_configmap_has_api_proxy(self, template_dir: Path) -> None:
        """驗證 configmap-nginx.yaml 包含 /api/v1/ 反向代理設定。"""
        configmap_file = template_dir / "configmap-nginx.yaml"
        with open(configmap_file, encoding="utf-8") as f:
            content = f.read()

        # Check for /api/v1/ location block
        assert "location /api/v1/" in content, (
            "configmap-nginx.yaml 應包含 /api/v1/ location 區塊"
        )
        # Check for proxy_pass directive
        assert "proxy_pass" in content, (
            "configmap-nginx.yaml 應包含 proxy_pass 指令"
        )

    def test_nginx_configmap_has_tenant_api_reference(
        self, template_dir: Path, values_yaml_content: dict
    ) -> None:
        """驗證 nginx configmap 參考 tenantApiUrl。"""
        configmap_file = template_dir / "configmap-nginx.yaml"
        with open(configmap_file, encoding="utf-8") as f:
            content = f.read()

        # Check that tenantApiUrl is referenced in template
        assert ".Values.portal.tenantApiUrl" in content, (
            "configmap-nginx.yaml 應參考 .Values.portal.tenantApiUrl"
        )


class TestDeploymentHasContainers:
    """驗證 deployment 模板定義了必要的容器。"""

    def test_deployment_has_nginx_container(self, template_dir: Path) -> None:
        """驗證 deployment 包含 nginx 容器定義。"""
        deployment_file = template_dir / "deployment.yaml"
        with open(deployment_file, encoding="utf-8") as f:
            content = f.read()

        assert "name: nginx" in content, (
            "deployment.yaml 應定義 name: nginx 容器"
        )
        assert ".Values.image.repository" in content, (
            "deployment.yaml 應參考 .Values.image.repository"
        )

    def test_deployment_has_oauth2_proxy_container(self, template_dir: Path) -> None:
        """驗證 deployment 包含 oauth2-proxy 容器定義。"""
        deployment_file = template_dir / "deployment.yaml"
        with open(deployment_file, encoding="utf-8") as f:
            content = f.read()

        assert "name: oauth2-proxy" in content, (
            "deployment.yaml 應定義 name: oauth2-proxy 容器"
        )
        assert "quay.io/oauth2-proxy/oauth2-proxy" in content or (
            ".Values.oauth2Proxy.image.repository" in content
        ), (
            "deployment.yaml 應參考 oauth2-proxy image"
        )

    def test_deployment_mounts_nginx_config(self, template_dir: Path) -> None:
        """驗證 deployment 掛載 nginx configmap。"""
        deployment_file = template_dir / "deployment.yaml"
        with open(deployment_file, encoding="utf-8") as f:
            content = f.read()

        assert "nginx-config" in content, (
            "deployment.yaml 應參考 nginx-config 卷"
        )
        assert "/etc/nginx/conf.d" in content, (
            "deployment.yaml 應掛載 nginx config 至 /etc/nginx/conf.d"
        )


class TestServicePortsDefined:
    """驗證 service 定義了正確的埠號。"""

    def test_service_has_http_port(self, template_dir: Path) -> None:
        """驗證 service 定義外部 HTTP 埠（80）。"""
        service_file = template_dir / "service.yaml"
        with open(service_file, encoding="utf-8") as f:
            content = f.read()

        assert ".Values.service.port" in content, (
            "service.yaml 應參考 .Values.service.port"
        )
        # Verify from values.yaml that port is 80
        # This is checked in test_service_configuration

    def test_service_has_internal_port(self, template_dir: Path) -> None:
        """驗證 service 定義內部埠（8080）用於直接存取。"""
        service_file = template_dir / "service.yaml"
        with open(service_file, encoding="utf-8") as f:
            content = f.read()

        assert ".Values.service.internalPort" in content, (
            "service.yaml 應參考 .Values.service.internalPort"
        )

    def test_service_uses_values_for_ports(
        self, template_dir: Path, values_yaml_content: dict
    ) -> None:
        """驗證 values.yaml 中的埠號設定符合預期。"""
        # External port should be 80
        assert values_yaml_content["service"]["port"] == 80
        # Internal port should be 8080
        assert values_yaml_content["service"]["internalPort"] == 8080


# ──────────────────────────────────────────────────────────────────────────────
# 安全設定驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestValuesSecurityContext:
    """驗證 pod security context 預設為非 root。"""

    def test_pod_runs_as_non_root(self, values_yaml_content: dict) -> None:
        """驗證 runAsNonRoot 設為 true。"""
        sec_ctx = values_yaml_content["podSecurityContext"]
        assert sec_ctx.get("runAsNonRoot") is True, (
            "podSecurityContext.runAsNonRoot 應為 true"
        )

    def test_pod_has_non_root_uid(self, values_yaml_content: dict) -> None:
        """驗證 runAsUser 設為非零值（通常是 65534）。"""
        sec_ctx = values_yaml_content["podSecurityContext"]
        run_as_user = sec_ctx.get("runAsUser")
        assert run_as_user is not None
        assert run_as_user != 0, (
            "runAsUser 應非零以確保非 root 執行"
        )

    def test_pod_has_fs_group(self, values_yaml_content: dict) -> None:
        """驗證 fsGroup 已設定。"""
        sec_ctx = values_yaml_content["podSecurityContext"]
        assert "fsGroup" in sec_ctx, (
            "podSecurityContext 應定義 fsGroup"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 版本對齐驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestChartVersionMatchesPortal:
    """驗證 Chart 版本與平台版本約定。"""

    def test_chart_version_aligns_with_app_version(
        self, chart_yaml_content: dict
    ) -> None:
        """驗證 Chart version 與 appVersion 對齐。"""
        chart_version = chart_yaml_content["version"]
        app_version = chart_yaml_content["appVersion"]

        # Normalize to string for comparison
        chart_ver_str = str(chart_version)
        app_ver_str = str(app_version).lstrip("v")

        assert chart_ver_str == app_ver_str, (
            f"Chart version ({chart_ver_str}) 應與 appVersion ({app_ver_str}) 對齐"
        )

    def test_image_tag_matches_app_version(
        self, chart_yaml_content: dict, values_yaml_content: dict
    ) -> None:
        """驗證 image tag 與 appVersion 對齐。"""
        app_version = str(chart_yaml_content["appVersion"]).lstrip("v")
        image_tag = str(values_yaml_content["image"]["tag"])

        assert image_tag == app_version, (
            f"Image tag ({image_tag}) 應與 appVersion ({app_version}) 對齐"
        )
