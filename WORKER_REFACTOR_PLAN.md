# Worker 改造计划（最终版：stdio 最简方案）

## 1. 目标

- 使用一个常驻 Python Worker，避免每次同步都新起进程。
- 同账号请求复用 auth 上下文，减少重复 `init_auth`。
- 去掉文件队列与复杂状态目录，最小化故障面。

## 2. 非目标

- 不兼容旧架构（直接切换）。
- 不做取消任务（`cancel` 暂不接入）。
- 不引入数据库。

## 3. 总体架构

```text
React UI
  -> invoke(Tauri)
  -> Rust WorkerHost (singleton)
  -> Python Worker (long-running)
  -> gemini_export.py (复用现有业务逻辑)
  -> 本地业务数据文件(accounts/conversations/jsonl/media)
```

关键点：
- 只有一个常驻 Worker 进程。
- Rust 与 Worker 通过 stdin/stdout 按行 JSON 通信。
- Worker 运行态仅在内存维护，不做重开恢复快照。

## 4. 通信模型（JSON lines）

每条消息一行 JSON，以换行符分隔（line-delimited JSON）。

请求（Rust -> Worker）：

```json
{"id":"req_1","method":"enqueue_job","params":{"type":"sync_list","accountId":"xxx"}}
```

响应（Worker -> Rust）：

```json
{"id":"req_1","ok":true,"result":{"jobId":"job_123"}}
```

事件（Worker -> Rust）：

```json
{"event":"job_state","payload":{"jobId":"job_123","state":"running","type":"sync_conversation","accountId":"xxx","conversationId":"yyy","phase":"download_media","progress":{"current":1,"total":2}}}
```

错误响应：

```json
{"id":"req_1","ok":false,"error":{"code":"AUTH_EXPIRED","message":"...","retryable":true}}
```

## 5. 最小接口面

对前端只暴露一个命令：

1. `enqueue_job`
- 入参：`type`, `accountId`, `conversationId?`
- 返回：`jobId`

说明：
- 不做 `cancel_job`。
- 不做重开恢复查询接口。

## 6. Worker 生命周期

### 6.1 启动

- 在 Tauri `run()` 初始化时启动 Worker 子进程。
- 启动后发送 `ping`，确认可用。

### 6.2 崩溃恢复

- Rust 侧监听子进程退出。
- 退出后自动重启（退避：0.5s -> 1s -> 2s）。
- 所有进行中任务标记为 `failed/interrupted`，不自动重入队。

### 6.3 退出

- App 退出时发送 `shutdown`。
- 超时未退出则强制 kill。

### 6.4 切账号行为

- 不中断当前任务。
- 新任务按队列顺序执行。

## 7. 任务与队列（内存）

Worker 内存中维护：
- `queued`
- `running`
- `done`（短期保留，供查询）
- `failed`（短期保留，供查询）

首版并发策略：
- 单 worker 串行执行（一次一个任务）。

任务类型：
- `sync_list`
- `sync_conversation`
- `sync_full`
- `sync_incremental`
- `clear_account_data`

## 8. 认证复用策略

按 `accountId + authuser` 维护会话上下文缓存：
- 同账号连续任务复用同一 exporter/auth。
- 遇到 auth 失败时强制刷新一次并重试一次。
- 重试仍失败则任务失败并上报。

## 9. 前端更新方式

- Rust 接收 worker 事件后通过 Tauri event `emit` 给前端。
- 前端根据 `job_state` 事件更新：
  - `listSyncing`
  - `fullSyncing`
  - `syncingConversationIds`

这样可去掉 worker 状态轮询，避免和现有列表轮询叠加。

## 10. 错误与超时处理

统一错误码：
- `AUTH_EXPIRED`
- `NETWORK_ERROR`
- `INVALID_INPUT`
- `SCRIPT_ERROR`
- `WORKER_UNAVAILABLE`
- `TASK_INTERRUPTED`

