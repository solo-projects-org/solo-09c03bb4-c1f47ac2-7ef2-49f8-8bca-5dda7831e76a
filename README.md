# 联合目录补采采集服务

联合目录管理员对单馆书目进行补采的 OAI-PMH 采集服务。核心保证：**翻页失败绝不让半批记录入库**。

## 需求与实现对照

| 需求 | 实现 |
| --- | --- |
| FastAPI 接收截止日，首次另填起始日 | `POST /api/v1/harvest`，请求体 `until` 必填、`from` 选填 |
| 之后从上次成功采集的截止日接着采，起止日都包含 | `from` 留空时取 `harvest_progress.last_until` 作为起始日（含当天） |
| OAI-PMH ListRecords 按日期拉取 oai_dc | 首请求 `verb=ListRecords&metadataPrefix=oai_dc&from=...&until=...` |
| 续页用 resumptionToken 替代日期和格式参数 | 续页请求只带 `verb` + `resumptionToken`；样例源对混用参数返回 `badArgument` |
| 逐页核对不是串页 | 每个成功页核对 `<request>` 回显参数与实际请求一致；有 `cursor` 时必须等于本轮此前收到的记录数（删除标记也计入）；不看 `completeListSize` 判断是否采完 |
| 串页/令牌失效丢弃已取页，按原区间重抓 | 重抓时清空本轮内存页，用原始 from/until 重新拉取；两种故障**合计只重抓一次**，重抓仍逐页核对 |
| 只有首请求 noRecordsMatch 才按空批推进 | 首请求无匹配 -> `fetched=0` 推进截止日；续页返回该错误 -> 整批失败，截止日不动 |
| 重抓仍失败返回 502 且目录/截止日不变 | 501/502 响应说明原因，事务回滚，`harvest_runs` 记录 `failed` |
| 按标识更新 SQLite 书目 | `bib_records.identifier` 主键 upsert，重复采集幂等 |
| 遇删除标记就撤下 | header `status=deleted` 的记录置为 `withdrawn`（库中不存在则写墓碑） |
| 整批采完才提交书目变更和截止日 | 单事务 `BEGIN IMMEDIATE`，全部页取完才写库并提交；异常整体回滚 |
| Docker Compose 启动服务和本地分页样例源 | `docker compose up --build` 启动 `harvester-api` + `oai-source` |
| Swagger 发起采集并查询目录和进度 | http://localhost:8000/docs |

## 目录结构

```
app/                  FastAPI 采集服务
  main.py             路由（采集/目录/进度/运行历史/健康检查）
  harvester.py        采集编排：翻页、令牌失效重抓、整批事务
  oai_client.py       OAI-PMH 2.0 客户端（httpx + xml.etree）
  db.py               SQLite 表结构与事务操作
  schemas.py          Pydantic 请求/响应模型
  Dockerfile
sample-oai/           本地分页样例 OAI-PMH 源（纯标准库）
  server.py           27 条样例记录（rec-013 为删除记录），PAGE_SIZE=10，3 页
                      另带串页/令牌失效/续页无匹配故障注入端点
  Dockerfile
tests/                pytest 端到端测试（20 个用例，含串页/失效重抓与整体失败）
scripts/              辅助脚本（故障回滚演示）
docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集服务 API / Swagger：http://localhost:8000/docs
- 样例 OAI 源：http://localhost:8080/oai?verb=Identify
- SQLite 数据保存在命名卷 `harvester-data`（容器内 `/data/harvester.db`）

## 操作流程（Swagger 或 curl）

### 1. 首次采集：另填起始日，起止日都包含

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-15"}'
```

响应：

```json
{
  "status": "success",
  "from_date": "2026-10-01",
  "until_date": "2026-10-15",
  "pages": 2, "fetched": 15,
  "inserted": 14, "updated": 0, "withdrawn": 1,
  "retries": 0, "run_id": 1
}
```

