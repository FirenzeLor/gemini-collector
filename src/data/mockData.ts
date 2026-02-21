// Interfaces match DATA_STRUCTURE.md exactly.

export interface Attachment {
  mediaId: string;   // filename with extension in media/ dir
  mimeType: string;
}

export interface ConvMessage {
  type: "message";
  id: string;
  role: "user" | "model";
  text: string;
  attachments: Attachment[];
  timestamp: string;   // ISO 8601
  model?: string;      // only when role=="model"
  thinking?: string;   // only when role=="model" and thinking exists
}

export interface Conversation {
  id: string;
  accountId: string;
  title: string;
  createdAt: string;   // ISO 8601
  updatedAt: string;   // ISO 8601
  syncedAt: string;    // ISO 8601
  remoteHash: string | null;
  messages: ConvMessage[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  lastMessage: string;      // plain text, max 80 chars
  messageCount: number;
  hasMedia: boolean;
  updatedAt: string;        // ISO 8601
  syncedAt: string | null;
  remoteHash: string | null;
}

export interface Account {
  id: string;
  name: string;
  email: string;
  avatarText: string;
  avatarColor: string;
  conversationCount: number;
  remoteConversationCount: number | null;
  lastSyncAt: string | null;  // ISO 8601
  lastSyncResult: "success" | "partial" | "failed" | null;
  authuser?: string | null;
}

export const mockConversations: Conversation[] = [
  {
    id: "c_001",
    accountId: "acc_1",
    title: "React Hooks 最佳实践",
    createdAt: "2026-02-19T14:30:00Z",
    updatedAt: "2026-02-19T14:36:00Z",
    syncedAt: "2026-02-19T15:00:00Z",
    remoteHash: "abc123",
    messages: [
      {
        type: "message",
        id: "turn1_u",
        role: "user",
        text: "能帮我解释一下 React 中 useCallback 和 useMemo 的区别吗？",
        attachments: [],
        timestamp: "2026-02-19T14:32:00Z",
      },
      {
        type: "message",
        id: "turn1_m",
        role: "model",
        text: `## useCallback vs useMemo

这两个 Hook 都用于性能优化，但用途不同：

### useCallback
**用于缓存函数引用**，防止子组件因父组件重渲染而不必要地重新渲染。

\`\`\`tsx
const handleClick = useCallback(() => {
  doSomething(id);
}, [id]); // 依赖项变化时才重新创建
\`\`\`

### useMemo
**用于缓存计算结果**，避免每次渲染都执行昂贵的计算。

\`\`\`tsx
const sortedList = useMemo(() => {
  return items.sort((a, b) => a.name.localeCompare(b.name));
}, [items]);
\`\`\`

### 核心区别

| Hook | 缓存对象 | 返回值 |
|------|---------|--------|
| \`useCallback(fn, deps)\` | 函数本身 | 缓存的函数 |
| \`useMemo(fn, deps)\` | 函数返回值 | 计算结果 |

> **注意**：过度使用这两个 Hook 反而会降低性能，应该在真正有性能问题时再考虑使用。`,
        attachments: [],
        timestamp: "2026-02-19T14:33:00Z",
        model: "Gemini 2.0 Flash",
      },
      {
        type: "message",
        id: "turn2_u",
        role: "user",
        text: "那什么时候应该用 useCallback？给个具体场景",
        attachments: [],
        timestamp: "2026-02-19T14:35:00Z",
      },
      {
        type: "message",
        id: "turn2_m",
        role: "model",
        text: `好的，一个典型场景是**配合 React.memo 使用**：

\`\`\`tsx
// 子组件用 memo 包裹，避免不必要的重渲染
const Button = React.memo(({ onClick, label }: Props) => {
  console.log("Button 渲染了");
  return <button onClick={onClick}>{label}</button>;
});

// 父组件
function Parent() {
  const [count, setCount] = useState(0);
  const [text, setText] = useState("");

  const handleSubmit = useCallback(() => {
    submitForm(text);
  }, [text]);

  return (
    <>
      <input value={text} onChange={e => setText(e.target.value)} />
      <p>Count: {count}</p>
      <button onClick={() => setCount(c => c + 1)}>+</button>
      <Button onClick={handleSubmit} label="提交" />
    </>
  );
}
\`\`\`

当 \`count\` 变化时，没有 \`useCallback\` 的话 \`Button\` 也会重渲染（即使 \`text\` 没变）。这才是 \`useCallback\` 真正发挥作用的地方。`,
        attachments: [],
        timestamp: "2026-02-19T14:36:00Z",
        model: "Gemini 2.0 Flash",
      },
    ],
  },
  {
    id: "c_002",
    accountId: "acc_1",
    title: "Python 异步编程",
    createdAt: "2026-02-18T10:00:00Z",
    updatedAt: "2026-02-18T10:17:00Z",
    syncedAt: "2026-02-18T11:00:00Z",
    remoteHash: "def456",
    messages: [
      {
        type: "message",
        id: "turn1_u",
        role: "user",
        text: "asyncio.gather 和 asyncio.wait 有什么区别？",
        attachments: [],
        timestamp: "2026-02-18T10:15:00Z",
      },
      {
        type: "message",
        id: "turn1_m",
        role: "model",
        text: `## asyncio.gather vs asyncio.wait

### asyncio.gather

- **更简洁**，直接返回所有结果列表
- 任意一个任务抛出异常，默认会传播到调用方
- 保持结果顺序与输入顺序一致

\`\`\`python
results = await asyncio.gather(
    fetch_user(1),
    fetch_user(2),
    fetch_user(3),
)
# results = [user1, user2, user3]
\`\`\`

### asyncio.wait

- **更灵活**，可以设置等待条件（\`FIRST_COMPLETED\`, \`FIRST_EXCEPTION\`, \`ALL_COMPLETED\`）
- 返回两个集合：\`done\` 和 \`pending\`
- 适合需要"哪个先完成就先处理哪个"的场景

\`\`\`python
tasks = {asyncio.create_task(fetch(url)) for url in urls}
done, pending = await asyncio.wait(
    tasks,
    return_when=asyncio.FIRST_COMPLETED
)
\`\`\`

**简单并发用 gather，需要精细控制用 wait。**`,
        attachments: [],
        timestamp: "2026-02-18T10:17:00Z",
        model: "Gemini 1.5 Pro",
      },
    ],
  },
  {
    id: "c_003",
    accountId: "acc_1",
    title: "Rust 所有权系统",
    createdAt: "2026-02-17T09:00:00Z",
    updatedAt: "2026-02-17T09:03:00Z",
    syncedAt: "2026-02-17T10:00:00Z",
    remoteHash: "ghi789",
    messages: [
      {
        type: "message",
        id: "turn1_u",
        role: "user",
        text: "能用简单的例子解释 Rust 的所有权规则吗？",
        attachments: [],
        timestamp: "2026-02-17T09:00:00Z",
      },
      {
        type: "message",
        id: "turn1_m",
        role: "model",
        text: `## Rust 所有权三原则

1. 每个值都有唯一的**所有者**
2. 同一时间只能有**一个所有者**
3. 所有者离开作用域，值被**自动释放**

\`\`\`rust
fn main() {
    let s1 = String::from("hello"); // s1 是所有者
    let s2 = s1;                    // 所有权转移给 s2
    // println!("{}", s1);          // ❌ 编译错误！s1 已无效
    println!("{}", s2);             // ✅
}
\`\`\`

### 借用

不转移所有权，只借用引用：

\`\`\`rust
fn print_len(s: &String) {  // 借用，不获取所有权
    println!("长度: {}", s.len());
}

let s = String::from("hello");
print_len(&s);  // 传引用
println!("{}", s);  // ✅ s 仍然有效
\`\`\`

这套规则让 Rust 无需 GC 就能保证内存安全。`,
        attachments: [],
        timestamp: "2026-02-17T09:03:00Z",
        model: "Gemini 2.0 Pro",
      },
    ],
  },
  {
    id: "c_004",
    accountId: "acc_1",
    title: "数据库索引优化",
    createdAt: "2026-02-16T16:00:00Z",
    updatedAt: "2026-02-16T16:23:00Z",
    syncedAt: "2026-02-16T17:00:00Z",
    remoteHash: null,
    messages: [],
  },
  {
    id: "c_005",
    accountId: "acc_1",
    title: "Docker 多阶段构建",
    createdAt: "2026-02-15T11:00:00Z",
    updatedAt: "2026-02-15T11:40:00Z",
    syncedAt: "2026-02-15T12:00:00Z",
    remoteHash: null,
    messages: [],
  },
];

export function conversationToSummary(conv: Conversation): ConversationSummary {
  const lastMsg = conv.messages[conv.messages.length - 1];
  return {
    id: conv.id,
    title: conv.title,
    lastMessage: lastMsg ? lastMsg.text.replace(/\n/g, " ").slice(0, 80) : "",
    messageCount: conv.messages.length,
    hasMedia: conv.messages.some((m) => m.attachments.length > 0),
    updatedAt: conv.updatedAt,
    syncedAt: conv.syncedAt,
    remoteHash: conv.remoteHash,
  };
}

export const mockConversationSummaries: ConversationSummary[] =
  mockConversations.map(conversationToSummary);
