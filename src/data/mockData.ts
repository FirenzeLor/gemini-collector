export interface Attachment {
  type: "image" | "video";
  url: string;        // remote URL or absolute local path
  name?: string;
  isLocal?: boolean;  // true = local file, needs convertFileSrc
  mimeType?: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  date: string;
  model?: string;
  attachments?: Attachment[];
}

export interface Conversation {
  id: string;
  title: string;
  lastMessage: string;
  updatedAt: string;
  updatedTime: string;
  messages: Message[];
}

import sessionData from "./session_c8d0a41aba76e19e.json";
import largeSessionData from "./session_large_test.json";
import chatListData from "./chat_list_test.json";

const realSession = sessionData as unknown as Conversation;
const largeSession = largeSessionData as unknown as Conversation;
const chatListConvs = (chatListData as { conversations: Conversation[] }).conversations;

export const mockConversations: Conversation[] = [
  largeSession,
  realSession,
  ...chatListConvs,
  {
    id: "c_001",
    title: "React Hooks 最佳实践",
    lastMessage: "useCallback 和 useMemo 应该谨慎使用...",
    updatedAt: "2026-02-19",
    updatedTime: "14:36",
    messages: [
      {
        id: "m1",
        role: "user",
        content: "能帮我解释一下 React 中 useCallback 和 useMemo 的区别吗？",
        timestamp: "14:32",
        date: "2026-02-19",
      },
      {
        id: "m2",
        role: "assistant",
        content: `## useCallback vs useMemo

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
        timestamp: "14:33",
        date: "2026-02-19",
        model: "2.0 Flash",
      },
      {
        id: "m3",
        role: "user",
        content: "那什么时候应该用 useCallback？给个具体场景",
        timestamp: "14:35",
        date: "2026-02-19",
        attachments: [
          { type: "image", url: "https://picsum.photos/seed/img1/400/400", name: "screenshot1.png" },
          { type: "image", url: "https://picsum.photos/seed/img2/400/400", name: "screenshot2.png" },
          { type: "image", url: "https://picsum.photos/seed/img3/400/400", name: "screenshot3.png" },
        ],
      },
      {
        id: "m4",
        role: "assistant",
        content: `好的，一个典型场景是**配合 React.memo 使用**：

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

  // ✅ 没有 useCallback：每次 text 变化，handleSubmit 都是新函数引用
  //    → Button 会不必要地重渲染

  // ✅ 有 useCallback：只有依赖项变化才更新
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
        timestamp: "14:36",
        date: "2026-02-19",
        model: "2.0 Flash",
      },
    ],
  },
  {
    id: "c_002",
    title: "Python 异步编程",
    lastMessage: "asyncio.gather 可以并发执行多个协程...",
    updatedAt: "2026-02-18",
    updatedTime: "10:17",
    messages: [
      {
        id: "m1",
        role: "user",
        content: "asyncio.gather 和 asyncio.wait 有什么区别？",
        timestamp: "10:15",
        date: "2026-02-18",
      },
      {
        id: "m2",
        role: "assistant",
        content: `## asyncio.gather vs asyncio.wait

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
        timestamp: "10:17",
        date: "2026-02-18",
        model: "1.5 Pro",
      },
    ],
  },
  {
    id: "c_003",
    title: "Rust 所有权系统",
    lastMessage: "借用检查器在编译期保证内存安全...",
    updatedAt: "2026-02-17",
    updatedTime: "09:03",
    messages: [
      {
        id: "m1",
        role: "user",
        content: "能用简单的例子解释 Rust 的所有权规则吗？",
        timestamp: "09:00",
        date: "2026-02-17",
      },
      {
        id: "m2",
        role: "assistant",
        content: `## Rust 所有权三原则

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
        timestamp: "09:03",
        date: "2026-02-17",
        model: "2.0 Pro",
      },
    ],
  },
  {
    id: "c_004",
    title: "数据库索引优化",
    lastMessage: "复合索引的列顺序很关键...",
    updatedAt: "2026-02-16",
    updatedTime: "16:23",
    messages: [
      {
        id: "m1",
        role: "user",
        content: "如何为慢查询创建合适的索引？",
        timestamp: "16:20",
        date: "2026-02-16",
      },
      {
        id: "m2",
        role: "assistant",
        content: `## 慢查询索引优化步骤

### 1. 先用 EXPLAIN 分析

\`\`\`sql
EXPLAIN SELECT * FROM orders
WHERE user_id = 123 AND status = 'pending'
ORDER BY created_at DESC;
\`\`\`

关注 \`type\` 列：\`ALL\` = 全表扫描（需要优化），\`ref\`/\`range\` = 已用索引。

### 2. 复合索引列顺序原则

遵循**最左前缀**原则，同时按选择性排序：

- **等值条件**的列放前面
- **范围条件**的列放后面
- **ORDER BY** 的列尽量纳入索引

\`\`\`sql
-- ✅ 针对上面查询的优化索引
CREATE INDEX idx_orders_user_status_time
ON orders(user_id, status, created_at);
\`\`\`

### 3. 验证效果

再次 EXPLAIN 确认 \`type\` 变为 \`ref\` 或更好。`,
        timestamp: "16:23",
        date: "2026-02-16",
        model: "1.5 Flash",
      },
    ],
  },
  {
    id: "c_005",
    title: "Docker 多阶段构建",
    lastMessage: "最终镜像只包含运行时必要文件...",
    updatedAt: "2026-02-15",
    updatedTime: "11:40",
    messages: [],
  },
  {
    id: "c_006",
    title: "TypeScript 高级类型",
    lastMessage: "条件类型和映射类型的组合使用...",
    updatedAt: "2026-02-14",
    updatedTime: "20:15",
    messages: [],
  },
  {
    id: "c_007",
    title: "系统设计：消息队列",
    lastMessage: "Kafka 的 partition 机制保证顺序消费...",
    updatedAt: "2026-02-13",
    updatedTime: "08:52",
    messages: [],
  },
  {
    id: "c_008",
    title: "CSS Grid 布局技巧",
    lastMessage: "grid-template-areas 让布局更直观...",
    updatedAt: "2026-02-12",
    updatedTime: "19:07",
    messages: [],
  },
];

export interface Account {
  id: string;
  name: string;
  email: string;
  avatarText: string;
  avatarColor: string;
  conversationCount: number;
  lastSync: string;
}

export const mockAccounts: Account[] = [
  {
    id: "acc_1",
    name: "user1",
    email: "user1@example.com",
    avatarText: "R",
    avatarColor: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
    conversationCount: 8,
    lastSync: "2026-02-19",
  },
  {
    id: "acc_2",
    name: "work_account",
    email: "work@company.com",
    avatarText: "W",
    avatarColor: "linear-gradient(135deg, #f093fb 0%, #f5576c 100%)",
    conversationCount: 23,
    lastSync: "2026-02-18",
  },
  {
    id: "acc_3",
    name: "alice_dev",
    email: "alice@example.com",
    avatarText: "A",
    avatarColor: "linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)",
    conversationCount: 5,
    lastSync: "2026-02-15",
  },
];
