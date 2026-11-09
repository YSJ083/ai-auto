# 结果模型与分诊

## 为什么要有统一结果模型

同一份 `result.json` 要同时满足三拨人：

- **人**：看报告知道该修什么
- **CI**：看退出码决定放不放行
- **下一轮**：当基线做回归对比

所以要稳定、可 diff、字段不随手加。schema 标识 `autotest.result/2`。

## 结构

```json
{
  "schema": "autotest.result/2",
  "tool": {"name": "ai-auto-test", "version": "1.0.0"},
  "run": {
    "id": "7e6a8e950bc9",
    "started_at": "2026-10-19 17:37:22",
    "duration_ms": 1024,
    "spec_path": "/abs/path/spec.json",
    "spec_name": "登录后全模块接口用例集",
    "spec_version": "1.1",
    "filter": "TC-APP"
  },
  "target": {
    "env": "test",
    "base": "http://10.31.0.101/common/idaas/v1",
    "auth_type": "header",
    "credential_ok": true,
    "credential_error": "",
    "credential_source": "file:seeds/token.txt"
  },
  "summary": {
    "total": 29, "auto": 28, "passed": 19, "failed": 9,
    "blocked": 0, "skipped": 0, "manual": 1, "flaky": 0,
    "pass_rate": 67.9,
    "assertions_total": 93, "assertions_passed": 76
  },
  "coverage": {
    "enabled": true, "declared_source": "spec",
    "total": 15, "covered": 12, "verified": 10, "rate": 66.7,
    "uncovered": ["R-07", "R-11"], "failed": ["R-03"], "manual": ["R-09"],
    "partial_run": false
  },
  "cases": [...],
  "defects": [...]
}
```

## 单条用例

```json
{
  "id": "TC-APP-005",
  "module": "我的应用",
  "title": "页码超出总页数应返回空列表而非报错",
  "priority": "P1",
  "type": "异常",
  "status": "failed",
  "flaky": false,
  "duration_ms": 42,
  "reason": "请求第 999 页 → body.success=False（期望 True）",
  "basis": "页码越界应返回空列表，后端却抛出下标越界。",
  "defect_level": "中｜越界处理不统一",
  "requirements": ["R-01"],
  "assertions": {"passed": 1, "total": 3},
  "retried": false,
  "steps": [
    {
      "name": "请求第 999 页",
      "method": "GET",
      "url": "http://.../app/my-app?...",
      "status": 200,
      "elapsed_ms": 42,
      "passed": false,
      "net_error": false,
      "checks": [
        {"assertion": {"类型": "成功标志", "等于": true},
         "passed": false,
         "actual": "body.success=False（期望 True）"}
      ],
      "response_excerpt": "{\"success\":false,...}"
    }
  ]
}
```

`checks[].actual` 是给人看的一句话，必须同时含**实际值**和**期望值**，能直接贴进缺陷单。`response_excerpt` 截前 1500 字符，够复现。

`requirements` 是规格 `需求点` 的回填（数组，没标就是空数组）。它只服务覆盖度聚合，不影响判定。

### UI 步骤多出的三个字段

| 字段 | 含义 |
|---|---|
| `status` | 可能是 **`null`**：这一步没有发生导航，就不存在「状态码」这回事。别按整数比较，也别据此判阻断 |
| `diagnostics` | 采集/等待环节的异常（截图没存成、监听器没挂上、networkidle 超时、cookie 注入失败）。**不影响 `passed`**，但意味着本轮证据可能不完整 |
| `text_collected` | 是否采集了整页文本。`false` 时 `response_excerpt` 为空是正常的，不是页面空白 |

`diagnostics` 非空**不代表用例失败**。报告会把它单独列成一节并标注「不影响用例判定」。
其中两类要特别当心，它们会让结论看起来比实际更可信：

- **事件监听器未能挂载** → `console_errors` 恒为空，`控制台` 断言永远通过
- **失败截图没能保存** → 报告里截图位空白，看起来像「页面本来就这样」

## 需求覆盖（coverage 块）

通过率回答「跑过的过没过」，`coverage` 回答「该跑的跑了没有」。两个数必须一起看，否则「全绿」会被读成「测全了」。

```json
"coverage": {
  "enabled": true,
  "declared_source": "spec",
  "total": 15, "covered": 12, "verified": 10, "rate": 66.7,
  "verified_ids": ["R-01"], "uncovered": ["R-07"], "failed": ["R-03"],
  "blocked": [], "skipped": [], "manual": ["R-09"],
  "items": [
    {"id": "R-01", "说明": "登录锁定", "declared": true,
     "status": "passed", "cases": ["TC-AUTH-003"], "auto_cases": 1}
  ],
  "tracked_cases": 26, "untracked_cases": ["TC-MISC-001"],
  "partial_run": false, "filter": "", "interrupted": false
}
```

| 字段 | 口径 |
|---|---|
| `declared_source` | `spec` = 分母来自规格 `需求`；`observed` = 规格没声明，分母是「被引用到的需求点」，此时 `rate` 只说明「标了的都测了」 |
| `total` / `covered` / `verified` | 需求总数 / 至少有一条关联用例 / 关联用例全部通过 |
| `items[].status` | `passed`(已验证) / `failed` / `blocked` / `skipped` / `manual` / `uncovered`，取关联用例里**最差**的一个 |
| `uncovered` | 一条用例都没关联。与 `failed` 分开：一个是没写用例，一个是写了没过 |
| `untracked_cases` | 没标需求点的用例。它们不进分母，所以别用「覆盖率 100%」掩盖它们 |
| `partial_run` | `--only` 或 `--fail-fast` 中断为 `true`：**范围内的覆盖度不是覆盖度** |

