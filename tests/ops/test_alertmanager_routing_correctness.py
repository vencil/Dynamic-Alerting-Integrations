#!/usr/bin/env python3
"""test_alertmanager_routing_correctness.py — 守住「一則具體告警會被路由到哪個
receiver」這一層的正確性（誤路由 = 漏 page）。

覆蓋缺口背景：在本檔之前，Alertmanager 這一側只有兩種驗證——
  (a) test_alertmanager_amtool_validity.py：amtool check-config 驗 rendered
      config「合法」（parser 收不收），
  (b) try-local smoke：驗 Prometheus 端有 critical 在 firing。
兩者都不回答「這則告警最後會落在哪個 receiver」。route tree 的分支順序 /
continue 語意 / matcher 打錯一個字，config 依然完全合法、smoke 依然全綠，
但 Watchdog 心跳可能被吞、sentinel 可能 page 到人、tenant critical 可能掉進
default 黑洞。本檔用 Alertmanager 自家的路由引擎當 oracle：

    amtool config routes test --config.file=<rendered> \
        --verify.receivers=<expected> label=value ...

amtool 對 --verify.receivers 做的是「依 route tree 順序 resolve 出的 receiver
list」與逗號 split 後的期望值 **完全相等** 比對（多送、漏送、順序錯都非 0）——
所以 continue:true 的雙送語意也驗得到。

兩層防線：
  1. TestLiveRouteTreeRouting — live conf.d + 部署用 base ConfigMap 依 GitOps
     --output-configmap 路徑 render（與 validity 測試、CI validate-config job
     同一條 render 鏈），逐分支驗證實際部署的 route tree。
  2. TestEnforcedRoutingGuardrails — live _defaults.yaml 的 _routing_enforced
     是註解掉的（NOC enforced 分支與 per-rule override 分支在 live tree 上
     踩不到），用 synthetic demo tenants 組出啟用該分支的 config，驗
     continue:true 雙送 / override 優先 / 隔離子樹擋 NOC 洩漏這三個守則。

執行環境：與 validity 測試同一套 amtool 取得策略（docker + 從 deployment
manifest 導出的 prom/alertmanager image；缺 docker 整個 module skip、離線拉
不到 image 也 skip）。CI 上與 validity 的 amtool class 跑在同一個 Python
Tests job（ubuntu runner 自帶 docker）。

維護指引：改 route tree（configmap-alertmanager.yaml 的手寫 base、
_grar_routes.py 的產生邏輯、_grar_render.py 的注入順序）時，下表案例的
「預期 receiver」欄就是要同步改的地方——每個測試 docstring 都標了它對應
哪條路由分支。
"""
import os
import subprocess

import pytest
import yaml

from generate_alertmanager_routes import (
    assemble_configmap,
    generate_inhibit_rules,
    generate_routes,
    load_base_config,
    load_tenant_configs,
)

# 沿用 validity 測試的 amtool 取得策略（同一個 image derivation、同一個
# docker 偵測、同一個 offline-skip）——單一 SOT，兩個 amtool suite 不會 drift。
from test_alertmanager_amtool_validity import (
    _AM_IMAGE,
    _CONF_D,
    _DOCKER,
    _render_live_alertmanager_yml,
    ensure_am_image,
)

# Set to "1" by the CI job that is SUPPOSED to have docker (ci.yml python-tests
# ``env:``). When set, a missing docker is a FAILURE, not a skip — see
# test_docker_present_when_required（對齊 VIBE_REQUIRE_MTAIL pattern）。
_REQUIRE_DOCKER = os.environ.get("VIBE_REQUIRE_DOCKER") == "1"

# 刻意用 per-class mark 而非 module-level pytestmark：presence guard
# （test_docker_present_when_required）必須留在 mark 之外，否則 docker 缺席時
# 它自己也被 skip、fail-closed 訊號永遠發不出來。
_needs_docker = pytest.mark.skipif(
    not _DOCKER,
    reason="docker not available — amtool `config routes test` guard needs the "
           "prom/alertmanager image")