超时策略：
- `sync_conversation`：120s 无进度事件判超时
- `sync_list/sync_full/sync_incremental`：心跳续期

处理策略：
- 超时任务置 `failed` 并上报错误。
- Worker 崩溃触发自动重启；进行中任务视为失败，下次重跑允许少量重复，依赖落盘去重。

## 11. 与现有代码的落点

Rust：
- 新增 `src-tauri/src/worker_host.rs`（进程管理、请求路由、事件分发）
- `src-tauri/src/lib.rs`：
  - 删除 `Command::new(python ...output())` 路径
  - `run_list_sync/run_conversation_sync/...` 改为转发到 WorkerHost

Python：
- 新增 `scripts/gemini_worker.py`（主循环 + 队列 + 事件输出）
- 复用 `scripts/gemini_export.py` 的现有逻辑函数

前端：
- `App.tsx`：
  - 点击按钮改为 `enqueue_job`
  - 订阅 worker `job_state` 事件更新动画

## 12. 实施步骤

### Step 1：WorkerHost + Worker 骨架

- Rust 启动/重启/关闭 worker
- `ping` 往返
- JSON line 收发稳定

### Step 2：接入核心任务

- 先接 `sync_list` 与 `sync_conversation`
- 事件推送到前端动画

### Step 3：接入剩余任务并切换

- `sync_full` / `sync_incremental` / `clear_account_data`
- 删除旧的单次脚本执行路径

## 13. 验收标准

- 同账号连续两次 `sync_conversation`，第二次不重复完整 `init_auth`。
- worker 崩溃后可自动重启，进行中任务明确标记失败。
- 中断重跑时允许少量重复数据，但最终落盘结果去重正确。
- 不再使用文件队列目录（`inbox/running/done/failed`）。
- 前端不新增 worker 状态轮询。

## 14. 结论

采用 `stdio JSON lines` 是当前最简且稳定的方案：
- 比文件队列更轻。
- 比每次起脚本更快。
- 足够支撑你当前的同步需求。

## 15. 测试方案（完备）

本节用于验证：
- 启动后可完成完整列表同步。
- 可完成单对话同步（无媒体/有媒体/长对话）。
- 每个 case 都验证“中断 -> 重启 App -> 再次触发同任务继续执行”。
- 数据落盘正确，允许少量重复请求，但最终结果去重正确。

### 15.1 测试前置

- 运行环境：macOS + Tauri 桌面端。
- Worker 模式已启用（不走旧的单次脚本路径）。
- 本地账号映射确认：
  - `authuser=1` -> `cynaustralineeun_gmail_com`
  - `authuser=4` -> `cynaustine88_gmail_com`
- 样本对话：
  - `user4` 无媒体：`799f497104d1dd8c`
  - `user4` 有媒体：`019445126bb40106`
  - `user1` 长对话：`1b7de7846f0a44c1`

重点观察文件：
- `accounts/{accountId}/sync_state.json`
- `accounts/{accountId}/conversations.json`
- `accounts/{accountId}/conversations/{conversationId}.jsonl`
- `accounts/{accountId}/media/*`

重测执行规则（必须）：
- 若某个 case 需要重测，先删除该 case 相关的本地已落盘数据（对应 `jsonl`、关联 `media`、相关临时文件/状态条目）。
- 清理完成后再按同一流程幂等重跑，不在脏数据基础上直接复测。

### 15.2 Case A：启动后完整列表同步（user4）

目标：
- 启动后可以成功执行列表同步。
- 中断重启后，再次触发同任务可继续（续传或重跑均可），最终落盘正确。

步骤（正常路径）：
1. 启动 App，进入 `user4`。
2. 点击账号区 `List` 按钮。
3. 等待任务完成。

校验：
- `conversations.json` 更新，`items` 非空且 `updatedAt` 变化。
- `sync_state.json.fullSync.phase == \"done\"`。
- `accounts.json/meta.json` 的 `lastSyncAt` 更新。

