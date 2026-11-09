# 认证接入

执行器不关心"人怎么登录"，只关心"后续请求带什么"。所有认证都收敛到七种类型，用例完全不用改。

选型顺序：能用静态凭据就别用登录接口，能用登录接口就别开浏览器。**越往右越慢、越脆。**

| 类型 | 适用 | 稳定性 |
|---|---|---|
| `none` | 匿名接口、健康检查 | 最高 |
| `header` | 固定请求头令牌（`app-sec-at`、`X-Api-Key`） | 高 |
| `bearer` | `Authorization: Bearer <jwt>` | 高 |
| `cookie` | 已有会话 Cookie 串 | 高 |
| `form_login` | 有登录接口，密码可直传 | 中 |
| `command` | 自己写脚本取凭据（含签名、加密、二次验证） | 中低 |
| `browser_login` | 必须点页面才能登录（图形验证码 / 扫码 / SSO），走 Playwright 原生驱动 | 中（L1 复用登录态时较高） |

`browser_login` 有三级策略：L1 复用人工登录存的 storageState（推荐）、L2 按 login_steps 自动填表、L3 验证码交给 `command` 挂脚本。详见下面 `browser_login` 一节。

> 用 UI 通道时，**别只为了拿 token 就开浏览器**。如果 `form_login` 能拿到，就走 `form_login` —— 浏览器启动本身要 1-2 秒，比一次 HTTP 登录慢一个数量级。

## none

```json
{"type": "none"}
```

## header

```json
{"type": "header", "header": "app-sec-at", "token_env": "AUTOTEST_TOKEN"}
```

凭据来源三选一，优先级：字面量 > 环境变量 > 文件。

| 写法 | 说明 |
|---|---|
| `"token": "abc"` | 直接写死，**只用于本地临时验证，不要提交** |
| `"token_env": "AUTOTEST_TOKEN"` | 从环境变量读，CI 里用 secret 注入（推荐） |
| `"token_from": "seeds/token.txt"` | 从文件读，相对规格所在目录 |

可加 `"prefix": "Token "` 自定义前缀，默认无前缀。

## bearer

```json
{"type": "bearer", "token_env": "AUTOTEST_TOKEN"}
```

等价于 `header` + `Authorization` + `Bearer ` 前缀。要自定义头名就写 `"header": "X-Token"`。

## cookie

```json
{"type": "cookie", "cookie_env": "AUTOTEST_COOKIE"}
```

值形如 `SESSION=abc; tenant=1001`，会拆成 Cookie 罐。文件方式用 `cookie_from`。

## form_login

有登录接口时的首选。支持前置步骤（拿 CSRF / nonce）、JSON 或表单体、断言登录成功、提取令牌或会话 Cookie。

```json
{
  "type": "form_login",
  "header": "Authorization",
  "prefix": "Bearer ",
  "login": {
    "method": "POST",
    "path": "/api/auth/login",
    "json": {"username": "${env.USER}", "password": "${env.PASS}"},
    "pre_steps": [
      {"method": "GET", "path": "/api/auth/csrf", "提取": {"csrf": "data.token"}}
    ],
    "断言": [{"类型": "成功标志", "等于": true}],
    "extract": {"token": "data.accessToken"}
  }
}
```

要点：

- `extract` 的 key 固定用 `token`（要加进请求头的那个）；其他 key 会存成用例变量供后续步骤引用。
- 会话型系统不用 `extract`，改 `"extract_cookie": "SESSION"`，执行器会把整个 Cookie 罐带下去。
- 登录成功后必须能拿到至少一个凭据，否则直接报错，不会带着空身份继续跑。
- `login.json` 里的 `${env.USER}` 从环境变量/.env 取，密码不进规格文件。

真实例子（一个需要签名的登录）：

```json
{
  "type": "form_login",
  "login": {
    "method": "POST",
    "path": "/common/idaas/v1/login",
    "form": {
      "username": "${env.USER}",
      "password": "${env.PASS_B64}",
      "captcha": "${env.CAPTCHA}"
    },
    "断言": [{"类型": "成功标志", "等于": true}],
    "extract": {"token": "data.token"}
  }
}
```

图形验证码是随机的，`form_login` 撑不住——直接换 `browser_login`。

## command

自己控制取凭据的全过程：调内部 CLI、跑签名脚本、解加密、读 vault。

```json
{
  "type": "command",
  "command": "python3 scripts/get_token.py --env test",
  "timeout": 120,
  "header": "app-sec-at"
}
```

约定：

- 脚本 **stdout 输出令牌本体**，或输出 JSON（取 `token` / `access_token` / `value` 字段）。
- 退出码非 0 视为失败，stderr 会进错误信息。
- 脚本能读到被注入的环境变量；工作目录是规格所在目录。
- 会做一次 `probe` 才继续，脚本拿到废 token 也能立刻发现。

## browser_login

原生浏览器登录。三种做法，按可靠性排序，能走前一级就不走后一级。

### L1：复用 storageState（推荐）

人工登录一次，把整个浏览器态存下来，之后每条用例直接加载。

```json
{
  "type": "browser_login",
  "storage_state": "seeds/state.json",
  "save_state": "seeds/state.json",
  "token_from": "localStorage:token",
  "header": "app-sec-at",
  "probe": {"path": "/auth/tokenValid", "断言": [{"类型": "成功标志", "等于": true}]}
}
```

生成 state：

```bash
python3 scripts/auto_test.py login spec.json --env test
```

打开**可见**浏览器 → 人工完成登录（含验证码）→ 回车 → 存盘。

`token_from` 支持：`localStorage:键名` / `sessionStorage:键名` / `cookie:名` / `js:表达式` / 裸键名（先试 localStorage 再试 sessionStorage）。

