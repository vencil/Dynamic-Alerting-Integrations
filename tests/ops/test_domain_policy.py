#!/usr/bin/env python3
"""test_domain_policy.py — Webhook domain allowlist policy 測試。

驗證 validate_receiver_domains() 的 fnmatch 邊界案例：
  - 精確匹配、wildcard、subdomain
  - 多模式 OR 邏輯
  - 空 allowlist = 不限制
  - 非 webhook 類型忽略
  - generate_routes 整合 policy
"""

import os
import tempfile

import pytest
import yaml

from factories import make_receiver, make_routing_config, write_yaml
from _lib_exitcodes import EXIT_CALLER_ERROR

from generate_alertmanager_routes import (
    validate_receiver_domains,
    load_policy,
    generate_routes,
    PolicyInputError,
)


# ── validate_receiver_domains unit tests ─────────────────────


class TestValidateReceiverDomains:
    """fnmatch domain allowlist 邊界測試。"""

    def test_exact_match_allowed(self):
        """精確匹配 domain 通過。"""
        receiver = {"type": "webhook", "url": "https://hooks.example.com/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", ["hooks.example.com"])
        assert warnings == []

    def test_exact_match_blocked(self):
        """不在 allowlist 的 domain 被阻擋。"""
        receiver = {"type": "webhook", "url": "https://evil.example.com/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", ["hooks.example.com"])
        assert len(warnings) == 1
        assert "not in allowed_domains" in warnings[0]

    def test_wildcard_subdomain(self):
        """*.example.com 匹配所有子網域。"""
        receiver = {"type": "webhook", "url": "https://any.example.com/hook"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert warnings == []

    def test_wildcard_blocks_different_tld(self):
        """*.example.com 不匹配 example.org。"""
        receiver = {"type": "webhook", "url": "https://hook.example.org/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert len(warnings) == 1

    def test_multiple_patterns_or_logic(self):
        """多個 pattern 任一匹配即通過。"""
        receiver = {"type": "webhook", "url": "https://slack.com/hook"}
        warnings = validate_receiver_domains(
            receiver, "db-a", ["hooks.example.com", "slack.com"])
        assert warnings == []

    def test_empty_allowlist_no_restriction(self):
        """空 allowlist = 不限制。"""
        receiver = {"type": "webhook", "url": "https://anything.evil.com/hook"}
        warnings = validate_receiver_domains(receiver, "db-a", [])
        assert warnings == []

    def test_none_allowlist_no_restriction(self):
        """None allowlist = 不限制。"""
        receiver = {"type": "webhook", "url": "https://anything.com/hook"}
        warnings = validate_receiver_domains(receiver, "db-a", None)
        assert warnings == []

    def test_non_webhook_type_ignored(self):
        """非 webhook（如 pagerduty）不檢查 domain。"""
        receiver = {"type": "pagerduty", "service_key": "abc123"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert warnings == []

    def test_slack_api_url_checked(self):
        """Slack api_url 也進行 domain 檢查。"""
        receiver = {"type": "slack", "api_url": "https://hooks.slack.com/services/T/B/X"}
        warnings = validate_receiver_domains(
            receiver, "db-a", ["hooks.slack.com"])
        assert warnings == []

    def test_slack_api_url_blocked(self):
        """Slack api_url 被阻擋。"""
        receiver = {"type": "slack", "api_url": "https://evil.slack.com/services/T/B/X"}
        warnings = validate_receiver_domains(
            receiver, "db-a", ["hooks.slack.com"])
        assert len(warnings) == 1

    def test_email_smarthost_checked(self):
        """Email 的 smarthost 也進行 domain 檢查。"""
        receiver = {"type": "email", "to": "dba@example.com", "smarthost": "smtp.example.com:587"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.allowed.com"])
        assert len(warnings) >= 1
        assert "smtp.example.com" in warnings[0]

    def test_email_smarthost_allowed(self):
        """Email smarthost 在 allowlist 中通過。"""
        receiver = {"type": "email", "to": "dba@example.com", "smarthost": "smtp.example.com:587"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert warnings == []

    def test_non_dict_receiver_returns_empty(self):
        """非 dict receiver 回傳空 warnings。"""
        warnings = validate_receiver_domains("not-a-dict", "db-a", ["*.example.com"])
        assert warnings == []

    def test_url_with_port(self):
        """URL 含 port 的 domain 解析。"""
        receiver = {"type": "webhook", "url": "https://hooks.example.com:8443/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", ["hooks.example.com"])
        assert warnings == []

    @pytest.mark.parametrize("pattern,host,expected_pass", [
        ("*", "any.domain.com", True),
        ("*.internal.corp", "alerts.internal.corp", True),
        ("*.internal.corp", "external.com", False),
        ("hooks.example.com", "hooks.example.com", True),
        ("hooks.example.com", "HOOKS.EXAMPLE.COM", True),  # fnmatch 在 Linux 不區分大小寫（視 OS）
    ], ids=["wildcard-all", "subdomain-match", "subdomain-no-match",
            "exact-match", "case-insensitive"])
    def test_fnmatch_patterns(self, pattern, host, expected_pass):
        """fnmatch pattern 邊界驗證。"""
        receiver = {"type": "webhook", "url": f"https://{host}/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", [pattern])
        if expected_pass:
            assert warnings == [], f"預期通過但被阻擋: {host} vs {pattern}"
        else:
            assert len(warnings) >= 1, f"預期阻擋但通過: {host} vs {pattern}"


# ── load_policy ──────────────────────────────────────────────


class TestLoadPolicy:
    """load_policy() YAML 載入測試。"""

    def test_valid_policy_file(self, config_dir):
        """合法 policy YAML 載入 allowed_domains。"""
        policy = {"allowed_domains": ["*.example.com", "hooks.slack.com"]}
        path = write_yaml(config_dir, "policy.yaml", yaml.dump(policy))
        domains = load_policy(path)
        assert domains == ["*.example.com", "hooks.slack.com"]

    @pytest.mark.parametrize("body", [
        "",                                   # 0 位元組
        "\n  \n",                             # 只有空白
        "# allowed_domains: [example.com]\n",  # 只有註解
    ], ids=["empty", "whitespace", "comment-only"])
    def test_a_file_with_no_document_raises(self, config_dir, body):
        """#1649：檔可讀可解析，但沒說任何 `allowed_domains` → caller error。

        ⛔ 這格原本叫 `test_empty_policy_file`、斷言 `== []`，把「判不出來」
        釘成「不設限」——webhook 網域白名單整條關掉、rc 0、`[PASS] policy`。
        """
        path = write_yaml(config_dir, "policy.yaml", body)
        with pytest.raises(PolicyInputError, match="no YAML document"):
            load_policy(path)

    def test_nonexistent_policy_raises(self):
        """供了 --policy 但那不是檔案 → 必須拋，不可回空 list。

        ⛔ 這條原本斷言 `== []`，docstring 寫「不存在的 policy 檔案回傳空
        list」——只複述行為、沒有理由，於是把 #1556 的缺陷釘成契約：客戶照
        文件傳 `--policy "webhook.company.com,slack.com"`（域名清單、不是
        路徑）⇒ 空 allowlist ⇒ webhook 網域白名單整條沒跑，而輸出是
        `[PASS] policy`、rc=0。
        """
        with pytest.raises(PolicyInputError):
            load_policy("/nonexistent/policy.yaml")

    def test_comma_separated_domains_raise(self):
        """文件教的那個逐字值必須被擋（#1556 的原始重現）。"""
        with pytest.raises(PolicyInputError):
            load_policy("webhook.company.com,slack.com")

    def test_none_path(self):
        """沒有供 --policy 回傳空 list —— 「不要求限制」與「判不出來」不同。"""
        assert load_policy(None) == []

    def test_policy_without_allowed_domains_key_raises(self, config_dir):
        """#1649：缺 `allowed_domains` 鍵 → caller error（原本斷言 `== []`）。

        錯誤訊息要說怎麼明示「不設限」，否則最便宜的轉綠是拿掉旗標。
        """
        path = write_yaml(config_dir, "policy.yaml",
                          "denied_functions: [holt_winters]\n")
        with pytest.raises(PolicyInputError,
                           match="no `allowed_domains` key") as exc:
            load_policy(path)
        assert "allowed_domains: []" in str(exc.value)

    def test_a_misspelled_key_is_named(self, config_dir):
        """`allowed_domain:`（少一個 s）→ raise，且訊息點名拼錯的那個鍵。"""
        path = write_yaml(config_dir, "policy.yaml",
                          "allowed_domain: [example.com]\n")
        with pytest.raises(PolicyInputError, match="found `allowed_domain`"):
            load_policy(path)

    def test_a_block_list_key_is_not_offered_as_the_fix(self, config_dir):
        """`disallowed_domains` 與 `allowed_domains` 字面很近、語意相反：
        照「did you mean」改名會把封鎖清單變成允許清單。仍要 raise，但不提示。"""
        path = write_yaml(config_dir, "policy.yaml",
                          "disallowed_domains: [evil.test]\n")
        with pytest.raises(PolicyInputError) as exc:
            load_policy(path)
        assert "did you mean" not in str(exc.value)

    def test_the_message_is_one_line(self, config_dir):
        """validate-config 會把控制字元印成 `?`，訊息必須是單行。"""
        path = write_yaml(config_dir, "policy.yaml", "")
        with pytest.raises(PolicyInputError) as exc:
            load_policy(path)
        assert "\n" not in str(exc.value)

    def test_non_string_entries_raise(self, config_dir):
        """全是非字串的清單原本被濾成 `[]` ＝ 不設限（#1649）。"""
        path = write_yaml(config_dir, "policy.yaml", "allowed_domains: [123]\n")
        with pytest.raises(PolicyInputError, match="non-string"):
            load_policy(path)

    def test_an_explicit_empty_list_is_no_restriction(self, config_dir):
        """對照組：鍵在、值是 `[]` → 仍是「不設限」，不得被上面幾格一起擋掉。"""
        path = write_yaml(config_dir, "policy.yaml", "allowed_domains: []\n")
        assert load_policy(path) == []

    def test_allowed_domains_with_an_empty_value_is_no_restriction(
            self, config_dir):
        """`allowed_domains:`（key 在、值為空）≡ `allowed_domains: []` ≡ 不設限。

        ⛔ 這條釘的是**我在這支 PR 裡造成、又在下一顆 commit 修掉的回歸**。
        收窄 `load_policy` 時我讓空值走 raise，於是本 repo 自己的
        `.github/custom-rule-policy.yaml` 只要把域名條目註解掉就 rc=2——而最
        便宜的轉綠是連 key 一起刪，正好回到 #1556 要消滅的靜默關閉狀態。該檔
        自己就寫著「空清單 = 不限制（向後相容）」。

        ⛔ 盲審實測：拿掉 `_grar_validate.py` 那兩行 `if domains is None`，
        464 個測試全綠——一條已經發生過一次的回歸，唯一防線是散文註解。
        """
        path = write_yaml(config_dir, "policy.yaml", "allowed_domains:\n")
        assert load_policy(path) == []

    def test_allowed_domains_of_the_wrong_type_still_raises(self, config_dir):
        """對照組：放寬「空值」不得把「型別真的錯」一起放過。

        沒有這一格，上面那條會被「`allowed_domains` 一律回 `[]`」滿足——而那
        正是 #1556 的缺陷形狀本身。
        """
        for bad in ("allowed_domains: ''\n", "allowed_domains: {}\n"):
            path = write_yaml(config_dir, "policy_bad.yaml", bad)
            with pytest.raises(PolicyInputError):
                load_policy(path)


class TestPolicyContentAxisExitCodes:
    """#1649 的 CLI 面：required check 走的 `generate-routes --validate
    --strict --policy`。每一格都與兩個必響對照組一起跑：有效 policy 必須點名
    evil 網域並回 1；明示的 `[]` 必須仍回 0。"""

    _TENANT = ("tenants:\n  t1:\n    _routing:\n      receiver:\n"
               "        type: webhook\n"
               "        url: \"https://evil.attacker.test/hook\"\n")

    def _run(self, config_dir, policy_body, capsys, cli_argv):
        from generate_alertmanager_routes import main as gen_main
        confd = os.path.join(config_dir, "conf.d")
        os.makedirs(confd)
        write_yaml(confd, "t1.yaml", self._TENANT)
        policy = write_yaml(config_dir, "policy.yaml", policy_body)
        cli_argv("generate_alertmanager_routes", "--config-dir", confd,
                 "--validate", "--strict", "--policy", policy)
        try:
            rc = gen_main()
        except SystemExit as exc:
            rc = exc.code
        captured = capsys.readouterr()
        return rc, captured.out + captured.err

    @pytest.mark.parametrize("body", [
        "", "# allowed_domains: [example.com]\n",
        "allowed_domain: [example.com]\n", "allowed_domains: [123]\n",
    ], ids=["empty", "comment-only", "misspelled-key", "non-string"])
    def test_content_that_says_nothing_is_a_caller_error(
            self, config_dir, body, capsys, cli_argv):
        rc, _ = self._run(config_dir, body, capsys, cli_argv)
        assert rc == EXIT_CALLER_ERROR

    def test_control_a_real_policy_names_the_domain(self, config_dir, capsys,
                                                    cli_argv):
        rc, out = self._run(config_dir, "allowed_domains: [example.com]\n",
                            capsys, cli_argv)
        assert rc == 1
        assert "evil.attacker.test" in out

    @pytest.mark.parametrize("body", ["allowed_domains: []\n",
                                      "allowed_domains:\n"],
                             ids=["empty-list", "empty-value"])
    def test_control_an_explicit_no_constraint_still_passes(
            self, config_dir, body, capsys, cli_argv):
        rc, _ = self._run(config_dir, body, capsys, cli_argv)
        assert rc == 0


# ── generate_routes + policy integration ─────────────────────


class TestGenerateRoutesWithPolicy:
    """generate_routes() 整合 domain policy。"""

    def test_blocked_receiver_produces_warning(self):
        """被 policy 阻擋的 receiver 產生 warning。"""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://evil.com/hook"},
            }
        }
        _, _, warnings = generate_routes(
            routing_configs, allowed_domains=["*.example.com"], tenants=())
        domain_warns = [w for w in warnings if "not in allowed_domains" in w]
        assert len(domain_warns) >= 1

    def test_allowed_receiver_no_warning(self):
        """符合 policy 的 receiver 不產生 domain warning。"""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://hooks.example.com/alert"},
            }
        }
        _, _, warnings = generate_routes(
            routing_configs, allowed_domains=["*.example.com"], tenants=())
        domain_warns = [w for w in warnings if "not in allowed_domains" in w]
        assert domain_warns == []
