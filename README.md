# document-archive-engine-python

带生命周期管理的文档归档引擎：保留策略驱动到期，过期文档自动归档，
命中法务保全的文档免于销毁，执行引擎分批、并发、可恢复。

## 生命周期语义

```
active --(到期, 认领)--> archiving --(归档提交)--> archived --(销毁扫描)--> pending_destruction --> destroyed
  ^                      |
  |----(失败/取消/超时回滚)--|
```

- **active（生效）**：文档可被正常读写；到期后等待归档。
- **archiving（归档中）**：执行引擎已认领、正在写归档。崩溃只会停在此状态，
  重启后由 `recover_inflight()` 回滚为 `active` 重新处理。
- **archived（已归档）**：原文已写入归档存储并带 sha256 校验；
  状态与 `archive_id` 在同一原子更新中提交。
- **pending_destruction（待销毁）**：销毁前的中间态，删除操作幂等，
  重启后由下一次销毁扫描继续完成。
- **destroyed（已销毁）**：终态。

一致性保证：

- 所有状态流转通过条件更新（`UPDATE ... WHERE status=<期望值>`）完成，
  并发执行者中只有一个能认领成功，同一文档不会被重复处理。
- 多步写操作在单事务内提交，读取方任意时刻只能看到完整的已提交状态，
  不存在"状态已推进但 archive_id 未写入"之类的中间态。
- 处理结果写入 append-only 的 `results` 表，历史结果不可被追溯改写。

## 保留策略

- 策略按 `retain_days` 定义保留期，以**递增版本号**更新（版本回退会被拒绝）。
- 策略变更后，仅对仍处于 `active` 的文档按新策略重算 `expires_at`
  （基于原 `created_at`，不随重算时刻漂移）；已进入归档/销毁流程或已完结的
  文档及其历史结果不受影响。

## 法务保全

- `place_hold` / `release_hold` 只打标记，不改变文档状态。
- 命中保全的文档被排除在自动销毁之外，销毁结果记录为 `LEGAL_HOLD`。
- 保全解除后文档仍在原状态，下一次扫描自然重新进入原进度，
  不跳过也不重复处理。

## 执行引擎

- **分批推进**：按 `doc_id` 游标分页扫描，每批最多 `batch_size` 个文档，
  不会一次性把全部文档加载进内存。
- **并发执行**：批内由线程池（`workers`）处理；单个文档失败只影响自身，
  失败原因可区分：`content_missing` / `content_corrupt` / `legal_hold` /
  `invalid_state` / `cancelled` / `timeout` / `internal`。
- **可恢复**：归档按"先原子写归档文件（临时文件 + rename），再提交状态"
  的顺序执行；重启后 `recover_inflight()` 回收未提交的认领，
  `pending_destruction` 由下一次销毁扫描幂等续跑。
- **取消与超时**：`cancel()` 停止认领新文档；单文档超过 `doc_timeout`
  或提交前检测到取消，都会回滚到 `active`，不留半归档状态。

## 归档完整性

每个归档单元由 `<archive_id>.bin`（原文）与 `<archive_id>.meta`
（含 sha256 的 JSON 元数据）组成。读取归档内容强制校验：

- 文件缺失 → `ContentMissingError`（HTTP 410）
- 校验和不匹配 → `ContentCorruptError`（HTTP 422）

销毁前同样先校验归档内容，缺失或损坏时拒绝销毁并记录对应失败原因，
绝不静默返回空结果。

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DAE_DB_PATH` | `dae.db` | SQLite 数据库路径 |
| `DAE_ARCHIVE_ROOT` | `archive_data` | 归档文件根目录 |
| `DAE_BATCH_SIZE` | `100` | 每批处理文档数 |
| `DAE_WORKERS` | `4` | 批内并发线程数 |
| `DAE_DOC_TIMEOUT` | `30` | 单文档处理超时（秒） |

## HTTP API 摘要

| 方法/路径 | 说明 |
| --- | --- |
| `PUT /policies` | 创建/更新保留策略（版本必须递增），返回重算条数 |
| `POST /documents` | 创建文档（按策略计算到期时间） |
| `GET /documents/{id}` | 查询文档状态 |
| `GET /documents/{id}/content` | 读取内容（已归档的走归档校验读取） |
| `POST /holds/{id}` / `DELETE /holds/{id}` | 设置/解除法务保全 |
| `POST /engine/archive` | 执行一轮归档 |
| `POST /engine/destroy` | 执行一轮销毁 |
| `POST /engine/cancel` | 请求取消 |
| `POST /engine/recover` | 回收 in-flight 文档（启动时自动执行一次） |

## 运行与测试

```bash
uv run python main.py     # 启动服务
uv run pytest tests/ -q   # 运行测试（策略变更/保全/部分失败/并发/重启恢复）
```