浏览器里的 Cookie 会**自动同步**给 HTTP 客户端，两条通道登录态一致，不需要额外配置。

**为什么必须由真实登录产生 state**：实测过一个反例 —— 手工拼一个只塞了 `token` 的 state，路由守卫能过（`tokenValid` 返回 `success:true`、URL 也跳到目标页），但 `#app` 里只有一个空注释，页面全白。真实登录还会写 `loginType`、`app`、`user`、`route`、`tabs` 等前端状态，主布局依赖它们才渲染。抄近路得到的是一个"看起来登录了、实际什么都测不了"的假会话。

### L2：login_steps 自动填表

验证码已关闭（或有可白名单的测试账号）时用。

```json
{
  "type": "browser_login",
  "login_url": "/login",
  "login_steps": [
    {"ui": "填写", "选择器": "input[placeholder*='账号']", "值": "${env.TEST_USER}"},
    {"ui": "填写", "选择器": "input[placeholder*='密码']", "值": "${env.TEST_PASS}"},
    {"ui": "点击", "选择器": "button:has-text('登录')", "等待": {"选择器": "text=我的应用", "超时": 10000}}
  ],
  "success": {"类型": "URL", "包含": "/application"},
  "save_state": "seeds/state.json",
  "token_from": "localStorage:token",
  "header": "app-sec-at"
}
```

`login_steps` 用与用例相同的 UI 动作语法。`success` 用与断言相同的语法。

登录失败时会抛出**带页面现场的错误**：判定详情 + 当前 URL + 页面文本片段。这样能直接分辨是账号错、验证码错，还是页面根本没加载。

`save_state` 让执行器登录成功后自动存盘，下次走 L1 快速路径，不必每天都登。

### L3：验证码

不做进主流程。图形验证码要 OCR 或打码平台，成功率不稳；而且绕过验证码可能违反被测系统的安全策略，这个决定不该由工具替你做。

需要时用 `command` 挂自己的脚本：

```json
{"type": "command", "command": "python3 scripts/ocr_login.py", "header": "app-sec-at"}
```

脚本职责只有一个：登录完把 token 打到 stdout（或输出 `{"token": "..."}`）。

**不要**在脚本里写断言 —— 判定统一由执行器产出，否则两套结论会打架。

## 探活（probe）

任何需要身份的规格都应该配。没配的话，token 过期会跑出几十条业务失败，看起来像系统崩了。

```json
{
  "type": "header",
  "header": "app-sec-at",
  "token_env": "AUTOTEST_TOKEN",
  "probe": {
    "method": "GET",
    "path": "/auth/tokenValid",
    "断言": [{"类型": "成功标志", "等于": true}]
  }
}
```

探活失败 → 所有用例标 `blocked`、退出码 3、报告顶部警告"本轮结论无效"。这是**唯一**允许整轮不执行的情况。

探活接口怎么选：挑一个"只要身份有效就一定成功、且没有副作用"的接口。`/auth/tokenValid`、`/user/self/info`、`/health` 这类。

## 多环境

凭据跟着环境走，不要写进规格。用环境档覆盖：

```json
{
  "default": "test",
  "profiles": {
    "test": {
      "base_url": "http://10.31.0.101",
      "api_prefix": "/common/idaas/v1",
      "auth": {"type": "header", "header": "app-sec-at", "token_env": "AUTOTEST_TOKEN_TEST",
               "probe": {"path": "/auth/tokenValid"}}
    },
    "uat": {
      "base_url": "http://10.31.0.102",
      "api_prefix": "/common/idaas/v1",
      "auth": {"type": "form_login",
               "login": {"method": "POST", "path": "/login",
                         "json": {"username": "${env.UAT_USER}", "password": "${env.UAT_PASS}"},
                         "extract": {"token": "data.token"}}}
    }
  }
}
```

```bash
python3 scripts/auto_test.py run spec.json --env uat
```

`.env` 放在规格同目录会被自动读取，优先于系统环境变量。**`.env` 必须进 .gitignore。**

## 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| 所有用例 `blocked` | 凭据未就绪 | 看报告顶部的 `credential_error` |
| 探活 401/`success=false` | token 过期或环境不对 | 重新生成凭据，确认 `--env` |
| 登录成功但后续 401 | 提取路径错 / 该用 Cookie 而非 Header | 打印登录响应，对字段名；改 `extract_cookie` |
| 只有部分接口 401 | 该接口要额外头或不同令牌 | 在步骤 `headers` 里补 |
| `blocked` 但地址能打开 | HTTPS 自签证书 | 规格里加 `"verify_tls": false` |
| Cookie 不生效 | 域名/路径不匹配 | 改用 `cookie` 类型直接注入 |
| 登录一直失败 | 有图形验证码 / 前端加密 | 换 `command` 或 `browser_login` |
| `storageState 文件不存在` | 还没生成 | 跑 `auto_test.py login spec.json --env test` |
| `没从「localStorage:xxx」取到 token` | 键名写错 / 登录未成功 | 用 `login` 子命令登录后在控制台 `Object.keys(localStorage)` 核对 |
| `登录已完成但没有凭据` | 只配了 storage_state，没配 token_from 也没 Cookie | 补 `token_from`，或确认登录后有会话 Cookie |
| 加载 state 后页面空白 | state 是手工拼的，缺前端状态 | 用 `login` 子命令重新生成，别自己拼 |
| `browser_login 需要 Playwright` | 依赖没装 | `pip install playwright` |
| 登录态过期导致整批失败 | 没配 `probe` | 加 `auth.probe`，让过期被拦成环境阻断 |
