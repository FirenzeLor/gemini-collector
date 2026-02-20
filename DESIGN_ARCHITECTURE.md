# 设计架构

## 整体架构

```
Gemini API
    │
    ▼
同步引擎（Rust/Tauri commands）
    ├── 列表拉取（listing）
    └── 详情拉取（并发 fetching，默认 concurrency=3）
           │
           ▼
     本地文件系统
     ├── conversations.json（索引）
     ├── {conv_id}.jsonl（详情）
     └── media/（媒体文件）
           │
           ▼
     React 前端
     ├── Sidebar（读索引）
     └── ChatView（读 JSONL）
```

## 关键设计决策

| 决策 | 原因 |
|---|---|
| 索引与详情分离 | 侧边栏只读 `conversations.json`（数百KB），不加载消息体 |
| JSONL 格式 | 追加写入友好，支持流式读取；第一行 meta 保持文件自描述 |
| 媒体文件账号级平铺 | 路径规则简单：`media/{mediaId}.{ext}`，唯一性由同步保证 |
| `remoteHash` 增量判断 | 比对 hash 决定是否跳过拉取，避免重复下载未变更对话 |
| 两阶段断点续传 | listing 用 page cursor 续传，fetching 用待处理队列续传 |
| `concurrency` 配置 | 默认值 3（保守），可按账号调整，存在 sync_state.json |
| JSON 不用 SQLite | 导出友好，实现简单，当前访问模式无需复杂查询 |

## 同步流程

### 全量同步

```
1. 写入 sync_state: phase="listing"
2. 循环拉取列表（带 cursor 断点）→ 写入 conversations.json
3. 写入 sync_state: phase="fetching"，填充 conversationsToFetch
4. 并发（concurrency=N）拉取各对话详情 → 写入 {conv_id}.jsonl
5. 每完成一个从 conversationsToFetch 移除，失败的放入 conversationsFailed
6. 完成后写入 sync_state: phase="done"，更新 meta.json
```

### 增量同步

```
1. 拉取列表，对每条目对比 remoteHash
2. Hash 相同 → 跳过
3. Hash 不同 / syncedAt=null → 加入拉取队列
4. 后续流程同全量 fetching 阶段
```

### 断点续传

```
重启后读取 sync_state.json：
- phase="listing" → 从 listingCursor 继续拉列表
- phase="fetching" → 直接进入详情拉取，conversationsToFetch 即待处理队列
- conversationsFailed → 优先重试
```

## 前端数据流

```
AccountPicker
  └─ 读 accounts.json

Sidebar
  └─ 读 conversations.json（轻量索引，一次加载全量）
       → ConversationSummary[] → 渲染列表

ChatView
  └─ 点击对话时读 {conv_id}.jsonl
       → 解析逐行 → ConvMeta + ConvMessage[]
       → 媒体附件通过 Tauri asset 协议按需加载 media/{mediaId}
```

## 与现有 mockData.ts 的映射

| 现有字段 | 变更 |
|---|---|
| `Message.role: "assistant"` | 存储层改 `"model"`，UI 层渲染时映射回 |
| `Message.content: string` | 拆分为 `text` + `attachments[]` |
| `Message.timestamp: "14:32"` | 改为完整 ISO 8601 |
| `Conversation.updatedAt + updatedTime` | 合并为单一 ISO 8601 |
| `Conversation.messages[]` | 从索引移除，仅保留 `messageCount` |

**前端类型建议：** 将 `src/data/mockData.ts` 拆分为 `src/types/storage.ts`（存储 schema）和 `src/types/ui.ts`（UI ViewModel），通过适配函数转换。

## 涉及文件

| 文件 | 变更内容 |
|---|---|
| `src/data/mockData.ts` | 拆分重构为独立类型文件 |
| `src/components/Sidebar.tsx` | 改为消费 `ConversationSummary[]` |
| `src/components/ChatView.tsx` | 改为解析 JSONL，媒体附件按需加载 |
| `src-tauri/src/lib.rs` | 新增文件读写、JSONL 操作、同步状态管理 commands |
| `src-tauri/Cargo.toml` | 添加 `tauri-plugin-fs` |
| `src-tauri/tauri.conf.json` | 配置 fs 权限范围（限定 `$APPDATA`） |
