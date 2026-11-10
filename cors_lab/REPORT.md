# Starlette CORS 对照实验报告（Starlette 1.10.0，纯内存 ASGI 调用）

## 1. 复现的问题与结论速览

前端跨域访问 Starlette 后端：正常 JSON 响应可读，路由抛异常时浏览器只报 CORS 错误。
根因不是“CORS 没配对”，而是 **`CORSMiddleware` 与 `ServerErrorMiddleware` 的相对位置**：

- 用 `app.add_middleware(CORSMiddleware, ...)` 时，CORS 位于应用内部、`ServerErrorMiddleware` 的内层。
- 未捕获异常由最外层的 `ServerErrorMiddleware` 转成 500 响应并直接发出，**这个 500 响应不会再经过内层 CORS 的 `send` 包装**，因此没有任何 `Access-Control-*` 头。
- 浏览器对“跨域响应缺少允许头”的处理就是对 JS 隐藏响应体并报 CORS 错误，于是真实的 500 错误信息被掩盖。
- 把 CORS 包在应用最外层后，500 响应也会经过它的 `send` 包装，跨域头得以保留，浏览器就能读到 500 响应体。

两个流传的建议，结论如下：

1. **“允许所有来源（`*`）并开启 credentials”不可取。** Starlette 不会报错，而是在收到带 `Origin` 的请求时把 `*` 替换成“回显该 Origin”并带上 `Access-Control-Allow-Credentials: true`（见 `starlette/middleware/cors.py:163`）。这等于对任意站点（含 `https://evil.example`）开放带 cookie 的跨域读取，等同关闭同源保护。
2. **“加了 CORS 就不用防跨站提交”是错误的。** CORS 只约束浏览器里 JS 能否**读取**跨域响应，不阻止请求**到达并执行**。简单请求（表单 POST 等）根本不触发预检；即使响应因 CORS 被拦截，服务端写操作已经完成。会话 cookie 的写接口仍需应用层 CSRF 防护。

## 2. 为什么异常响应会丢掉跨域头（调用链追踪）

`Starlette.build_middleware_stack()` 固定把 `ServerErrorMiddleware` 放在最外层、`ExceptionMiddleware` 放在路由之外（`starlette/applications.py:63`）。

CORS 放在应用内部时，从外到内是：

```
ServerErrorMiddleware -> CORSMiddleware -> SessionMiddleware -> ExceptionMiddleware -> Router
```

- `CORSMiddleware.simple_response()` 用 `functools.partial(self.send, ...)` 包装下游的 `send`，跨域头只在这个包装里、于 `http.response.start` 消息上添加（`starlette/middleware/cors.py:147`）。
- `HTTPException` 由内层的 `ExceptionMiddleware` 转成正常响应，响应消息仍要穿过 CORS 的 `send` 包装，所以 **403 等 HTTPException 响应保留 CORS 头**。
- 普通 `RuntimeError` 不是 `HTTPException`，会一路冒泡到最外层 `ServerErrorMiddleware`。它 `except` 后调用 `await response(scope, receive, send)` 直接把 500 发给**外层原始 send**（`starlette/middleware/errors.py:180`），完全绕过 CORS 的 `send` 包装；随后还会 `raise exc` 重新抛出，便于服务器记录日志。
- 因此内层 CORS 对这个 500 毫无感知：既没有 `Allow-Origin`/`Allow-Credentials`，也没有它本应追加的 `Vary: Origin`，SessionMiddleware 同样被跳过，所以连 `Set-Cookie`、`Vary: Cookie` 都没有。

把 CORS 移到最外层后，顺序变为：

```
CORSMiddleware -> ServerErrorMiddleware -> SessionMiddleware -> ExceptionMiddleware -> Router
```

此时 `ServerErrorMiddleware` 发出的 500 仍会穿过外层 CORS 的 `send` 包装，跨域头被补齐；异常本身依旧会重新抛出，日志行为不变。注册 500 自定义 exception handler 不能修复内层摆放问题，因为该 handler 的响应同样在 CORS 内层生成。

## 3. 对照实验结果

运行 `.venv/bin/python -m cors_lab.run_lab` 可重跑。下表中 “-” 表示该响应头不存在；
“浏览器判定”按 Fetch/CORS 规则推导，与 ASGI 调用方能否读到响应无关。

### 3.1 普通请求与预检（CORS 位于应用内部，显式可信来源 + credentials）

