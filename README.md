# AI 自动测试执行器

`ai-auto-test` 是一个以 JSON 规格驱动的自动化测试执行器。它将已有测试用例编译、校验并执行为 HTTP API 或 Web UI 操作，通过明确断言判定结果，最终生成可复现的测试证据、报告和 CI 可消费的退出码。

它适用于已经有测试用例或可执行规格、需要批量执行回归并可信地汇报结果的场景。它不负责凭空生成测试设计，也不把“请求成功”或“页面看起来正常”当成测试通过。对话式智能体评测由配套工具执行；本项目可将其结果与 API/UI 结果统一汇总。

## 分层理解

从使用者视角看，执行链路可以拆成五层：

```text
用例 / JSON 规格
       ↓
编译与静态校验（compile、lint）
       ↓
执行适配（HTTP API、Playwright UI、认证与环境）
       ↓
断言判定与证据采集（含重试、覆盖度、阻断分诊）
       ↓
统一结果、报告、回归对比与 CI 门禁
```

### 第 1 层：输入与规格

规格描述测试对象、环境、身份认证、变量、用例步骤和可判定的预期。已有用例表可以通过 `compile` 转成规格；只有规格时可直接 `lint` 和 `run`。字段、断言和安全能力的完整说明见 [`references/spec-schema.md`](references/spec-schema.md)。

### 第 2 层：编译与校验

编译器把用例步骤规范化，并检查断言缺失、模糊预期、变量引用错误、UI 轮询配置错误，以及虚拟依赖和观测配置是否符合约束。`lint` 不访问被测系统；有错误的规格不应进入执行阶段。

### 第 3 层：执行通道

- **HTTP API**：发送请求、传递变量、复用认证信息，并采集状态码、响应、耗时等证据。
- **Web UI**：通过 Playwright 执行页面操作，可检查页面、URL、控制台、网络请求和下载；失败时保留截图。
- **认证与环境**：支持令牌/表单/Cookie 等认证适配器、环境档和浏览器 `storageState`。依赖不可用或凭据探活失败时应报告阻断，而不是伪装成产品缺陷。

UI 依赖是可选的：未安装 Playwright 时 API 用例仍可运行，UI 用例会标记为 `blocked`。

### 第 4 层：判定、分诊与证据

断言引擎将实际响应与规格比较。每条用例最终保留独立状态：`passed`、`failed`、`blocked`、`skipped` 或 `manual`；重试后才通过的用例还会标记为不稳定项。环境阻断、人工项和跳过项不计入自动用例通过率，但会在报告中单独呈现。

测试结果包含执行上下文和可复现证据。默认对令牌、密码、Cookie 等敏感信息脱敏；需求覆盖度只有在规格提供需求点时才能精确对应需求清单。

### 第 5 层：报告、回归与流水线

一次执行会生成机器可读的 `result.json`、Markdown/HTML 报告和 JUnit XML。`compare` 用稳定用例 ID 对比两轮结果；`merge_results.py` 可把 API/UI 与兼容的智能体评测结果归一化为统一报告。门禁策略需显式配置，工具不会自行替项目决定阻断或 P0 是否允许发布。

## 核心模块

| 位置 | 职责 |
|---|---|
| `scripts/auto_test.py` | CLI 入口：`compile`、`lint`、`run`、`compare`、`login` |
| `scripts/core/compile.py` | 用例编译、规格静态校验与能力约束检查 |
| `scripts/core/runner.py` | 用例编排、步骤执行、依赖/重试及结果汇总 |
| `scripts/core/httpc.py`、`scripts/core/ui.py` | HTTP 与浏览器执行适配 |
| `scripts/core/asserts.py`、`scripts/core/contract.py` | 断言判断与响应契约校验 |
| `scripts/core/auth.py`、`scripts/core/env.py` | 认证和环境解析 |
| `scripts/core/safety.py`、`scripts/core/redact.py` | 高影响能力的安全策略与敏感信息脱敏 |
| `scripts/core/report.py`、`scripts/core/junit.py`、`scripts/core/coverage.py` | 报告、JUnit 和需求覆盖度 |
| `scripts/merge_results.py` | 跨执行通道的结果归一化、汇总与门禁 |
| `references/` | 规格、认证、UI、结果契约和分诊说明 |
| `assets/`、`tests/` | 示例规格/服务及自动化测试 |

## 快速开始

建议使用独立虚拟环境。Python 版本要求见 `requirements.txt` 注释（Python 3.9+）。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

# 查看命令
python scripts/auto_test.py --help

# 检查已有规格（离线静态检查）
python scripts/auto_test.py lint path/to/spec.json

# 执行规格
python scripts/auto_test.py run path/to/spec.json --env test
```

浏览器通道需要 Playwright 和可用浏览器；验证码识别会优先使用 `ddddocr`，并可回退到系统 `tesseract`。这些依赖缺失时对应能力会明确报告阻断，不影响纯 API 用例。详细登录配置见 [`references/auth-adapters.md`](references/auth-adapters.md)，页面动作见 [`references/ui-adapters.md`](references/ui-adapters.md)。

常用命令：

```bash
# 将带“执行”块的用例 JSON 编译成规格
python scripts/auto_test.py compile cases.json --out spec.json --name "示例回归"

# 只运行指定 ID 前缀的用例，并允许失败重试一次
python scripts/auto_test.py run spec.json --only TC-AUTH --retry 1

# 与上一轮结果比较
python scripts/auto_test.py compare baseline/result.json current/result.json

# 汇总多个执行通道的结果
python scripts/merge_results.py api/result.json agent/result.json --out merged
```

## 安全边界

- 规格中的自动判定必须由明确断言支撑；不完整或未知断言不能静默通过。
- `blocked` 表示环境或能力未就绪，不等于产品失败；`manual` 也不等于已验证。
- 并发写入、虚拟依赖和 DB/MQ/Trace 观测等能力要求显式隔离模式、主机白名单及对应许可开关。
- DB/MQ/Trace 只通过测试专用只读 HTTP 网关访问；执行器不直连后端，不接受 SQL、消息管理命令或 TraceQL。
- 结果默认脱敏，但执行用例时仍应避免把真实密码、令牌和个人数据写入规格、报告或截图。
- `execution/`、`outputs/` 等目录是本机运行数据，不属于可发布的技能源码；不要将真实环境配置或测试产物提交到仓库。

## 测试

```bash
python -m unittest discover -s tests
```

## 作为 Agent Skill 使用

本仓库根目录的 [`SKILL.md`](SKILL.md) 包含给 AI Agent 的触发条件、执行流程和操作规范。将本仓库安装到支持 Agent Skill 的环境后，用户可在已有测试用例或规格的前提下，让 Agent 按该流程执行、分诊并汇报测试结果。

变更历史见 [`CHANGELOG.md`](CHANGELOG.md)。
