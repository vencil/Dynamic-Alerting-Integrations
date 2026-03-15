"""Test data factories and builders for da-tools test suite.

所有 factory helpers 和 PipelineBuilder 集中在此模組，
conftest.py 只保留 sys.path 設定和 pytest fixtures。

Factory functions:
  - ``write_yaml()``: 寫入 YAML 檔案到暫存目錄
  - ``make_receiver()``: 產生 Alertmanager receiver dict
  - ``make_routing_config()``: 產生 routing config dict
  - ``make_tenant_yaml()``: 產生標準 tenant YAML 字串
  - ``make_defaults_yaml()``: 產生 _defaults.yaml 字串
  - ``make_am_receiver()``: 產生 AM 原生格式 receiver dict
  - ``make_am_config()``: 產生完整 AM config dict
  - ``make_override()``: 產生 per-rule routing override dict
  - ``make_enforced_routing()``: 產生 platform enforced routing config dict

Builder:
  - ``PipelineBuilder``: 鏈式建構 scaffold → generate_routes 管線資料
"""
import json
import os
import stat
from unittest.mock import MagicMock

import yaml


# ── Shared helpers ───────────────────────────────────────────────────

def write_yaml(tmpdir, filename, content):
    """Write a YAML file into tmpdir with secure permissions.

    Returns the absolute path to the written file.
    """
    path = os.path.join(tmpdir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


# ── HTTP mock factory ────────────────────────────────────────────────

def mock_http_response(body=None, status=200):
    """建立模擬 HTTP response context manager。

    Args:
        body: response body（dict 自動 JSON 序列化，bytes 直接使用，None 回傳 {}）。
        status: HTTP status code。

    Returns:
        MagicMock — 可作為 urlopen() 的回傳值。
    """
    if body is None:
        raw = b"{}"
    elif isinstance(body, bytes):
        raw = body
    else:
        raw = json.dumps(body).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = raw
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ── Receiver factories ────────────────────────────────────────────────

def make_receiver(rtype="webhook", **overrides):
    """產生 Alertmanager receiver dict。

    Args:
        rtype: receiver 類型（webhook, slack, email, teams, pagerduty）。
        **overrides: 覆蓋或補充預設欄位。

    Returns:
        dict — 可直接用於 routing config 的 receiver 結構。
    """
    defaults = {
        "webhook": {"type": "webhook", "url": "https://hooks.example.com/alert"},
        "slack": {"type": "slack", "api_url": "https://hooks.slack.com/services/T/B/X"},
        "email": {"type": "email", "to": "admin@example.com", "smarthost": "smtp.example.com:587"},
        "teams": {"type": "teams", "webhook_url": "https://outlook.office.com/webhook/test"},
        "pagerduty": {"type": "pagerduty", "service_key": "test-service-key"},
    }
    result = dict(defaults.get(rtype, {"type": rtype}))
    result.update(overrides)
    return result


def make_routing_config(tenant="db-a", rtype="webhook", **receiver_overrides):
    """產生標準 routing config dict（含 receiver）。

    Args:
        tenant: tenant 名稱（目前僅供語意標記，不影響 dict 內容）。
        rtype: receiver 類型。
        **receiver_overrides: 傳遞給 make_receiver() 的額外欄位。

    Returns:
        dict — ``{"receiver": {...}}`` 結構。
    """
    return {"receiver": make_receiver(rtype, **receiver_overrides)}


def make_tenant_yaml(tenant, keys=None, routing=None, severity_dedup=None,
                     metadata=None):
    """產生標準 tenant YAML 字串。

    Args:
        tenant: tenant 名稱（作為 YAML 中 tenants 區塊的 key）。
        keys: 閾值 key-value dict（如 ``{"mysql_connections": "70"}``）。
        routing: _routing 區塊 dict（含 receiver 等）。
        severity_dedup: _severity_dedup 值（"enable" / "disable"）。
        metadata: _metadata dict（如 ``{"owner": "dba-team"}``）。

    Returns:
        str — 可直接寫入 YAML 檔案的字串。
    """
    tenant_data = {}
    if keys:
        tenant_data.update(keys)
    if routing:
        tenant_data["_routing"] = routing
    if severity_dedup is not None:
        tenant_data["_severity_dedup"] = severity_dedup
    if metadata is not None:
        tenant_data["_metadata"] = metadata
    data = {"tenants": {tenant: tenant_data}}
    return yaml.dump(data, default_flow_style=False)


# ── Defaults factory ──────────────────────────────────────────────────

def make_defaults_yaml(defaults=None, state_filters=None):
    """產生 _defaults.yaml 字串。

    Args:
        defaults: 平台預設閾值 dict。預設提供 container_cpu/memory/restarts。
        state_filters: 可選的 state_filters 區塊 dict。

    Returns:
        str — 可直接寫入 _defaults.yaml 的字串。
    """
    if defaults is None:
        defaults = {
            "container_cpu": 80,
            "container_memory": 85,
            "container_restarts": 5,
        }
    data = {"defaults": defaults}
    if state_filters:
        data["state_filters"] = state_filters
    return yaml.dump(data, default_flow_style=False)


# ── AM-native receiver factory ────────────────────────────────────────

def make_am_receiver(name, rtype="webhook", url="https://hooks.example.com/alert",
                     **extra):
    """產生 Alertmanager 原生格式 receiver dict。

    與 make_receiver() 不同：此函式產生 AM 實際使用的結構
    （含 ``name`` + ``webhook_configs`` 等 config key）。

    Args:
        name: receiver 名稱（如 "tenant-db-a"）。
        rtype: receiver 類型。
        url: URL 或 email 地址（依 rtype 填入對應欄位）。
        **extra: 附加欄位（如 ``send_resolved=True``）。

    Returns:
        dict — AM 原生 receiver 結構。
    """
    receiver = {"name": name}
    config_key = {
        "webhook": "webhook_configs",
        "slack": "slack_configs",
        "email": "email_configs",
        "teams": "msteams_configs",
        "pagerduty": "pagerduty_configs",
    }.get(rtype, f"{rtype}_configs")

    if rtype == "webhook":
        receiver[config_key] = [{"url": url, **extra}]
    elif rtype == "slack":
        receiver[config_key] = [{"api_url": url, **extra}]
    elif rtype == "email":
        receiver[config_key] = [{"to": url, **extra}]
    else:
        receiver[config_key] = [{**extra}] if extra else [{"url": url}]
    return receiver


# ── AM config factory ────────────────────────────────────────────────

def make_am_config(routes=None, receivers=None, inhibit_rules=None):
    """建構完整 Alertmanager config dict。

    Args:
        routes: route 清單（插入 route.routes）。預設空清單。
        receivers: receiver 清單。預設含 ``{"name": "default"}``。
        inhibit_rules: inhibit rule 清單。預設空清單。

    Returns:
        dict — 完整 AM config 結構（含 route/receivers/inhibit_rules）。
    """
    return {
        "route": {"receiver": "default", "routes": routes or []},
        "receivers": receivers or [{"name": "default"}],
        "inhibit_rules": inhibit_rules or [],
    }


# ── Override factory ─────────────────────────────────────────────────

def make_override(match_type="alertname", match_value="HighCPU",
                  rtype="webhook", group_by=None, **timing):
    """產生 per-rule routing override dict。

    Args:
        match_type: 匹配類型（"alertname" 或 "metric_group"）。
        match_value: 匹配值（如 "HighCPU"）。
        rtype: override receiver 類型。
        group_by: 可選的 group_by 清單。
        **timing: timing 參數（group_wait, repeat_interval 等）。

    Returns:
        dict — 可放入 routing config overrides 清單的結構。
    """
    override = {match_type: match_value, "receiver": make_receiver(rtype)}
    if group_by is not None:
        override["group_by"] = group_by
    override.update(timing)
    return override


# ── Enforced routing factory ─────────────────────────────────────────

def make_enforced_routing(rtype="webhook", per_tenant=False, **overrides):
    """產生 platform enforced routing config dict。

    Args:
        rtype: enforced receiver 類型。
        per_tenant: 是否在 URL 中插入 ``{{tenant}}`` 佔位符。
        **overrides: 覆蓋 receiver dict 中的額外欄位。

    Returns:
        dict — ``{"receiver": {...}}`` enforced routing 結構。
    """
    receiver = make_receiver(rtype)
    if rtype == "webhook":
        if per_tenant:
            receiver["url"] = "https://noc.example.com/{{tenant}}"
        else:
            receiver["url"] = "https://noc.example.com/alert"
    elif rtype == "slack":
        if per_tenant:
            receiver["api_url"] = "https://hooks.slack.com/services/T/B/{{tenant}}"
    receiver.update(overrides)
    result = {"receiver": receiver}
    return result


# ── Routing dir factory ──────────────────────────────────────────────

# 預設 tenant 清單：db-a (webhook) + db-b (slack)
DEFAULT_ROUTING_TENANTS = [
    ("db-a", "webhook", {"mysql_connections": "70", "mysql_slow_queries": "5"},
     {"group_wait": "30s", "repeat_interval": "4h"}),
    ("db-b", "slack", {"mysql_connections": "100"}, {}),
]


def populate_routing_dir(tmpdir, tenants=None):
    """在暫存目錄中寫入 routing YAML 檔案。

    Args:
        tmpdir: 暫存目錄路徑。
        tenants: list of (name, rtype, keys, timing_kw) tuples。
                 預設為 DEFAULT_ROUTING_TENANTS。

    Returns:
        str — tmpdir（方便鏈式呼叫）。
    """
    if tenants is None:
        tenants = DEFAULT_ROUTING_TENANTS

    for name, rtype, keys, timing in tenants:
        receiver_kw = {}
        if rtype == "slack":
            receiver_kw["channel"] = f"#{name}-alerts"
        routing = {"receiver": make_receiver(rtype, **receiver_kw)}
        routing.update(timing)
        write_yaml(tmpdir, f"{name}.yaml", make_tenant_yaml(
            name, keys=keys, routing=routing, severity_dedup="enable"))
    return tmpdir


# ── PipelineBuilder ───────────────────────────────────────────────────

class PipelineBuilder:
    """Integration test 用的 pipeline 資料建構器。

    鏈式呼叫產生 scaffold → generate_routes 管線所需的測試資料。

    Examples::

        result = (PipelineBuilder(config_dir)
            .with_tenant("db-a", "webhook")
            .with_tenant("db-b", "slack", channel="#alerts")
            .build())

        assert "db-a" in result.routing_configs
    """

    class Result:
        """build() 的回傳結構。"""
        __slots__ = ("routing_configs", "dedup_configs", "config_dir",
                     "extra_configs")

        def __init__(self):
            self.routing_configs = {}
            self.dedup_configs = {}
            self.config_dir = None
            self.extra_configs = {}

    def __init__(self, config_dir):
        self._config_dir = config_dir
        self._tenants = []
        self._dedup = {}
        self._metadata = {}

    def with_tenant(self, name, rtype="webhook", dedup="enable",
                    keys=None, metadata=None, **receiver_kw):
        """加入一個 tenant。"""
        self._tenants.append((name, rtype, keys or {}, receiver_kw))
        self._dedup[name] = dedup
        if metadata:
            self._metadata[name] = metadata
        return self

    def with_dedup(self, tenant, mode):
        """覆蓋特定 tenant 的 dedup 設定。"""
        self._dedup[tenant] = mode
        return self

    def build(self):
        """寫入 YAML 檔案並回傳 Result。"""
        result = self.Result()
        result.config_dir = self._config_dir

        for name, rtype, keys, receiver_kw in self._tenants:
            receiver = make_receiver(rtype, **receiver_kw)
            routing = {"receiver": receiver}
            yaml_str = make_tenant_yaml(
                name,
                keys=keys if keys else {"placeholder_metric": "50"},
                routing=routing,
                severity_dedup=self._dedup.get(name, "enable"),
                metadata=self._metadata.get(name),
            )
            write_yaml(self._config_dir, f"{name}.yaml", yaml_str)

        from generate_alertmanager_routes import load_tenant_configs
        routing_configs, dedup_configs, *extra = load_tenant_configs(
            self._config_dir)
        result.routing_configs = routing_configs
        result.dedup_configs = dedup_configs
        result.extra_configs = extra
        return result
