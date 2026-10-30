# 节点连接契约

前端直接访问使用者配置的 HTTP(S) API。服务端使用允许来源与可选 Bearer 访问码控制访问；公开部署应启用访问码并配置 HTTPS。

## 浓差电池数值节点

- `GET /health`：连接检查；`auth_required` 表示是否配置访问码。
- `GET /state`、`/steps`、`/run`、`/comparison`、`/history`：既有数值和历史。
- `POST /simulate`、`POST /batch/start`：沿用原条件与批量接口。

## Matterix 协调节点

- `GET /health`：`mode: first_transfer`。
- `GET /capabilities`：反应模板、批量、预览与过程校验能力。
- `POST /simulate`：条件与 `Idempotency-Key`，返回本次运行编号。
- `GET /state?run_id=…`、`/preview?run_id=…`、`/history`：对应运行的状态与资料。
- 原校验、受约束局部修复与继续接口保留既有门禁。

## HMF 数值节点

- `GET /health`：`mode: hmf_numeric`，`models: [oxidation]`。
- `POST /api/hmf/run`：`{seed, conditions}`；返回 NDJSON 事件流。
- `GET /api/hmf/history`、`GET /api/hmf/history/<run_id>`：本节点记录。
- `POST /api/hmf/repair`：`{run_id}`，复用已有有界局部修复，不自动继续后续流程。

访问码使用 `Authorization: Bearer …`；不通过 URL 查询参数传递。公网域名更换后，更新后端的 `--allowed-origin`。节点地址不是 SSH 地址，浏览器填写的应是实际可访问的 API 入口。