### 2. 后续采集：只填截止日，自动从上次截止日（含）续采

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-27"}'
# 实际区间：2026-10-15 ~ 2026-10-27（last_until 含当天，避免边界漏采）
```

### 3. 查询目录与进度

```bash
curl 'http://localhost:8000/api/v1/catalog?status=active'   # 在架（默认）
curl 'http://localhost:8000/api/v1/catalog?status=all'      # 含已撤下
curl 'http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-013'
curl 'http://localhost:8000/api/v1/progress'                # 上次成功截止日
curl 'http://localhost:8000/api/v1/runs'                    # 采集运行历史
```

### 4. 无匹配也推进截止日

只有**本轮首请求**返回 `noRecordsMatch` 才按空批处理：返回 `fetched=0`，`last_until`
照常推进。若是**续页**返回 `noRecordsMatch`，视为翻页故障之外的协议异常，整批失败
（`502`），目录与截止日不变。

## 串页/令牌失效重抓的验证方法

样例源提供故障注入端点：

```bash
# 注入故障（mode 默认 bad-token；times 默认 1；mode 可用逗号分隔形成序列）
curl -X POST 'http://localhost:8080/admin/fail-next?mode=cross-page&times=1'
curl -X POST 'http://localhost:8080/admin/fail-next?mode=cross-page,bad-token&times=2'
curl http://localhost:8080/admin/state

# 简写：串页（cross-page 或 cursor-mismatch）
curl -X POST 'http://localhost:8080/admin/cross-page?times=1'
curl -X POST 'http://localhost:8080/admin/cross-page?mode=cursor-mismatch&times=1'
```

故障模式：

| mode | 样例源行为 | 采集端如何识别 |
| --- | --- | --- |
| `bad-token` | 续页返回 `badResumptionToken` | 令牌失效 |
| `cross-page` | 续页返回别的请求的末页，`<request>` 回显另一个令牌 | 回显参数与实际请求不一致 |
| `cursor-mismatch` | 回显与实际请求一致，但分页位置落在别的请求上 | `cursor` ≠ 本轮已收记录数 |
| `no-records-match` | 续页返回 `noRecordsMatch` | 续页不允许空批，整批失败 |

验证流程（Swagger 或 curl）：

```bash
# A. 串页一次 -> 丢弃本轮已取页 -> 按原区间重抓成功
curl -X POST 'http://localhost:8080/admin/cross-page?times=1'
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-27"}'
# 响应 retries=1、pages=3、fetched=27；进度 last_until=2026-10-27

# B. 两种故障合计只重抓一次：重抓期间再次串页/失效 -> 502
curl -X POST http://localhost:8080/admin/reset
curl -X POST 'http://localhost:8080/admin/fail-next?mode=cross-page,bad-token&times=2'
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-06", "until": "2026-10-27"}'
# 返回 502 与原因；目录条数、last_until 均与失败前一致；harvest_runs 新增 failed

curl -X POST http://localhost:8080/admin/reset   # 清除故障与令牌
```

## 事务与一致性说明

- 采集过程先在内存中收集全部页面，**不向 SQLite 写任何数据**；
- 全部页成功后，在单个 `BEGIN IMMEDIATE` 事务内完成 upsert、撤下、截止日推进、运行历史更新；
- 事务中任何一步失败都会 rollback，库状态回到采集前；
- 进程内互斥锁保证同一时刻只有一个采集任务，并发请求返回 `409`。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 终端 1：样例 OAI 源
python3 sample-oai/server.py

# 终端 2：API
APP_DB_PATH=./data/harvester.db OAI_ENDPOINT=http://127.0.0.1:8080/oai \
  uvicorn app.main:app --reload

# 测试
pytest
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OAI_ENDPOINT` | `http://oai-source:8080/oai` | OAI-PMH 端点 |
| `APP_DB_PATH` | `/data/harvester.db` | SQLite 数据库路径 |
| `PAGE_SIZE`（样例源） | `10` | 样例源每页记录数 |
| `TOKEN_TTL`（样例源） | `3600` | 样例源 resumptionToken 有效期秒数 |
