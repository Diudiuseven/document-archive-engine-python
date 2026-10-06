# document-archive-engine

文档归档生命周期引擎：在基本存取之上，提供按保留策略自动推进的归档与销毁能力，
支持法务保全、分批并发执行、失败失败隔离、检查点恢复与归档完整性校验。

## 生命周期语义

### 状态机

```
ACTIVE --(到期归档)--> ARCHIVING --(提交)--> ARCHIVED --(到期销毁)--> PENDING_DESTRUCTION --(提交)--> DESTROYED
                       |                     ^
                       +--(失败/取消/超时回滚)-+
                                          PENDING_DESTRUCTION --(保全拦截/失败回退)--> ARCHIVED
```

- 所有状态流转都在**单个事务**中校验并提交；任何时刻读取方只能看到某个完整的
  已提交状态，不会出现只更新了一半的中间态。
- `ARCHIVING` / `PENDING_DESTRUCTION` 是处理中的瞬时状态；取消、超时或失败会
  把文档**回滚**到处理前状态（`ACTIVE` / `ARCHIVED`），不会留下半归档数据。
- 非法流转（如 `ACTIVE -> DESTROYED`）会被引擎拒绝并抛出
  `InvalidStateTransitionError`。

### 保留策略

- 策略由 `active_days`（生效期）与 `archived_days`（归档保留期）组成；
  文档创建时按策略计算 `archive_due_at` 与 `destroy_due_at`。
- 策略变更会使 `version` 递增，并对该策略下**尚未进入销毁流程**的文档
  （`ACTIVE` / `ARCHIVING` / `ARCHIVED`）按游标分页重算到期时间。
- **历史结果不被追溯改写**：`archived_at`、`destroyed_at` 等已完成记录不变；
  `PENDING_DESTRUCTION` / `DESTROYED` 状态的文档不参与重算。

### 法务保全

- 对文档可放置任意多个保全（`place_hold`）；命中保全的文档在销毁阶段被拦截，
  状态回退到 `ARCHIVED`，批次中记录 `LEGAL_HOLD` 失败原因。
- 保全解除（`release_hold`）后，文档在下一轮销毁批次中重新进入原有进度，
  不跳过、不重复处理（已销毁文档不会再次被选中）。

### 批量执行与恢复

- 归档/销毁按 `batch_size` 分批推进，批内用 `max_workers` 个线程并发处理；
  单文档失败（数据缺失、被并发锁定、超时等）只记录为批次失败项，不拖垮整批。
- 每批提交后写入检查点；进程重启后从上次已提交位置继续。
  一轮扫描完成（不足一批或扫描到末尾）时检查点自动重置，避免漏掉
  游标之前但当时未到期或新加入的文档。
- 到期文档按 `document_id` 游标分页拉取，不会一次性把全部文档与内容加载进内存。
- 取消（`CancelToken`）与每文档超时（`per_document_timeout`）都是协作式的：
  在事务边界检查，触发后回滚到处理前状态。

### 归档完整性

- 归档时记录原文 SHA-256 校验和（同时存于文档元数据与归档数据）。
- 读取归档内容时双重校验：底层数据缺失抛 `ArchiveDataMissingError`，
  校验和不匹配抛 `ArchiveDataCorruptedError`，不会静默返回空结果。

## 失败原因分类

| FailureReason    | 含义                               |
|------------------|------------------------------------|
| `cancelled`      | 任务被取消                         |
| `timeout`        | 单文档处理超时                     |
| `locked`         | 文档正被其他任务处理               |
| `legal_hold`     | 命中法务保全，销毁被拦截           |
| `data_missing`   | 底层原文/归档数据缺失              |
| `data_corrupted` | 归档数据校验和不匹配               |
| `invalid_state`  | 文档当前状态不允许该操作           |
| `internal`       | 其他内部错误                       |

## 配置项

`DocumentArchiveService` 构造参数（HTTP 服务对应环境变量）：

| 参数                    | 环境变量              | 默认值     | 说明                 |
|-------------------------|-----------------------|------------|----------------------|
| `db_path`               | `ARCHIVE_DB`          | `:memory:` | SQLite 数据库路径    |
| `batch_size`            | `ARCHIVE_BATCH_SIZE`  | `50`       | 每批处理文档数       |
| `max_workers`           | `ARCHIVE_MAX_WORKERS` | `4`        | 批内并发线程数       |
| `per_document_timeout`  | `ARCHIVE_DOC_TIMEOUT` | `30.0`     | 单文档处理超时（秒） |

## HTTP API

```
POST   /policies                      创建保留策略
PATCH  /policies/{policy_id}          更新策略（触发重算）
POST   /documents                     创建文档（policy_id + content）
GET    /documents/{document_id}       查询文档状态
GET    /documents/{document_id}/archive  读取归档内容（带校验）
POST   /documents/{document_id}/holds    放置法务保全
DELETE /holds/{hold_id}               解除保全
POST   /tasks/archive                 推进一批归档任务
POST   /tasks/destroy                 推进一批销毁任务
```

## 运行与测试

```bash
uv run pytest          # 运行全部自动化测试
uv run python main.py  # 启动 HTTP 服务
```

测试覆盖：策略变更重算与历史保护、法务保全拦截与恢复、部分失败隔离、
并发批处理、取消/超时回滚、重启后检查点恢复、归档完整性校验。
