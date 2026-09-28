---
title: "工具鏈版本對照 — 本地 / CI 同版的 SSOT 與守衛"
tags: [internal, ci, dx, toolchain]
audience: [contributors, maintainers, ai-agent]
version: v2.9.0
lang: zh
---

# 工具鏈版本對照（本地 ↔ CI）

> 目的：本地（dev container、`make`、pre-commit）與 GitHub Actions 跑**同一套工具版本**，查案時「本地綠、CI 紅」不會是版本差造成的。[#1337](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1337)。
>
> ⛔ 本頁**刻意不寫版本號**——版本號只存在於下表「SSOT」欄那個檔案裡，寫在這裡就是第二份、必然先腐爛。要看現值，打開 SSOT 那一格；要知道升版時要一起改哪裡，看「消費端」與「守衛」兩欄。守衛會在任何一處沒跟上時轉紅。

## 升版的通則

1. 改 SSOT，再改同一列的每一個消費端——**同一個 PR**。
2. 有 checksum 的一起換（上游的 `sha256sums.txt` / `checksums.txt`；下載覆核一次再寫進去）。
3. 跑該列的守衛；守衛沒寫到的列，按「消費端」欄逐一 grep 確認。
4. 跨 major 的升級另開 PR，並在 PR 內附上游 release notes 的破壞性變更評估。

## 對照表

| 工具 | SSOT | 消費端（升版時一起改） | 守衛 |
|---|---|---|---|
| Runner OS | 各 workflow job 的 `runs-on`（全部同值） | `.devcontainer/devcontainer.json` 的 base `image` tag | `tests/shared/test_toolchain_pin_parity.py`（runner） |
| Node.js | `.devcontainer/devcontainer.json` node feature `version`（major） | 每個 `actions/setup-node` 的 `node-version`（ci.yml portal、playwright.yml ×2、visual-baseline.yaml、commitlint.yaml、docs-ci.yaml）；Makefile `test-e2e` 說明 | 同上（node） |
| Python | `.devcontainer/devcontainer.json` python feature `version` | 每個 `actions/setup-python` 的 `python-version`（含 `${{ matrix.python }}` 展開後的值）；`requirements/ci-constraints.txt` 的版本是對這個 Python 解的 | 同上（python） |
| Go | 各 module 的 `go.mod` | Dockerfile builder、devcontainer go feature、CI `setup-go`（`go-version-file`）皆從它對齊 | 另一支 PR 補守衛 |
| golangci-lint | `.github/workflows/validate.yaml` `GOLANGCI_VERSION` | devcontainer go feature `golangciLintVersion`（⛔ 不帶 `v`） | `tests/ops/test_go_lint_module_coverage.py` |
| Trivy | `Makefile` `TRIVY_VERSION` | 每個 `aquasecurity/trivy-action` 步驟的 `version:`（release.yaml、component-docker-build.yaml、nightly-image-scan.yaml）；`.devcontainer/install-trivy.sh`（`TRIVY_VERSION` + `TRIVY_SHA256`） | `test_toolchain_pin_parity.py`（trivy） |
| Helm（全 repo） | devcontainer kubectl-helm-minikube feature `helm` | 每個 `azure/setup-helm` 的 `version:`（ci.yml Python Tests／Coverage／federation-e2e、release.yaml ×4） | `test_toolchain_pin_parity.py`（helm） |
| Helm（Container SAST job） | `scripts/tools/lint/check_iac_helm.py` `HELM_VERSION`（+ `HELM_DIGEST`） | ci.yml Container SAST job 的 `setup-helm` `version:` | 同上（「跑 `check_iac_helm.py` 的 job」由守衛自動辨識） |
| kubectl | devcontainer kubectl-helm-minikube feature `version`（⛔ 這個 feature 沒有 `kubectl` 選項） | 無 CI 消費端；跟著 kind 預設 node image 的 minor 走（±1 skew） | `test_toolchain_pin_parity.py`（kubectl、不得浮動） |
| kind | devcontainer `postCreateCommand` 的下載 URL 版本 + sha256 | `scripts/setup.sh` 等本地叢集流程；kind 版本決定預設 node image，kubectl 跟著調 | `test_toolchain_pin_parity.py`（kind） |
| Docker（dev container） | —（刻意 `latest`，理由見 devcontainer.json） | CI 端由 runner image 決定，兩邊都不釘 | 豁免登記在測試的 `_FLOATING_ALLOWED` |
| actionlint | `.pre-commit-config.yaml` `rev` | ci.yml `ACTIONLINT_VERSION`（兩個 job） | `tests/shared/test_actionlint_pin_parity.py` |
| ShellCheck | `.pre-commit-config.yaml` shellcheck-py `rev` | CI 以 `pre-commit run shellcheck` 呼叫同一個 hook，自動同版 | 升版需重量該 hook 註解中的 fail-closed 三道門 |
| hadolint | `scripts/tools/lint/check_iac_vibe_rules.py` `HADOLINT_VERSION`（+ digest） | ci.yml `HADOLINT_VERSION` + SHA-256 + cache key | `test_toolchain_pin_parity.py`（wrapper fallback） |
| kube-linter | `scripts/tools/lint/check_iac_helm.py` `KUBE_LINTER_VERSION`（+ digest） | ci.yml `KL` + `KL_SHA256` + cache key | 同上 |
| pint | `scripts/tools/lint/check_pint.py` `PINT_VERSION`（+ digest） | ci.yml pint 下載步驟 + cache key | `tests/lint/test_check_pint.py` |
| promtool（rule-pack gate） | `.devcontainer/install-promtool.sh` | ci.yml、nightly-vm-replay.yaml | `tests/preview/test_promtool_pin_parity.py` |
| promtool（docs-ci I-4） | docs-ci.yaml `PROMTOOL_VERSION` | 目前與 rule-pack gate 同版；major 須等於部署的 Prometheus image | `tests/ops/test_docs_ci_promtool_major.py`（只守 major） |
| amtool | docs-ci.yaml `AMTOOL_VERSION` | 刻意等於部署的 alertmanager image（`k8s/03-monitoring/deployment-alertmanager.yaml`） | 無 |
| yq | docs-ci.yaml `YQ_VERSION` | 無其他消費端 | 無 |
| Vector | ci.yml `VECTOR_VERSION` | `.devcontainer/install-vector.sh`、`helm/vector` image tag / appVersion | `tests/shared/test_vector_pin_parity.py` |
| GitHub Actions | 無單一 SSOT：同一個 action 在全部 workflow 必須同一個 ref | `.github/workflows/**` | `test_toolchain_pin_parity.py`（single version） |
| Python 套件 | `requirements/ci-constraints.txt` | 每個 `pip install … -c`、devcontainer `postCreateCommand`、`.claude/hooks/session-start.sh` | `check_unpinned_deps`、`check_devcontainer_dep_parity.py` |

## 已知未統一（刻意保留，附理由）

- **Container SAST job 的 Helm 仍是 3.x**，其餘 CI 與 dev container 是 4.x。它必須與 `check_iac_helm.py` 的 Docker fallback 同版，而那個 fallback 是 image + digest 釘住的；要升，得連同 `HELM_VERSION`／`HELM_DIGEST` 一起重新解析 digest。守衛已把這一對綁住，升的時候不會只動一半。
- **hadolint、kube-linter、pint、golangci-lint 落後上游**：每一個都有「CI 二進位 + wrapper 的 digest-pinned fallback image」兩處要一起動，升版需要重新解析 image digest，另開 PR。
- **`download-artifact@v4`、`docker/*`、`dorny/paths-filter@v3`、`codeql-action@v3`、`configure-pages` / `deploy-pages` / `upload-pages-artifact`** 等各自已是單一版本，但上游已有更新的 major；跨 major 需逐一評估 release notes，不在「同 major 內統一」的範圍。
- **cosign／syft（release.yaml）**：安裝器 action 與二進位版本都刻意釘住（檔內註解：「bump deliberately」），簽章鏈不跟著一般升版走。
- **未 pin 的 npm 全域安裝**：commitlint.yaml 的 `@commitlint/*`、docs-ci.yaml 的 `@mermaid-js/mermaid-cli` / `puppeteer` 每次抓最新版——`check_unpinned_deps` 的 `BASELINE_ALLOWLIST` 明列這兩筆為已知例外（#1158 follow-up）。Node 釘到 24 之後，commitlint 最新版 `engines` 要求的 `>=22.12` 才第一次被滿足。
