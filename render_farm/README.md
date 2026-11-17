# 动画帧渲染农场（本地可运行）

一个零第三方依赖的分布式帧渲染农场（仅用 Python 3 标准库；自带 PNG 编解码器和
2D 光栅化渲染器）。提交一个固定场景和帧区间后，调度器把它拆成逐帧任务，多个
**本地工作进程**真正渲染像素，上传经过完整校验的图像，最后按时间顺序组成预览。

## 它如何满足关键要求

| 要求 | 实现位置与机制 |
| --- | --- |
| 任务身份含场景版本/帧号/画面设置 | `renderfarm/identity.py`：场景文档做内容哈希得到 `version`；分辨率、SSAA 等设置连同渲染器版本一起做 `settings_hash`；任务键 = `(scene_id, version, frame, settings_hash)` 的哈希 |
| 改过的镜头不能命中旧图 | 场景内容或设置任何一项改变 → 版本/设置哈希改变 → 新任务键，旧 blob 永远无法满足新任务；相同重提交才会复用（`reused_frames`） |
| 可过期租约 + 独立尝试标识 | 每次 `claim` 生成全新 `attempt_id` 和带到期时间的租约；后台 reaper 线程与调度器启动恢复都会回收过期租约 |
| 心跳续租 | worker 心跳线程定期调用 `/api/heartbeat` 延长租约；失联即到期 |
| 完整图像 + 尺寸 + 解码检查才生效 | `storage.FarmStore._validate_and_store`：PNG 签名、每个数据块 CRC、完整 zlib/IDAT 解码、五种过滤器还原、精确期望尺寸，全部通过才原子落盘成为正式帧 |
| 半张图/损坏图被拒 | 返回 422，任务回到队列由**新的尝试**重渲；半图字节不会进入 blob 存储 |
| 失联/重复回报/重启不能让旧尝试覆盖新结果 | 回报只在“该 attempt 仍 active 且归该 worker 且任务仍由其租约持有”时接受；过期或重复的迟到回报统一 409；结果只记录在成功的 attempt 上 |
| 未完成帧明确标记 | 未成功帧返回实时生成的斜纹占位 PNG（不同状态不同配色），HTTP 头 `X-Frame-Status` 标明状态；预览导出 JSON 同时列出 `marked_unfinished` |
| 结果归属可追溯 | 每帧记录成功的 `worker_id`、`attempt_id`、blob sha256、场景版本、设置哈希、全部尝试历史 |
| 不是固定图片轮播 | 每个缩略图都来自调度器逐帧接口；“顺序预览”按帧号拉取真实帧，占位图也是按状态实时生成 |
| 不能只凭文件存在判定有效 | 正式帧只在完整解码+尺寸校验通过后写入；预览导出同样对每张图完整 `decode_png` |

## 目录结构

```
renderfarm/
  identity.py     # 场景版本/设置哈希/任务键
  png.py          # 纯标准库 PNG 编码 + 严格解码（CRC/截断/尺寸校验）
  renderer.py     # 真实 2D 光栅化：渐变背景、地面、运动圆形/矩形、SSAA
  placeholders.py # 未完成帧的状态占位图（生成，非静态素材）
  storage.py      # SQLite 调度内核：提交/租约/心跳/回报/回收/取消/审计
  worker.py       # 本地工作进程：领取→渲染→心跳→上传，支持故障注入
  web.py          # HTTP API + 调度面板页面
  preview.py      # 时间顺序预览拼版（未完成帧带标记）
examples/scene_bounce.json   # 含运动物体的示例场景
tests/test_core.py           # 21 项进程内单元测试
tests/e2e_fault_matrix.py    # 真实子进程的故障矩阵端到端测试
run_demo.sh                  # 一键演示
```

## 界面怎样启动（两步）

需要 Python 3.10+，无需 pip 安装任何东西。

**第 1 步：启动调度器和面板**

```bash
cd render_farm
python3 -m renderfarm.web --host 127.0.0.1 --port 8765 --data-dir ./farm_data
```

浏览器打开 **http://127.0.0.1:8765/** 即“出图任务面板”。

**第 2 步：启动一个或多个本地工作进程**（可在另外的终端、同一台机器开多个）