| 场景 | 状态 | Allow-Origin | Allow-Credentials | Vary | Set-Cookie | 浏览器判定 | 副作用 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 同源 GET /session（无 Origin） | 200 | - | - | Cookie, Origin | 1 个 | 无 CORS 检查 | 无 |
| 可信来源 GET /session | 200 | https://app.example | true | Cookie, Origin | 1 个 | 可读 | 无 |
| 可信来源 POST /increment | 200 | https://app.example | true | Origin | - | 可读 | 计数 0→1 已生效 |
| 可信来源 GET /vary（应用自带 Vary） | 200 | https://app.example | true | Accept-Encoding, Origin | - | 可读 | 无 |
| 可信来源 OPTIONS 预检 | 200 | https://app.example | true | Origin, Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network | - | 预检通过，再发真实请求 | 路由未调用，计数不变 |
| 不可信来源 OPTIONS 预检 | 400 | - | true | 同上四项 | - | 预检失败，真实请求不发送 | 计数不变 |
| 不可信来源 POST /increment（简单请求） | 200 | - | true | Origin | - | CORS 失败，响应对 JS 隐藏 | 计数 0→1 已生效 |

要点：

- 预检由 CORS 中间件自己应答，失败时（400、无 Allow-Origin）浏览器**根本不发真实请求**，因此没有副作用；这是唯一能“挡住请求”的 CORS 环节，且仅对非简单请求生效。
- 不可信来源的简单 POST 不触发预检，服务端照常 200 并完成计数；浏览器只是禁止 JS 读取响应。**“前端报跨域错误”不等于“请求没生效”。**
- `Vary` 是合并而非覆盖：应用自带的 `Accept-Encoding`、SessionMiddleware 的 `Cookie` 都会保留，CORS 再追加 `Origin`（`starlette/middleware/cors.py:170` 与 `starlette/middleware/sessions.py:61`）。

### 3.2 错误响应：CORS 在应用内部 vs 包在应用外部

| 场景 | 状态 | Allow-Origin | Allow-Credentials | Vary | Set-Cookie | 异常 | 浏览器判定 | 副作用 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 内层 GET /boom（未捕获异常） | 500 | - | - | - | - | RuntimeError 重抛 | CORS 失败，500 被隐藏 | 无 |
| 外层 GET /boom（未捕获异常） | 500 | https://app.example | true | Origin | - | RuntimeError 重抛 | 可读 500 响应 | 无 |
| 内层 POST /count-boom（先计数后抛异常） | 500 | - | - | - | - | RuntimeError 重抛 | CORS 失败 | 计数 0→1 已生效 |
| 外层 POST /count-boom（先计数后抛异常） | 500 | https://app.example | true | Origin | - | RuntimeError 重抛 | 可读 500 响应 | 计数 0→1 已生效 |
| 内层 GET /http-error（HTTPException 403） | 403 | https://app.example | true | Origin | - | 无 | 可读 | 无 |
| 外层 GET /http-error（HTTPException 403） | 403 | https://app.example | true | Origin | - | 无 | 可读 | 无 |

要点：

- 内层摆放只在“未捕获异常产生的 500”上丢头；`HTTPException` 因由内层 `ExceptionMiddleware` 处理而不受影响——这解释了为什么“普通接口和 403 都正常，只有 500 看起来像跨域故障”。
- 外层摆放让 500 也带跨域头，但不会吞掉异常：`ServerErrorMiddleware` 仍重抛，日志与监控行为保持不变。
- `/count-boom` 证明：只要写操作发生在抛异常之前，**无论 CORS 头是否存在、前端能否读到响应，副作用都已落库**；外层摆放改善的是“可观测性”，不是“回滚能力”。

### 3.3 通配来源与 credentials 的三种预设

| 预设 | 响应 Allow-Origin | Allow-Credentials | 风险 |
| --- | --- | --- | --- |
| 显式可信来源 + credentials（推荐） | 回显可信来源 | true | 仅可信来源可带 cookie 跨域读取 |
| `*` 且不允许 credentials | 字面量 `*` | - | 任意来源可读公开响应，但无法带 cookie |
| `*` 且允许 credentials（危险） | 回显**任意**请求来源（含 evil.example） | true | 任意站点可携带受害者 cookie 发起并读取跨域响应 |

实验中 `wildcard_credentials` 预设下，对 `https://evil.example` 的预检返回 200 并回显该来源、附带 `credentials: true`，随后的 POST 也被浏览器放行且计数生效。这正是该组合等同于关闭同源保护的直接证据。

### 3.4 Set-Cookie 观察