步骤（中断恢复路径）：
1. 再次触发 `List`，在运行中强制退出 App（或 kill 进程）。
2. 重新打开 App，进入同账号。
3. 再次点击 `List` 触发同任务。

校验：
- 任务可完成，不会卡死。
- `conversations.json.items.id` 无重复。
- `sync_state.json` 最终为 `done`，错误字段为空或已清理。

### 15.3 Case B：单对话同步（无媒体，user4）

目标：
- 单对话无媒体同步可完成。
- 中断后重启再触发同任务，最终 JSONL 不重复。

样本：
- `accountId = cynaustine88_gmail_com`
- `conversationId = 799f497104d1dd8c`

步骤（正常路径）：
1. 删除本地该会话 JSONL（若存在）。
2. 在 UI 触发该会话同步（点击会话项右侧按钮或点击会话后自动触发）。
3. 等待完成。

校验：
- `conversations/{id}.jsonl` 生成并可解析。
- message 行存在，附件数为 0（无媒体）。
- `conversations.json` 对应条目 `messageCount` 更新。

步骤（中断恢复路径）：
1. 再次触发该会话同步，运行中强退 App。
2. 重启后再次触发同会话同步。

校验：
- 最终 JSONL 可解析。
- message 的 `id` 去重后数量等于总行数（无重复消息行）。
- `sync_state.pendingConversations` 中该会话最终不残留运行态条目。

### 15.4 Case C：单对话同步（有媒体，user4）

目标：
- 有媒体会话可完成详情+媒体落盘。
- 中断后重启再触发同任务，媒体与 JSONL 保持一致且去重正确。

样本：
- `accountId = cynaustine88_gmail_com`
- `conversationId = 019445126bb40106`

步骤（正常路径）：
1. 删除本地该会话 JSONL 及其关联媒体文件（若存在）。
2. 触发会话同步，等待完成。

校验：
- `conversations/{id}.jsonl` 存在，附件记录中有图片媒体。
- `media/` 下对应 `mediaId` 文件存在且大小 > 0。
- `conversations.json` 的 `hasMedia=true`，`messageCount` 合理。

步骤（中断恢复路径）：
1. 触发同步，在媒体下载阶段强退 App。
2. 重启后再次触发同会话同步。

校验：
- 最终媒体文件齐全，无 0 字节损坏文件。
- JSONL 中消息/附件无重复堆叠（同消息同媒体不重复落盘）。
- 允许下载请求重复，但最终结果去重正确。

### 15.5 Case D：长对话同步（user1 指定 ID）

目标：
- 长对话（多轮+媒体）同步稳定。
- 中断后重启再触发同任务，最终完整落盘。

样本：
- `accountId = cynaustralineeun_gmail_com`（authuser=1）
- `conversationId = 1b7de7846f0a44c1`

步骤（正常路径）：
1. 删除本地该会话 JSONL 和关联媒体（若存在）。
2. 触发该会话同步，等待完成。

校验：
- JSONL 轮次数明显大于短对话（当前样本约 26 条 message）。
- 附件中包含图片（当前样本约 14 张）。
- `conversations.json` 对应条目计数与时间更新。

步骤（中断恢复路径）：
1. 在会话详情分页或媒体下载过程中强退 App。
2. 重启后再次触发同会话同步。

校验：
- 最终 JSONL 完整可解析，`message.id` 全局唯一。
- 媒体文件可加载，缺失率为 0（或在失败记录中可见并可重试补齐）。
- 最终 UI 展示与本地文件一致，无重复渲染。

### 15.6 通用判定标准

通过标准：
- 每个 case 的“正常路径 + 中断恢复路径”都能完成。
- 不出现任务卡死（前端动画无限转且无状态变化）。
- 数据落盘一致：索引、详情、媒体三者可互相对齐。
- 允许重试导致请求重复，但不允许最终数据重复或损坏。

建议记录：
- 任务开始/结束时间、失败原因。
- 中断时机（列表页码、对话阶段、媒体序号）。
- 重启后再次触发到完成的总耗时。
