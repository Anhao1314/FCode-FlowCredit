# Chat 侧栏分页／归档浏览器验收记录

本轮实际执行时间：2026-10-27。使用隔离的临时数据目录与受控分页代理，
没有读写用户现有的 `chat.sqlite` / `runs`，也没有调用真实付费模型。

## 启动隔离环境

```bash
rm -rf /tmp/fcode-pagination
mkdir -p /tmp/fcode-pagination/runs /tmp/fcode-pagination/config

cd <repo>/viewer
FCODE_RUNS_DIR=/tmp/fcode-pagination/runs \
FCODE_CONFIG_DIR=/tmp/fcode-pagination/config \
FCODE_CHAT_MOCK_DELAY=5.0 \
../.venv-desktop/bin/python server.py --repo .. --port 8642

python3 tests/browser/pagination_proxy.py
```

代理监听 `127.0.0.1:8643`，把所有请求转发给本机 `8642`，只接管：

- `GET /api/chat/threads`：5 条受控服务端线程，每页固定返回 2 条；
- `DELETE /api/chat/threads/vol-*`：从受控列表移除；
- `GET /__control?delay_ms=3000&once=1`：延迟下一次线程列表响应，用于制造在途看似旧响应。

其余请求（含 `/api/chat/send`）都流式转发到真实本地 FCode 服务。浏览器
`localStorage` 固定 `fa.chatModel = {"key":"mock","touched":true}`，
因此对话回答来自离线 mock（页面显示“离线 Mock 回复 … 0 µUSD”）。

## 实际步骤与观察

1. 打开 `http://127.0.0.1:8643/`。
   - 侧栏：`vol-1`、`vol-2` 两条；按钮“显示全部 · 还有 3 条”。
   - 代理记录：`offset=0 -> vol-1, vol-2, total=5`。

2. 发送“本地流式验收会话”，在流式尚未结束时点击“显示全部”。
   - 侧栏出现本地线程 `chat-20261027-145828-1e5f`，同时有 `vol-1..vol-4`。
   - 按钮仍显示“还有 1 条”，没有因为本地占位变成 0。
   - 代理记录：`offset=2 -> vol-3, vol-4, total=5`；没有重复 id。

3. 归档已加载服务端线程 `vol-1`。
   - 列表变为本地线程 + `vol-2/3/4`；剩余仍为 1。
   - 代理记录：`[archive] vol-1 total 5->4`。

4. 再加载下一页。
   - 代理记录：`offset=3 -> vol-5, total=4`；没有从 4 开始而跳过 `vol-5`。
   - 列表变为本地线程 + `vol-2/3/4/5`；剩余 0，按钮消失。

5. 归档本地流式占位 `chat-20261027-145828-1e5f`。
   - 列表只剩 `vol-2/3/4/5`，剩余仍为 0，按钮保持隐藏。
   - 随后停止一条新流触发 reset，归档占位没有重新出现。

6. 在 reset 尚未返回时归档（受控延迟 3000ms）。
   - 代理先收到 reset（快照 `vol-2,vol-3`，total 4），延迟期间归档 `vol-2`。
   - 旧 reset 返回后，列表没有恢复 `vol-2`；最终为 `vol-3/4`，剩余 1。
   - 代理记录：
     ```
     [archive] vol-2 total 4->3
     [threads] offset=0 delay=0  -> vol-3, vol-4, total=3
     [threads] offset=0 delay=3000 -> vol-2, vol-3, total=4
     ```
     最后一条是延迟的旧快照，被客户端丢弃。

全程 `window.__errs` 为空；没有调用 Kimi/DeepSeek 等付费模型。