`SessionMiddleware` 默认产出：

```
session=...; path=/; Max-Age=1209600; httponly; samesite=lax
```

- 只有会话被访问/修改的正常响应才会带它；内层 500 因绕过 SessionMiddleware 而没有 `Set-Cookie`。
- `HttpOnly` 不影响浏览器自动携带 cookie，只阻止 JS 读取。
- 默认 `SameSite=Lax`：跨站 POST 默认不携带该 cookie；要让跨域 `fetch(..., credentials: "include")` 真正带上，必须把会话 cookie 配成 `same_site="none"` 且 `https_only=True`（`SameSite=None` 要求 `Secure`），同时还要考虑浏览器的第三方 cookie 拦截策略。

## 4. 副作用矩阵：哪些情况请求已经生效

| 请求类型 | 预检结果 | 服务端是否执行 | 前端能否读响应 |
| --- | --- | --- | --- |
| 非简单请求（自定义头/JSON 等） | 预检失败 | 否，真实请求未发送 | 否（预检错误） |
| 非简单请求 | 预检通过 | 是 | 取决于响应 Allow-Origin/Credentials |
| 简单请求（表单 POST、简单 GET） | 无预检 | 是，始终执行 | 响应缺头则被浏览器隐藏 |
| 任意请求，路由在响应前抛未捕获异常（内层 CORS） | - | 异常前的代码已执行 | 否（500 无跨域头，被隐藏） |
| 同上，CORS 在最外层 | - | 异常前的代码已执行 | 是（可读到 500） |

结论：**CORS 是响应读取控制，不是请求执行控制。** 幂等性、权限校验、事务边界必须由服务端保证，不能依赖浏览器拦截。

## 5. 会话 cookie 写接口的 CSRF 边界（仍由应用负责）

CORS 与 CSRF 是两件正交的事，必须分层处理：

1. **CORS 层**：用显式来源白名单 + `allow_credentials=True`，绝不使用 `*` + credentials。它只决定哪些站点的 JS 能读响应。
2. **Cookie 层**：优先保留 `SameSite=Lax`（Starlette 默认）。它能挡住跨站 POST 自动带 cookie，是成本最低的 CSRF 缓解；但 Lax 下跨域 `fetch` 带不上会话 cookie，且 Lax 对顶层 GET 导航不设防。
3. **应用层 CSRF 防护（写接口必备，尤其在使用 `SameSite=None` 时）**：
   - 同步器令牌（double-submit cookie 或 session 绑定的 CSRF token），前端从非 HttpOnly cookie 或响应头读取后，以 `X-CSRF-Token` 之类自定义头回传；自定义头同时会触发预检，形成第二道门槛。
   - 校验 `Origin`/`Sec-Fetch-Site` 作为深度防御，但不要只靠它（历史客户端、同源策略差异）。
   - 写操作保持幂等、做服务端授权与资源属主校验；敏感操作要求重新认证。
4. 当业务确实需要跨域带 cookie（`SameSite=None; Secure`）时，Lax 的默认保护消失，**令牌校验成为必需而非可选**。

## 6. 推荐配置

下面的配置把 CORS 放在最外层、使用显式来源白名单，并把会话 cookie 与 CSRF 令牌的职责分开标注：

```python
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

TRUSTED_ORIGINS = ["https://app.example"]  # 显式白名单，禁止 "*" + credentials

inner = Starlette(
    routes=routes,
    middleware=[
        Middleware(
            SessionMiddleware,
            secret_key=SECRET_KEY,
            # 需要跨域带 cookie 时才改为 same_site="none"、https_only=True，
            # 并必须同步落地应用层 CSRF 令牌校验。
        ),
    ],
)

# 关键：包在应用最外层，使 500 响应也带 CORS 头。
app = CORSMiddleware(
    inner,
    allow_origins=TRUSTED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)
```

如果坚持用 `app.add_middleware(CORSMiddleware, ...)`，需明确它位于 `ServerErrorMiddleware` 内层，
未捕获异常的 500 仍会丢跨域头；最外层包装是保证错误响应可被跨域读取的直接做法。
配合上节的 CSRF 令牌与 `SameSite` 策略，才能同时解决“异常被误报为跨域错误”和“跨站写防护”两个问题。

## 7. 重跑方式

在仓库根目录执行以下两条命令即可复现全部表格与断言：

```bash
.venv/bin/python -m cors_lab.run_lab       # 完整对照表
.venv/bin/python -m pytest cors_lab/ -q    # 12 项断言，固化全部结论
```
