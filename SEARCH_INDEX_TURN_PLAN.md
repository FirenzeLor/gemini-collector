# 全局搜索索引方案（Turn 级回源）

## 1. 目标与边界

- 目标：
  - 本地毫秒级检索（优先 p50 2~8ms，p99 < 20ms）。
  - 支持增量更新。
  - 允许空间换时间。
  - 搜索结果可定位到源数据具体 `turn`。
- 数据边界：
  - 仅处理 app 内格式：`accounts/{id}/conversations/{conv_id}.jsonl` + `media/`。
  - 文本只索引 `user/model` 对话文本，忽略 `thinking`。

## 2. 数据口径与 Turn 还原

- 每个会话 JSONL：
  - 第一行 `type="meta"`。
  - 后续行为 `type="message"`。
- `message.id` 采用 `{turn_id}_u` / `{turn_id}_m` 规则，可直接恢复 `turn_id`。
- 因此结果可以稳定回源到：
  - `accountId + conversationId + turnId + role(user|model) + messageId`。

## 3. 索引架构

- 引擎：Tantivy（Rust，本地嵌入，适合 Tauri）。
- 粒度：
  - 回源粒度：`turn`。
  - 建索引粒度：`turn-side-chunk`（对 `user/model` 文本分块）。
- 分块建议：
  - `chunk_size = 768`，`overlap = 96`（默认）。
  - 长文本不会拖慢检索，且命中片段定位更稳定。

## 4. 文档 Schema

- 标识字段（keyword）：
  - `account_id`
  - `conversation_id`
  - `turn_id`
  - `message_id`
  - `role` (`user` / `model`)
- 过滤/排序字段（fast）：
  - `timestamp`（消息时间）
  - `updated_at`（会话更新时间）
- 定位字段（stored/fast）：
  - `chunk_idx`
  - `char_start`
  - `char_end`
- 检索字段：
  - `text_word`：英文/空格语言词法检索。
  - `text_ngram`：2~4 gram，覆盖中文与无空格词检索。
  - `chunk_text_stored`：用于 snippet 与二次精确校验。

## 5. 查询流程

1. 预处理 query（大小写、空白、全半角归一化）。
2. 在 `text_word + text_ngram` 做混合检索（BM25，可设字段权重）。
3. 取 topN chunk（如 200）后做二次 `contains` 精确校验，拿真实偏移。
4. 按 `account_id + conversation_id + turn_id` 聚合成 turn 结果并重排。
5. 返回 turn 级结果，包含可直接跳转的主键与命中片段。

## 6. 返回结构（建议）

```json
{
  "accountId": "acc_xxx",
  "conversationId": "conv_xxx",
  "turnId": "turn_xxx",
  "matchedRoles": ["user", "model"],
  "messageIds": ["turn_xxx_u", "turn_xxx_m"],
  "hits": [
    {
      "role": "model",
      "start": 123,
      "end": 137,
      "snippet": "...关键词上下文..."
    }
  ],
  "score": 12.34,
  "timestamp": "2026-01-17T22:35:47+00:00"
}
```

## 7. 增量更新机制

- 维护 `index_manifest.json`（按会话记录）：
  - `account_id`
  - `conversation_id`
  - `file_size`
  - `mtime_ns`
  - `remote_hash`
  - `indexed_at`
  - `doc_count`
- 更新判定：
  - 任一签名变化即判定会话变更。
- 更新策略：
  - 删除该会话旧索引文档（按 `account_id + conversation_id`）。
  - 重新解析该会话 JSONL 并写入新文档。
  - commit 后 reload searcher。
- 说明：
  - 当前同步会重写会话文件，不宜依赖“文件尾追加索引”。

## 8. 一致性与恢复

- 单 writer 线程串行写入，多 reader 并发查询。
- crash recovery：
  - 启动时比对 manifest 与文件签名，补索引缺口。
- 可选优化：
  - 按账号或时间做分片目录，降低大规模重建影响。

## 9. 当前规模下的定标（已评估）

- 现有基线（app + user0 合并口径）：
  - `372` conversations
  - `6,324` messages（仅 user/model 文本）
  - `3,162` turns
  - `4,847,524` text chars
- 分块文档数（基线）：
  - `chunk=512, overlap=96` -> `12,632` docs
  - `chunk=768, overlap=96` -> `8,415` docs
  - `chunk=1024, overlap=96` -> `6,699` docs

## 10. 方案决策

- 默认参数：
  - 索引粒度：`turn-side-chunk`
  - 分块：`768/96`
  - 双通道索引：`word + ngram`
  - 查询后二次精确校验 + turn 聚合
- 该方案满足：
  - 本地毫秒级检索
  - 可回源到具体 turn
  - 可增量更新
  - 接受空间换时间
