# 数据结构

## 目录布局

Tauri 应用数据目录（macOS: `~/Library/Application Support/com.gemini-collector.app/`）

```
{app_data_dir}/
├── accounts.json
└── accounts/
    └── {account_id}/
        ├── meta.json
        ├── conversations.json
        ├── sync_state.json
        ├── conversations/
        │   └── {conv_id}.jsonl
        └── media/
            └── {media_id}.{ext}
```

账号之间数据隔离。每对话一个 JSONL 文件，媒体文件按账号平铺在 `media/` 下。

---

## accounts.json

```typescript
interface AccountRegistry {
  version: 1;
  updatedAt: string;
  accounts: {
    id: string;
    email: string;
    addedAt: string;  // ISO 8601
    dataDir: string;  // "accounts/{id}"
    authuser?: string | null; // Gemini authuser (u/{n})，用于后续拉取
  }[];
}
```

## accounts/{id}/meta.json

```typescript
interface AccountMeta {
  version: 1;
  id: string;
  name: string;
  email: string;
  avatarText: string;
  avatarColor: string;
  conversationCount: number;
  remoteConversationCount: number | null;
  lastSyncAt: string | null;
  lastSyncResult: "success" | "partial" | "failed" | null;
  authuser?: string | null; // Gemini authuser (u/{n})
}
```

## accounts/{id}/conversations.json

```typescript
interface ConversationIndex {
  version: 1;
  accountId: string;
  updatedAt: string;
  totalCount: number;
  items: ConversationSummary[];
}

interface ConversationSummary {
  id: string;
  title: string;
  lastMessage: string;       // 纯文本摘要，最多 80 字符
  messageCount: number;
  hasMedia: boolean;
  status?: string;           // normal | lost | hidden | ...（可扩展）
  updatedAt: string;         // ISO 8601
  syncedAt: string | null;   // null = 仅有索引条目，详情尚未拉取
  remoteHash: string | null;
}
```

## accounts/{id}/conversations/{conv_id}.jsonl

第一行为对话元数据，其余每行为一条消息记录。

```typescript
// 第一行
interface ConvMeta {
  type: "meta";
  id: string;
  accountId: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  syncedAt: string;
  remoteHash: string | null;
}

// 其余行
interface ConvMessage {
  type: "message";
  id: string;
  role: "user" | "model";
  text: string;
  attachments: Attachment[];
  timestamp: string;
  model?: string;    // 仅 role=="model" 时存在，标识所用模型版本
  thinking?: string; // 仅 role=="model" 且有思考过程时存在（界面暂未使用）
}

interface Attachment {
  mediaId: string;  // 对应 media/{mediaId} 文件名（含扩展名）
  mimeType: string;
}
```

## accounts/{id}/sync_state.json

```typescript
interface AccountSyncState {
  version: 1;
  accountId: string;
  updatedAt: string;
  concurrency: number;             // 并发拉取数，默认 3
  fullSync: FullSyncState | null;
  pendingConversations: string[];  // 单对话更新队列
}

interface FullSyncState {
  phase: "listing" | "fetching" | "done" | "failed";
  startedAt: string;
  listingCursor: string | null;
  listingTotal: number | null;
  listingFetched: number;
  conversationsToFetch: string[];
  conversationsFetched: number;
  conversationsFailed: string[];
  completedAt: string | null;
  errorMessage: string | null;
}
```

## 导出格式

```typescript
interface ExportBundle {
  exportVersion: 1;
  exportedAt: string;
  appVersion: string;
  accounts: {
    id: string;
    name: string;
    email: string;
    conversationCount: number;
    lastSyncAt: string | null;
    conversations: {
      id: string;
      title: string;
      createdAt: string;
      updatedAt: string;
      messages: {
        id: string;
        role: "user" | "model";
        text: string;
        attachments: { mediaId: string; mimeType: string }[];
        timestamp: string;
      }[];
    }[];
  }[];
}
```

> 导出不内联媒体二进制，仅保留 `mediaId` 引用。如需携带媒体，将 `media/` 整体打包附带。