`enabled: false` 表示整份规格都没标需求点，报告不渲染这一节（老规格不受影响）。

## 五种状态，不许混

| 状态 | 含义 | 算不算缺陷 | 进不进通过率 |
|---|---|---|---|
| `passed` | 全部断言通过 | 否 | 进（分子） |
| `failed` | 有断言不通过，且拿到真实业务响应 | **是（候选）** | 进（分母） |
| `blocked` | 请求没完成：连不上、超时、凭据失效 | 否，环境问题 | 不进 |
| `skipped` | 依赖的用例没通过，本条没跑 | 否 | 不进 |
| `manual` | 规格声明人工执行，或判定需要人 | 待定 | 不进 |

`flaky` 不是状态，是标记：首次失败、重试后通过。这种用例**比稳定失败的更该优先修**，因为它意味着行为不确定。

判 `blocked` 的规则：

1. 认证失败（凭据取不到 / 探活不通过）→ 全部用例 `blocked`。
2. 单条用例所有失败步骤的 HTTP 状态都是 0（连接层异常）→ `blocked`。
3. 用例声明 `依赖` 的用例状态不是 `passed` → `skipped`。

注意第 2 条只在该用例**所有**失败都来自连接异常时成立。如果一条用例既连不上又断言错，判 `failed`——不能拿网络当遮羞布。

## 退出码

| 码 | 含义 | CI 里怎么处理 |
|---|---|---|
| 0 | 全部通过 | 放行 |
| 1 | 有失败 | 拦住，看报告 |
| 2 | 规格/参数错误 | 拦住，这是测试代码的问题 |
| 3 | 环境阻断 | **不要拦住**，先报警修环境 |

3 和 1 必须分开。否则一次 token 过期会让流水线红一片，团队很快就学会"忽略红色"。

## 回归对比

```bash
python3 scripts/auto_test.py compare 上轮/result.json 本轮/result.json
```

四类变化，按优先级：

1. **新增失败**：上轮过、本轮挂 —— 最可能是本轮改动引入的，先看这个。
2. **仍失败**：没修好，看有没有人认领。
3. **已修复**：确认后可以移到回归集。
4. **本次未执行**：用例被删/改名/筛选，注意别把"没跑"当成"修好了"。

对比只按 `id` 匹配。**改 id 等于删用例 + 加新用例**，历史会断，所以 id 一旦定下不要改。

数据驱动用例的行 id 是 `TC-X#1`、`TC-X#2`…，所以对比是**按行比的**：
第 2 组数据这轮挂了、上轮是过的，会出现在「新增失败」里，能直接看出是哪组参数出的问题。
但**调整数据组的顺序等于改了行的身份** —— 在第 1 组前面插一组，原来 `#1` 变成 `#2`，
历史对比会错位。要加数据组就加在末尾。

## CI 接法

```yaml
- name: 接口自动测试
  run: |
    python3 scripts/auto_test.py lint spec.json
    python3 scripts/auto_test.py run spec.json --env test --retry 1 \
      --junit build/reports/junit.xml
  env:
    AUTOTEST_TOKEN: ${{ secrets.AUTOTEST_TOKEN }}
```

`junit.xml` 是给 CI 读的那份（默认也写到 `测试结果/junit.xml`），用例级结果直接进
流水线报表；`最新报告.md` 是给人读的那份。两者判定一致，只是受众不同。

状态到 JUnit 元素的映射：通过 → `<testcase>`、失败 → `<failure>`、
**阻断 → `<error>`**、跳过/手动 → `<skipped>`。阻断不映射成 `skipped` 是刻意的：
skipped 在多数 CI 里不算失败，等于把「这轮没测成」伪装成「不需要测」。

需要"只跑冒烟"时用优先级过滤：

```bash
python3 scripts/auto_test.py run spec.json --only TC-SMOKE
```

需要把结果喂给别的系统时加 `--json`，stdout 末尾会打一段机器可读摘要。

## 汇报模板

不要只报数字。照这个结构说：

```
本轮 28 条自动用例：通过 19，失败 9，阻断 0，手动 1，通过率 67.9%。
失败 9 条均为规格定义行为与实际不符，其中高 3 / 中 4 / 低 2。
有 2 条重试后才通过（TC-X、TC-Y），行为不稳定，建议优先排查。
3 条未自动判定，需人工：……
需求覆盖 12/15：R-07、R-11 还没有对应用例；R-03 有失败用例。
本轮结论有效（凭据探活通过，环境 test）。
```

环境有问题时：

```
本轮结论无效：凭据探活失败（token 过期），28 条全部阻断，未真正执行。
重新生成 token 后可重跑，本轮不产生缺陷结论。
```

## 与对话式智能体评测的衔接

`testcase-designer` 的 `run_agent_eval.py` 产出 `评测结果/result.json`，字段与本模型不同（那份面向"回答质量"，有评分和票数）。两者互转时只保留：用例 id、状态、失败原因、耗时。判定方式（规则 vs 评分）必须一起带过去，否则"通过率"含义会被偷换。
