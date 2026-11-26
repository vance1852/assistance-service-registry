# 跨城便民接线的服务发现与转接

本目录提供现场联调用的合成合作方记录。记录中的机构、地点、旅客和标识均为虚构。每份记录可单独使用。

## 合作方报文

`agreements` 按服务请求地、归属机构、受托机构、业务和语言给出授权。`active` 为新接线窗口，`continue_until` 是已接起会话可在原席接续的右端点。时间均以该营运日本地分钟表示，窗口左闭右开。每个登记由机构、地址、业务和语言共同标识，`version` 是该声明的更新号；保活指向给定版本，`until` 表示在线证据的右端点。撤回即使后面还收到保活也算撤回。
交接邀请使用 `POST /seats/{id}/handoffs`，发送 `session` 与 `handoff_id`，受理为 202。接手单独回报 `accepted_at` 和 `handoff_id`。资料的 `invite` 对应一条前台安排的业务动作，标识用于对应后续回报；接手地点因实际发现结果而定。

## 本地联调

使用 Python 3.11 的标准库。现场联调环境固定为 Python 3.11.2。启动本地资料端后保持该进程运行，默认只监听回环地址的 8090 端口。

```sh
python partner.py fixtures/night-delegation.json
```

资料端按路径回放记录中的响应。接口 `GET /fixture/facts` 返回起始事实，`GET /fixture/events` 返回按到达时间排列的原始事件。同一到达时刻的事件按记录中的顺序出现。`GET /fixture/journal` 返回收到的合作方请求，含未匹配及已收件但主动断开连接的请求。重启资料端会从该份记录开头开始。

`exchanges` 每条只使用一次，按方法、路径和列出的正文键值及请求头匹配最早尚未使用的记录。路径中的花括号匹配一个标识段。`delay_ms` 是合作方回包延迟，`drop` 表示合作方收到请求后直接关闭连接。未匹配的请求返回 404。记录字段可随新的联调资料调整。

在同一目录用 `python -m unittest -v` 核对资料载入和合作方接口，包括已记录的状态码、回包、断连及接收记录。

---

# 公共接线中间件

本仓库同时交付一个仅依赖 Python 3.11 标准库的常驻 HTTP 服务 `middleware.py`。它把**登记/保活、服务发现、游客同意、授权有效期、会话责任、交接证明、拒绝原因和持久化恢复**放在同一处处理；车站、景区、市级热线等地址都作为带 `kind/site_kind` 的席位登记。

## 1. 启动与联调

默认监听 `127.0.0.1:8080`，状态原子写入 `/workspace/dispatch-state.json`，合作方邀请地址默认走 `http://127.0.0.1:8090`。

```sh
# 终端 A：仓库给定的合作方资料端
python partner.py fixtures/night-delegation.json --port 8090

# 终端 B：接线中间件
./run.sh
# 等价于：
python middleware.py --host 127.0.0.1 --port 8080 \
  --db ./dispatch-state.json \
  --partner-base http://127.0.0.1:8090

# 终端 C：按 fixture 中 events 的业务时间顺序回放
python replay_fixture.py fixtures/night-delegation.json
```

语言同意样例可直接回放；该样例没有外发交换：

```sh
python replay_fixture.py fixtures/language-consent.json
```

如果希望直接从正在运行的 `partner.py` 拉取 `/fixture/facts` 和 `/fixture/events`，也可以：

```sh
python sync_partner_fixture.py --partner http://127.0.0.1:8090 --middleware http://127.0.0.1:8080
```

后台常驻：

```sh
nohup ./run.sh > middleware.log 2>&1 &
echo $! > middleware.pid
```

重启进程时继续使用同一个 `--db` 文件即可恢复会话、责任归属、证明和审计。也可用环境变量 `DISPATCH_HOST`、`DISPATCH_PORT`、`DISPATCH_DB`、`PARTNER_BASE`。

## 2. 给前台和管理员的接口

服务根路径 `GET /api` 返回接口索引，`GET /healthz` 做健康检查。

### 合作方按仓库协议上报

最简单的方式是直接发送 fixture 中的事件对象：

```sh
curl -sS -X POST http://127.0.0.1:8080/api/events \
  -H 'Content-Type: application/json' \
  -d '{"at":1256,"kind":"heartbeat","agency":"rail","seat":"r4",
       "service":"lost","language":"zh","version":1,"until":1290}'
```

也提供语义化别名：

* `POST /api/register` / `/registrations`
* `POST /api/heartbeat` / `/heartbeats`
* `POST /api/withdraw` / `/withdrawals`
* `POST /admin/facts`：载入 `facts.agreements`
* `POST /api/agreement-changes`
* `GET /api/directory?at=1264`：地址表及每项能力当前是否在线

登记可带 `site_kind`（如 `station`、`scenic`、`hotline`）、`address`、`handoff_url`。回放 fixture 时的 `"kind":"register"` 是协议动作，不会被误当成场所类型。

### 前台接入新求助或找回原会话

