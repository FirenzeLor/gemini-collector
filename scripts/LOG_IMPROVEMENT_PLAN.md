# 日志改造方案

涉及文件：`gemini_export.py`、`gemini_worker.py`，不动 standalone。

## 改动点

### A. 媒体下载时间 + 体积
位置：`_download_media_batch_no_cdp`

当前 `timing_log` 有耗时但无体积，改为：
```
[timing] media id=abc.jpg size=84.3KB status=ok elapsed=412ms delay=320ms
[timing] media id=xyz.jpg size=210.1KB status=skip_exists elapsed=0ms
```
- 下载成功：`size = len(content) / 1024`
- skip_exists：`size = filepath.stat().st_size / 1024`

### B. 随机暂停时间合并到 timing_log
位置：`_before_request` + 各 `timing_log` 调用点

- `_before_request` 将 delay 存入 `self._last_delay_sec`（首次请求为 0）
- 删除独立 `[delay] ... 随机暂停: Xs` 打印行
- `_batchexecute`、`get_chats_page`、`get_chat_detail_page` 的 `timing_log` 调用加 `delay=` 字段

效果：每次 API 请求减少 1 行 `[delay]`，信息保留在 `[timing]` 行。

### C. 对话详情分 page 耗时
位置：`sync_single_conversation` page 循环

- 循环头记 `page_start = time.perf_counter()`
- 现有 `第N页: M轮(累计X)` 行追加耗时：
  ```
    第1页: 8轮(累计8) 340ms
  ```

### D. 单会话完成汇总行
位置：`sync_single_conversation` 末尾（替换原4行媒体统计）

- 在函数入口记 `conv_start = time.perf_counter()`，分别记录文本阶段和媒体阶段耗时
- 原4行（媒体下载/失败/预览生成/预览失败）合并为1行：
  ```
  [*] 单会话完成: turns=15 media=3(img)/1(vid) text=1.2s media_dl=3.4s total=4.6s
  ```

### E. 增量同步进度 N/total
位置：`export_incremental` 外层循环（L3516）

当前无进度，改为：
```
[5/233] 增量检查: 会话标题 (c_xxx)
```
`total = len(chats)`，在循环外已知。

## gemini_worker.py 改动点

纯格式/可读性，不新增数据。

### F. 进度标签过长
位置：`_sync_conversation_batch` L354/360/394

```python
# 当前
self._log(phase, f"当前对话任务列表更新进度: {idx}/{total} (success={len(succeeded)}, failed={len(failed)}, cid={cid})")
# 改为
self._log(phase, f"进度: {idx}/{total} ok={len(succeeded)} fail={len(failed)} cid={cid}")
```

### G. 主循环日志格式不一致
位置：L614

```python
# 当前（与其他 [worker:phase] 格式不符）
print(f"[worker] request json parse error: {exc}", file=sys.stderr)
# 改为
print(f"[worker:stdin] JSON 解析错误: {exc}", file=sys.stderr)
```

---

## 净效果（以 15 轮 / 3 媒体对话为例）

| 变化 | 行数 |
|------|------|
| 删除 `[delay]` 行（gemini_export.py） | -6 |
| 4 行媒体统计 → 1 行汇总（gemini_export.py） | -3 |
| 新增 per-page 耗时（字段追加，不增行） | 0 |
| 新增单会话汇总行 | +1 |
| worker 进度标签缩短（不增减行数） | 0 |
| **合计** | **-8 行 / 次** |