def test_docker_present_when_required() -> None:
    """Fail-closed guard against silent disarmament（對齊 ci.yml python-tests 的
    ``VIBE_REQUIRE_MTAIL`` / test_mtail_present_when_required 前例）。

    本檔與 test_alertmanager_amtool_validity.py 的 amtool 防線都掛在
    ``skipif(not _DOCKER)`` 之下——對本地無 docker 的開發機友善，但在「理應有
    docker」的 CI job 裡（GitHub runner 換 image、docker daemon 壞掉），整層
    路由正確性防線會靜默 skip、CI 照綠且無人察覺。該 CI job 設
    ``VIBE_REQUIRE_DOCKER=1`` 後，docker 缺席在這裡變成紅燈而非 skip。
    未設環境變數時本測試恆綠（不 skip——保持一顆常在的哨兵）。
    """
    if _REQUIRE_DOCKER:
        assert _DOCKER, (
            "VIBE_REQUIRE_DOCKER=1 but docker is not available — this CI job is "
            "supposed to provide docker; without it the ENTIRE Alertmanager "
            "routing-correctness + amtool validity layer silently skips (silent "
            "disarmament). Fix the runner/daemon instead of removing this guard.")

# Live conf.d 的 tenant 集合：動態導出（dev-rule #2 tenant-agnostic——不
# hardcode tenant id），conf.d 增刪租戶時所有 per-tenant 案例自動跟進。
_LIVE_TENANTS = sorted(load_tenant_configs(_CONF_D)[0].keys())

# 保證不存在於 conf.d 的 tenant 名（負向案例用）；模組載入時就驗證前提。
_UNKNOWN_TENANT = "fixture-unrouted-tenant"
assert _UNKNOWN_TENANT not in _LIVE_TENANTS


# ============================================================
# 共用 helpers
# ============================================================

def _amtool_routes_test(etc_dir, labels: dict, expected_receivers: str):
    """對掛載進 container 的 rendered config 跑 amtool config routes test。

    amtool 以 route tree 順序 resolve 出 receiver list，與
    ``expected_receivers``（逗號分隔）做完全相等比對；不相等（含順序不同、
    continue:true 多送或漏送）exit code 非 0。
    """
    argv = [
        "docker", "run", "--rm", "--entrypoint", "amtool",
        "-v", f"{etc_dir.as_posix()}:/etc/alertmanager",
        _AM_IMAGE, "config", "routes", "test",
        "--config.file=/etc/alertmanager/alertmanager.yml",
        f"--verify.receivers={expected_receivers}",
    ] + [f"{k}={v}" for k, v in sorted(labels.items())]
    return subprocess.run(argv, capture_output=True, text=True, timeout=300)


def _assert_routed(etc_dir, labels: dict, expected_receivers: str):
    r = _amtool_routes_test(etc_dir, labels, expected_receivers)
    assert r.returncode == 0, (
        f"alert {labels} did not resolve to [{expected_receivers}]\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")


def _write_etc(tmp_path_factory, name: str, am_yml: str):
    """把 rendered alertmanager.yml 放進一個可掛載的目錄。"""
    etc = tmp_path_factory.mktemp(name)
    (etc / "alertmanager.yml").write_text(am_yml, encoding="utf-8")
    return etc


# ============================================================
# 1. Live route tree（conf.d + 部署 base，--output-configmap 路徑）
# ============================================================

@pytest.fixture(scope="module")
def live_etc(tmp_path_factory):
    """live render 一次（module scope），所有 live 案例共用同一份 config。"""
    assert _LIVE_TENANTS, f"no tenants loaded from {_CONF_D} — fixture premise broken"
    ensure_am_image()
    work = tmp_path_factory.mktemp("am-live-render")
    return _write_etc(tmp_path_factory, "am-live-etc",
                      _render_live_alertmanager_yml(work))