```sh
curl -sS -X POST http://127.0.0.1:8080/api/intakes \
  -H 'Content-Type: application/json' \
  -d '{
    "at":1252,
    "session":"case-8",
    "area":"river",
    "service":"lost",
    "language":"yue",
    "accepted_alternatives":[["lost","zh"]]
  }'

curl -sS -X POST http://127.0.0.1:8080/api/continue \
  -H 'Content-Type: application/json' \
  -d '{"session":"case-8","at":1269}'
```

响应状态包括：

* `awaiting_answer` / `active`：有 `target` 可用；
* `needs_consent`：有未获游客同意的替代，返回 `alternatives` 和 `alternative_prompt`，但不会自动改派；
* `pending_duty`：无法自动分配或接续，返回 `missing`、`duty` 和具体原因；
* 同一会话 ID 重放/重连保持幂等，不产生第二单或两个回拨义务。

### 接起与跨机构交接

席位回报接起：

```sh
curl -sS -X POST http://127.0.0.1:8080/api/sessions/case-8/answer \
  -H 'Content-Type: application/json' \
  -d '{"at":1254,"seat":"h1"}'
```

安排交接。未指定 `seat` 时，中间件按当前时点的授权、登记、保活和游客已同意替代发现接手席，排除当前责任席，并按仓库协议向
`{partner_base}/seats/{seat_id}/handoffs` 发送恰好包含 `session`、`handoff_id` 的邀请：

```sh
curl -sS -X POST http://127.0.0.1:8080/api/sessions/case-8/handoffs \
  -H 'Content-Type: application/json' \
  -d '{"at":1259,"handoff_id":"h-26","reason":"delegation_ending"}'
```

接手席单独回报：

```sh
curl -sS -X POST http://127.0.0.1:8080/api/handoffs/h-26/acceptance \
  -H 'Content-Type: application/json' \
  -d '{"at":1264,"seat":"r4","accepted_at":1261}'
```

只有接受时点位于授权 `active` 窗口、接受席具备登记/有效保活，且回执处理时点仍满足 `continue_until` 等接续条件时，才生成 `proof` 并原子转移责任和唯一回拨义务。仅发出邀请不会把会话标成转出。

### 管理员核查

```sh
curl -sS http://127.0.0.1:8080/admin/sessions/case-8
curl -sS http://127.0.0.1:8080/admin/sessions/case-8/rejections
curl -sS http://127.0.0.1:8080/admin/duty
```

管理视图包含：

* `responsibility` / `original_responsibility`：当前与原始责任归属；
* `proofs`、`handoffs[].proof`：接手证明、接受时间、回执到达时间、授权和检查结果；
* `rejected_targets`：每个候选地址在何阶段因何原因被拒；
* `timeline`：接起、邀请、接受、陈旧回执拒绝、进入值守等事件；
* `callback`：全流程唯一的回拨义务及责任席变更链。

人工值守认领：

```sh
curl -sS -X POST http://127.0.0.1:8080/admin/sessions/case-8/duty/claim \
  -H 'Content-Type: application/json' \
  -d '{"duty_id":"officer-li","at":1275}'
```

## 3. 规则落地位置

| 要求 | 实现 |
| --- | --- |
| 能拨通不代表仍获准 | `evaluate_pair` 同时检查区域、业务、语言、`agreement.agent=seat.agency`、左闭右开的 `active` 窗口、登记版本、撤回墓碑和保活 `until` |
| 代管要说清原因 | 目标中的 `delegated`、`agreement_id`、`authorization`、`delegation_reason` |
| 只有同意的替代可作退路 | `accepted_alternatives` 才参与自动选择；其他在线授权只进入 `needs_consent.alternatives` |
| 撤回后迟到保活不能恢复 | 撤回形成更高版本/墓碑；任何该能力的后续保活均记为 `capability_withdrawn`，同席其他语言/业务键不受影响 |
| 证据不足要说明缺什么 | `missing` 与每个地址的 `rejected_targets[].reasons` |
| 重连按原会话识别 | 会话 ID 幂等；`/api/continue` 与 `GET /api/sessions/{id}` 都返回同一责任链 |
| 新席明确接下同一交接才转移 | 接受回执校验 `handoff_id`、目标席、`accepted_at` 时授权和当前接续条件，通过后才有 proof 和责任转移 |
| 授权到期 | 新求助检查 `active`；已接会话检查 `continue_until`、当前保活/撤回等接续条件 |
| 期限耗尽且未转接 | 会话进入 `pending_duty`，并出现在 `/admin/duty` |
| 迟到旧回执/双重回拨 | 已由新交接接手后，旧 ID 回执返回 409 `stale_handoff_receipt`；一个会话只有一个 `callback.obligation_id`，转移只更换其 owner |
| 进程恢复 | 每次状态迁移后临时文件、`fsync`、`os.replace` 原子落盘到 JSON |

## 4. 测试

```sh
python -m unittest -v
```

测试覆盖仓库自带 partner 回放、两个 fixture 的完整业务时间线、同意边界、授权/保活/撤回、交接证明、陈旧回执、单一回拨义务、HTTP 路由和重启后状态恢复。

