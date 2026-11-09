# 统一结果契约与跨通道汇总

两个 Skill 的执行通道不同：

```text
ai-auto-test        → HTTP / Web，断言驱动       → result.json（cases）
testcase-designer   → 对话 / 工具调用，评测驱动   → result.json（results）
```

通道不同，但**结论必须能放在一张表里比较**。否则一个项目既有接口又有智能体时，只能人工拼两份报告，还会出现两套通过率口径互相矛盾。

`scripts/merge_results.py` 解决的就是这件事：只做识别、归一化、汇总，不重跑、不推断。

## 一、统一结果字段

每条用例归一化为同一组字段：

| 字段 | 含义 | 缺失时 |
|---|---|---|
| `batch_id` | 该用例所属执行批次 | 空字符串 |
| `source` | 来源 Skill（`ai-auto-test` / `testcase-designer`） | 必填，不存在则拒绝汇总 |
| `channel` | 执行通道：`api` / `ui` / `agent` | 由来源推断 |
| `case_id` | 稳定用例 ID | 必填 |
| `title` / `module` / `priority` | 基本信息 | 空字符串 |
| `status` | 统一状态（见下） | 无法识别时按 `manual` 处理 |
| `decision_method` | 结论是怎么判出来的：硬断言 / 模型评审 / 人工 | `未标注` |
| `expected` / `actual` | 期望与实际 | 空字符串 |
| `reason` | 失败或判定说明 | 空字符串 |
| `evidence` | 证据引用：`file` / `path` / `trace_available` / `detail` | `trace_available` 保持 `null`，**不猜** |
| `version` | 产出该结果的工具版本 | 空字符串 |
| `target` / `env` | 被测对象与环境 | 空字符串 |

`evidence.trace_available` 为 `null` 表示该通道不涉及或不提供 Trace，**不等于**“没有发生工具调用”。做安全或行为结论时不能把它当通过依据。

## 二、状态映射

| 统一状态 | ai-auto-test | testcase-designer | 含义 |
|---|---|---|---|
| `passed` | `passed` | 通过 | 已判定通过 |
| `failed` | `failed` | 失败 | 已判定不符合预期 |
| `flaky` | `flaky: true` | 不稳定 | 同一用例多次结果不一致 |
| `blocked` | `blocked` | 阻断 | 环境/接入问题，没测成 |
| `skipped` | `skipped` | — | 主动跳过 |
| `manual` | `manual` | 待人工 | 无法自动判定 |

**关键纪律**：`blocked`、`skipped`、`manual` 不是失败，也不进入通过率的分子，但它们必须出现在报告里。用“通过率 90%”掩盖 10 条没测成，是最常见的假绿。

## 三、口径

```text
已判定通过率   = passed / (passed + failed + flaky)
自动执行完成率 = (passed + failed + flaky) / 用例总数
P0 失败        = priority 为 P0 且状态为 failed 或 flaky 的条数
```

两个数要一起看：
- 已判定通过率高、完成率低 → 结论好看，但大量用例没测成；
- 完成率高、已判定通过率低 → 确实测出了问题；
- 两个都低 → 环境和质量同时有问题，先修环境。

## 四、用法

```bash
# 1) 跑接口/页面通道
python3 scripts/auto_test.py run spec.json --out ./结果/api

# 2) 跑智能体通道
python3 ../testcase-designer/scripts/run_agent_eval.py cases.json \
  --adapter command --command "python3 adapter.py" --out ./结果/agent

# 3) 合并成一份
python3 scripts/merge_results.py ./结果/api/result.json ./结果/agent/result.json \
  --out ./结果/汇总

# 可选：与上一轮汇总比较
python3 scripts/merge_results.py ... --baseline ./结果/汇总/merged_result.json
```

产物：

```text
merged_result.json   机器可读，含统一字段与汇总
汇总报告.html        单文件离线可看
汇总报告.md          便于贴进工单或周报
```

退出码：`0` 无失败/不稳定；`1` 存在失败或不稳定；`2` 输入不可用（格式无法识别、文件缺失）。

## 五、离线演示（不依赖任何外部环境）

仓库内自带一个极简本地 API 服务和两个示例，可在本机完整跑通“接口 + 智能体 → 汇总”：

```bash
cd <ai-auto-test 目录>

# 起本地演示服务（另开一个终端）
python3 assets/demo_server.py --port 8899

# 接口通道（2 条：1 通过、1 失败——失败是服务故意忽略 pageSize）
python3 scripts/auto_test.py run assets/example_merge_spec.json --out /tmp/demo-api

# 智能体通道（mock 回放，只验证评测链路，不代表真实质量）
python3 ../testcase-designer/scripts/run_agent_eval.py \
  ../testcase-designer/assets/example_agent_cases.json \
  --adapter mock --out /tmp/demo-agent --yes

# 汇总
python3 scripts/merge_results.py /tmp/demo-api/result.json /tmp/demo-agent/result.json \
  --out /tmp/demo-merged
```

预期输出形态：

```text
汇总 10 条｜来源 ai-auto-test:2、testcase-designer:8
通过 6｜失败 2｜不稳定 1｜阻断 0｜跳过 0｜待人工 1
已判定通过率 66.7%　自动执行完成率 90.0%　P0 失败 1
```

若本机设置了 HTTP 代理，访问 `127.0.0.1` 需要 `no_proxy=127.0.0.1`，否则会被代理拦截成 502。

## 六、脱敏、旧结果与门禁

- **汇总层默认再脱敏一遍**：源结果可能来自没开脱敏的旧版本或第三方工具，不能假设它们已经干净。默认遮蔽令牌、Cookie、密码、授权头；`--redact-keys phone,id_card` 追加业务字段；`--no-redact` 仅限本地调试，脚本会打印警告。
- **旧结果没有 ID 时按位置兜底**：生成 `LEGACY-001` 这类 ID，避免多行 `case_id` 都为空、在回归映射里塌缩成一条导致用例凭空消失。代价是旧结果若调整了用例顺序，对比会错位；要精确对比，先用新版评测器重跑一轮生成稳定 ID。
- **门禁不默认放行**：不传 `--gate` 时，报告写明“未配置门禁策略，本次不判断是否允许发布”，并把阻断、待人工、P0 失败单列，而不是静默通过。传 `--gate blocked,manual,p0` 后，触发任一条件即退出码非零。

## 七、明确不做的事

- **不重跑用例**：汇总只读已有结果，不会因为报告难看而重试。
- **不推断缺失信息**：Trace 没暴露就不写“未调用工具”，只标记不可观测。
- **不合并同名不同源的用例**：`case_id` 相同但 `source` 不同，视为两条，用 `source + case_id` 做回归对齐键。
- **不代替门禁策略**：退出码只反映失败与不稳定；阻断、待人工、P0 是否放行必须由 `--gate` 显式配置，不配就不给结论。