```bash
python3 -m renderfarm.worker --server http://127.0.0.1:8765 --name node-1
python3 -m renderfarm.worker --server http://127.0.0.1:8765 --name node-2
```

### 面板操作

1. 点“载入示例运动场景”（或粘贴自己的场景 JSON），填帧区间和宽高/SSAA，点“提交帧段”。
   提交被内核拒绝时（帧越界、JSON 非法等），错误直接回填到面板反馈区。
2. “任务”区列出每个提交的场景版本、设置哈希、帧段与状态，open 状态可点“取消任务”。
3. 结果区每 2 秒刷新：时间顺序条带 + 帧缩略图网格，每格显示状态徽章、尝试次数、
   来源 worker、正式 attempt、版本、设置哈希、blob 哈希；未完成帧显示斜纹占位而不是留空。
4. “顺序预览”按钮按帧号顺序高亮轮播**当前真实帧**（再次点击停止）。
5. 工作进程区显示在线/失联（按心跳时间）、完成数、失败数；下方是调度审计事件流。

### 一键演示（含半张图注入与恢复）

```bash
bash run_demo.sh
```

它会清空演示数据目录、起调度器和 3 个 worker（node-A 首次上传故意只传半张图），
提交 12 帧运动场景，等待全部完成后生成 `preview_demo.png`。

## 故障注入（用于验证恢复过程）

worker 读取环境变量 `RENDERFARM_FAULT`：

| 值 | 行为 |
| --- | --- |
| `crash:<frame>` | 渲染后进程直接 `os._exit`，模拟节点崩溃；租约到期后任务被回收重派 |
| `halfupload:<frame>` | 只上传 PNG 的前半段字节，调度器解码失败返回 422，任务重新排队（`-1` 表示第一个领到的任务就触发） |
| `failreport:<frame>` | 回报一次失败后恢复正常 |
| `stall:<frame>` | 冻结心跳直到租约过期，再尝试用旧 attempt 上报 → 被 409 拒绝 |

## 恢复过程说明

- **节点失联/崩溃**：租约到期 → reaper（每秒）或调度器启动时把任务置回 `pending`，
  旧 attempt 标记 `superseded`；新 worker 领取生成新 attempt。
- **迟到/重复回报**：旧 attempt 已失效，成功和失败回报都返回 409，数据库状态不变，
  新结果不会被覆盖。
- **调度器重启**：状态全部在 SQLite（WAL）和内容寻址 blob 文件中；启动即执行
  `reap_expired()` 回收所有未完成租约，已完成帧继续可用。
- **取消任务**：pending/leased 帧置 `cancelled` 并释放租约；已成功帧保留。
- 超过 10 次尝试的任务置 `failed`，在面板与预览中明确标红。

## 测试

```bash
# 快速单元测试（PNG 编解码、身份哈希、渲染、租约/半图/过期/重启逻辑）
python3 -m unittest tests.test_core -v

# 完整端到端：真实调度器 + 多个故障 worker 子进程 + 调度器中途重启
python3 tests/e2e_fault_matrix.py
```

端到端测试覆盖：崩溃恢复、半图拒绝后重试、失败回报、失联旧尝试 409、重复回报幂等、
编辑后新版本不命中旧图、设置哈希隔离、取消标记、重启恢复，并对最终 12 帧逐张完整解码、
校验尺寸、确认相邻帧像素不同且无重复/错帧。

## 主要 HTTP API

| 方法与路径 | 作用 |
| --- | --- |
| `POST /api/submissions` | 提交场景+帧段+设置，返回版本/设置哈希/新建与复用帧 |
| `DELETE /api/submissions/{id}/cancel` | 取消提交 |
| `GET  /api/submissions/{id}` | 任务列表、状态汇总、每帧归属与尝试历史 |
| `GET  /api/submissions/{id}/frames/{n}` | 正式帧 PNG；未完成时为占位图（状态在 `X-Frame-Status`） |
| `POST /api/claim` | worker 领取任务（返回 attempt_id、租约到期时间、场景文档） |
| `POST /api/heartbeat` | 续租 |
| `POST /api/attempts/{id}/success` | 上传完整 PNG（`image/png`，worker 经 `X-Worker-Id` 标识） |
| `POST /api/attempts/{id}/fail` | 失败回报 |
| `GET  /api/workers`、`/api/events` | 节点与审计事件 |