@_needs_docker
class TestLiveRouteTreeRouting:
    """實際部署的 route tree，逐分支驗「告警 → receiver」。

    Rendered tree 的分支面（configmap-alertmanager.yaml base +
    _inject_custom_alert_isolation 注入順序，index 順序 load-bearing）：
      routes[0] alertname="Watchdog"            → watchdog-heartbeat  (continue:false)
      routes[1] component="custom"（含 per-tenant children）
                                                 → tenant-<t> / custom-alerts-firehose
      routes[2] component="synthetic-probe"     → synthetic-receiver  (continue:false)
      routes[3] component="sentinel"            → sentinel-sinkhole   (continue:false)
      routes[4..] tenant="<t>"（conf.d 產生）    → tenant-<t>
      （root fallback）                          → default
    live _defaults.yaml 未啟用 _routing_enforced → enforced NOC 分支見下一個
    class 的 synthetic 覆蓋。
    """

    @pytest.mark.parametrize("severity", ["critical", "warning"])
    @pytest.mark.parametrize("tenant", _LIVE_TENANTS)
    def test_core_alert_routes_to_tenant_receiver(self, live_etc, tenant, severity):
        """分支：tenant 主 route（matchers=[tenant="<t>"]，無 severity 條件）。

        critical / warning 都必須落在同一個 tenant receiver——severity dedup
        是 inhibit_rules 的職責，不是路由分支；若有人把 severity 條件加進
        tenant route matcher，warning 會掉進 default 黑洞，本案例變紅。
        """
        _assert_routed(live_etc, {
            "alertname": "TenantCoreAlert",
            "tenant": tenant,
            "severity": severity,
        }, f"tenant-{tenant}")

    def test_watchdog_routes_to_heartbeat(self, live_etc):
        """分支：routes[0]（ADR-025 D1 / #838 Watchdog liveness）。

        永遠 firing 的 Watchdog（無 tenant / component label）必須送達
        watchdog-heartbeat（外部 dead-man's-switch），不能落 default。
        """
        _assert_routed(live_etc, {"alertname": "Watchdog"}, "watchdog-heartbeat")

    def test_watchdog_priority_immune_to_extra_labels(self, live_etc):
        """分支：routes[0] 的 index-0 優先權。

        即使 Watchdog 帶上 tenant + severity label（被上游 relabel 污染的
        情境），仍必須被 index-0 route 以 continue:false 攔下——只送
        heartbeat、絕不進 tenant / 人類 channel。route 順序若被動到
        （Watchdog 掉出 index 0），本案例變紅。
        """
        _assert_routed(live_etc, {
            "alertname": "Watchdog",
            "tenant": _LIVE_TENANTS[0],
            "severity": "critical",
        }, "watchdog-heartbeat")

    @pytest.mark.parametrize("tenant", _LIVE_TENANTS)
    def test_custom_alert_routes_to_tenant_channel(self, live_etc, tenant):
        """分支：routes[1] custom 隔離子樹的 per-tenant child（#1092 0-pre）。

        page-mode custom alert（component="custom"）進隔離子樹後，由
        tenant="<t>" child 轉到該租戶自己的 channel（tenant-<t> receiver）。
        注意 receiver 名稱與 tenant 主 route 相同——「確實是子樹在接、而非
        fall-through 到主 route」由下一個 unknown-tenant 案例釘住（子樹
        parent 是 continue:false，漏接時 unknown tenant 會落 default 而非
        firehose）。
        """
        _assert_routed(live_etc, {
            "alertname": "Custom_demo_rule",
            "component": "custom",
            "tenant": tenant,
            "severity": "warning",
        }, f"tenant-{tenant}")

    def test_custom_alert_unknown_tenant_falls_back_to_firehose(self, live_etc):
        """分支：routes[1] custom 隔離子樹的 parent fallback（#741 S7/S8）。

        沒有 valid _routing 的租戶（此處以不存在於 conf.d 的 fixture tenant
        模擬）其 custom alert 必須被 parent 接住（custom-alerts-firehose，
        AM-UI 可見、無 notifier），絕不能穿出子樹落到 default 或其他
        receiver——continue:false 的隔離語意。
        """
        _assert_routed(live_etc, {
            "alertname": "Custom_demo_rule",
            "component": "custom",
            "tenant": _UNKNOWN_TENANT,
            "severity": "warning",
        }, "custom-alerts-firehose")

    def test_synthetic_probe_is_sinkholed(self, live_etc):
        """分支：routes[2] synthetic-probe sinkhole（ADR-025 interop 契約）。

        客戶 blackbox probe 的測試告警即使帶 tenant + critical，也必須被
        synthetic-receiver 接住（zero-risk：永不 page 人）。
        """
        _assert_routed(live_etc, {
            "alertname": "SyntheticProbeE2E",
            "component": "synthetic-probe",
            "tenant": _LIVE_TENANTS[0],
            "severity": "critical",
        }, "synthetic-receiver")

    def test_sentinel_is_sinkholed_not_paged(self, live_etc):
        """分支：routes[3] sentinel sinkhole（#1095 洩漏回歸）。

        sentinel（inhibit source / 狀態面，帶 tenant label）必須被
        sentinel-sinkhole 吞掉。#1095 修的正是這個洩漏：沒有此 route 時
        sentinel 會 fall-through 進 tenant 主 route（tenant="<t>" 無
        severity 過濾），對人類 channel 發 severity=none 噪音。
        """
        _assert_routed(live_etc, {
            "alertname": "TenantSilentWarning",
            "component": "sentinel",
            "tenant": _LIVE_TENANTS[0],
            "severity": "none",
        }, "sentinel-sinkhole")

    def test_oracle_rejects_wrong_receiver(self, live_etc):
        """守住守門員：amtool --verify.receivers 是完全相等比對，期望值錯時
        必須非 0——證明本檔的綠燈不是空對空假綠（oracle 真的在比對）。"""
        r = _amtool_routes_test(live_etc, {"alertname": "Watchdog"}, "default")
        assert r.returncode != 0, (
            "amtool accepted a deliberately WRONG expected receiver — the "
            "--verify.receivers oracle is not comparing; every green above "
            f"is suspect.\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")

    def test_unmatched_platform_alert_falls_to_default(self, live_etc):
        """分支：root fallback（負向案例）。

        不帶 tenant / component、alertname 也非 Watchdog 的平台雜項告警，
        不該 match 任何特殊分支——必須落在 root 的 default receiver。若未來
        有人加了一條過寬的 matcher（例如 matcher-less route），本案例變紅。
        """
        _assert_routed(live_etc, {
            "alertname": "PlatformMiscUnrouted",
            "severity": "warning",
        }, "default")


