# 性能修復說明

## 問題一：大對話虛擬滾動

**問題**：360 條消息全量渲染 → DOM 節點過多，滾動卡顿。

**方案**：`react-virtuoso` 替換 `messages.map()`

| 項目 | 舊方案 | 新方案 |
|------|--------|--------|
| 渲染節點 | 全部 | 僅可視區 ~10 條 |
| 自動滾底 | `scrollIntoView` | `followOutput="smooth"` |
| 動態高度 | 無需 | 原生支持 |

**修改文件**：`ChatView.tsx`，見 `<Virtuoso>` 替換 `messages.map()`

---

## 問題二：Model 空值顯示

**問題**：`model` 字段存在 `""` 空字符串，當前 `&&` 判斷無法攔截。

**修改**：時間戳區域條件由 `message.model &&` 改為 `message.model || null`，fallback 顯示 `"Gemini"`。

**修改文件**：`ChatView.tsx` L280 附近