# ============================================================
# 2. Enforced NOC + per-rule override（synthetic；live 未啟用的分支）
# ============================================================

# Synthetic demo tenants（dev-rule #2：非真實 tenant id 的 fixture 名）。
_DEMO_TENANTS = ("demo-blue", "demo-green")
_OVERRIDE_ALERTNAME = "DemoOverrideAlert"


def _build_enforced_am_yml() -> str:
    """組一份啟用 _routing_enforced（platform-wide、match severity=critical、
    continue:true）+ 一條 per-rule override 的 synthetic config。

    形態對齊 _defaults.yaml 註解掉的 _routing_enforced 範例（v1.7.0）；走
    與 live render 同一條 generate_routes → assemble_configmap 組裝鏈（含
    _inject_custom_alert_isolation 的四條 platform-static route 注入），
    差別只在輸入是 synthetic routing_configs 而非 conf.d。
    """
    routing_configs = {
        t: {"receiver": {"type": "webhook",
                         "url": f"https://hooks.example.com/{t}"}}
        for t in _DEMO_TENANTS
    }
    # v1.8.0 per-rule override：第一個 demo tenant 針對特定 alertname 改道。
    routing_configs[_DEMO_TENANTS[0]]["overrides"] = [{
        "alertname": _OVERRIDE_ALERTNAME,
        "receiver": {"type": "webhook",
                     "url": "https://hooks.example.com/override-sink"},
    }]
    enforced = {
        "receiver": {"type": "webhook", "url": "https://noc.example.com/alerts"},
        "match": ['severity="critical"'],
    }
    routes, receivers, warnings = generate_routes(
        routing_configs, enforced_routing=enforced)
    # receiver 建置若被 WARN+skip，下面的路由斷言會驗到殘缺樹 → 前提先擋。
    blocking = [w for w in warnings if "skipping" in w or "blocked" in w]
    assert not blocking, f"synthetic config unexpectedly degraded: {blocking}"
    inhibit_rules, _ = generate_inhibit_rules(
        {t: "enable" for t in _DEMO_TENANTS})
    cm_yaml = assemble_configmap(
        load_base_config(None), routes, receivers, inhibit_rules)
    return yaml.safe_load(cm_yaml)["data"]["alertmanager.yml"]


@pytest.fixture(scope="module")
def enforced_etc(tmp_path_factory):
    ensure_am_image()
    return _write_etc(tmp_path_factory, "am-enforced-etc",
                      _build_enforced_am_yml())


@_needs_docker
class TestEnforcedRoutingGuardrails:
    """Routing Guardrails：enforced NOC（continue:true）與 override 分支。

    Synthetic tree（_inject_custom_alert_isolation 注入後）：
      routes[0..3]  Watchdog / custom（含 demo children）/ probe / sentinel
      routes[4]     platform-enforced（severity="critical"，continue:true）
      routes[5]     tenant-demo-blue-override-0（alertname 專屬 override）
      routes[6..]   tenant="demo-*" 主 route
    """

    def test_critical_dual_delivers_to_noc_and_tenant(self, enforced_etc):
        """分支：enforced route（continue:true）+ tenant 主 route 的雙送。

        critical 必須「NOC 與租戶都收到」且順序為 enforced 先——amtool 的
        exact-match 驗證這裡若 continue:true 被拿掉（只剩其一）即變紅。
        """
        tenant = _DEMO_TENANTS[0]
        _assert_routed(enforced_etc, {
            "alertname": "DemoCoreAlert",
            "tenant": tenant,
            "severity": "critical",
        }, f"platform-enforced,tenant-{tenant}")

    def test_warning_skips_noc_reaches_tenant_only(self, enforced_etc):
        """分支：enforced route 的 severity="critical" matcher 過濾。

        warning 不進 NOC，只送租戶 channel——enforced matcher 若被放寬
        （吃掉所有 severity），本案例因多送 platform-enforced 而變紅。
        """
        tenant = _DEMO_TENANTS[1]
        _assert_routed(enforced_etc, {
            "alertname": "DemoCoreAlert",
            "tenant": tenant,
            "severity": "warning",
        }, f"tenant-{tenant}")

    def test_critical_unknown_tenant_still_reaches_noc(self, enforced_etc):
        """分支：enforced route 對「無租戶歸屬的 critical」是唯一接手者。

        critical 但 tenant 不在任何主 route（孤兒告警）→ 只有 NOC 收到；
        這是 enforced routing 的守門價值（沒有它就掉 default 黑洞）。
        """
        _assert_routed(enforced_etc, {
            "alertname": "DemoCoreAlert",
            "tenant": _UNKNOWN_TENANT,
            "severity": "critical",
        }, "platform-enforced")

    def test_override_route_wins_over_tenant_main_route(self, enforced_etc):
        """分支：per-rule override sub-route（v1.8.0，插在 tenant 主 route 前）。

        override 指定的 alertname 必須改道到 override receiver，而非租戶
        主 channel；順序若倒轉（主 route 先 match），本案例變紅。
        """
        tenant = _DEMO_TENANTS[0]
        _assert_routed(enforced_etc, {
            "alertname": _OVERRIDE_ALERTNAME,
            "tenant": tenant,
            "severity": "warning",
        }, f"tenant-{tenant}-override-0")

    def test_custom_alert_never_leaks_to_enforced_noc(self, enforced_etc):
        """分支：custom 隔離子樹（continue:false）擋在 enforced route 之前。

        這是 _inject_custom_alert_isolation 順序 load-bearing 的核心理由
        （#741 S7/S8）：critical 的 tenant custom alert 只送租戶 channel，
        絕不能雙送進 platform NOC——否則租戶 custom 告警風暴會淹掉 NOC。
        """
        tenant = _DEMO_TENANTS[0]
        _assert_routed(enforced_etc, {
            "alertname": "Custom_demo_rule",
            "component": "custom",
            "tenant": tenant,
            "severity": "critical",
        }, f"tenant-{tenant}")

    def test_watchdog_heartbeat_never_dual_delivers_to_noc(self, enforced_etc):
        """分支：Watchdog index-0 優先權 vs enforced match-critical。

        即使 Watchdog 被標成 critical，也只送 heartbeat（continue:false 在
        enforced 之前）——心跳進 NOC 會變成永遠 firing 的噪音、順序倒轉則
        可能吞掉心跳（外部 DMS 誤報平台死亡）。
        """
        _assert_routed(enforced_etc, {
            "alertname": "Watchdog",
            "severity": "critical",
        }, "watchdog-heartbeat")
