# 工地作业车危险区穿越审计服务

用 Python (FastAPI + Shapely + SQLite) 构建的 HTTP 服务，用于：

1. **登记危险区**：平面米制多边形（支持洞）+ UTC 左闭右开 `[start, end)` 生效时段；
2. **批量提交轨迹点**：车次、点 ID、UTC 时刻、平面坐标；同点 ID 同内容重传幂等，异内容返回冲突；
3. **穿越审计**：相邻点间按时间与坐标线性插值，仅在“轨迹覆盖且区域生效”的时间内计算穿越，按危险区返回起止区间与累计秒数；
4. **修订可复现**：每次登记/同 ID 覆盖都原子产生一个全局递增修订号并在响应中返回，同时保存各区的不可变历史版本；穿越查询可指定修订号，按当时的区域配置复现核算。

## 语义约定

- 坐标为**平面米制坐标**（单位统一为米），服务内部不做投影换算。
- 危险区 = 多边形内部 + **外边界**；**洞的内部与其边界均不计入**。
- 生效时段为 UTC、**左闭右开** `[start, end)`，仅当穿越时间落在生效时段内才统计。
- 相邻轨迹点按时刻排序后在相邻点间做**时间-空间线性插值**；乱序上传会自动按时刻重建。
- **零时长接触不算穿越**（轨迹仅擦过角点/边于一瞬不计）；静止点按点归属处理。
- 同一危险区的相接（端点重合）穿越区间自动合并；中间隔着未覆盖时间的区间不合并。
- 非法输入明确拒绝（HTTP 400）：自交多边形、洞越出外环、洞互相重叠、非有限坐标（NaN/Infinity）、不带时区的时刻、非正长度时段等。
- 同车次同一 UTC 时刻只能属于一个点 ID，否则视为冲突（HTTP 409）。
- 批量提交为**单事务原子写入**：任何一点校验/冲突失败，整批不写入任何数据。
- 每次区域登记或同 ID 覆盖更新都在单事务内原子地分配一个**全局单调递增修订号**（从 1 开始），登记响应返回该号，并落一份该区的**不可变历史版本**（几何 + 洞 + 生效时段快照）。
- 穿越查询可带 `revision`：取**各区截至该修订号的最后一个版本**复现核算；该版本中尚未登记的区域不参与核算。不带 `revision` 时沿用现有最新配置路径，请求字段与响应语义保持不变。
- 修订号不存在（小于 1）或超出当前最新范围时明确报错（422 / 404）。
- 并发更新与查询通过写事务 + 只读快照事务串行隔离，互不读到半次提交。
- 旧库现存区域在首次以新版本打开时**一次性迁移为基线版本**（按创建时刻顺序分配修订号），不推断迁移前历史；迁移幂等，重启不会重复迁移。
- SQLite 持久化到挂载卷，**进程/容器重启后记录仍可查询**。

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

启动后：

- 服务地址：`http://localhost:8080`
- 健康检查：`GET http://localhost:8080/health`
- 交互式 API 文档（Swagger UI）：`http://localhost:8080/docs`
- OpenAPI 描述：`http://localhost:8080/openapi.json`

数据保存在 Docker 命名卷 `audit-data`（容器内 `/data/dangerzones.db`），重启不丢。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据卷
```

仅构建镜像：`docker build -t danger-zone-audit:1.0.0 .`，容器入口脚本会先执行数据库初始化（建表）再启动 uvicorn。

## 本地直接运行（无 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
export DZ_DB_PATH=./data/dangerzones.db
python -m app.init_db                     # 可选，首次启动会自动建表
uvicorn app.main:app --host 0.0.0.0 --port 8080
# 或：python -m app.main
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DZ_HOST` | `0.0.0.0` | 监听地址 |
| `DZ_PORT` | `8080` | 监听端口 |
| `DZ_DB_PATH` | `./data/dangerzones.db`（Compose 内为 `/data/dangerzones.db`） | SQLite 数据文件路径 |
| `DZ_MIN_DURATION_SEC` | `1e-9` | 小于该秒数的覆盖片段视为零时长接触，不计入穿越 |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/zones` | 登记（或同 ID 覆盖更新）危险区，响应含全局 `revision` |
| GET | `/api/v1/zones` | 列出全部危险区（最新配置） |
| GET | `/api/v1/zones/{zone_id}` | 查询单个危险区（最新配置） |
| GET | `/api/v1/zones/{zone_id}/revisions` | 该区的不可变版本号列表与记录时刻 |
| GET | `/api/v1/revisions/current` | 当前最新全局修订号（无任何登记时为 0） |
| POST | `/api/v1/trips/{trip_id}/points` | 批量提交/重传某车次轨迹点（原子、幂等） |
| GET | `/api/v1/trips/{trip_id}` | 查询车次全部轨迹点（按时刻排序） |
| GET | `/api/v1/trips/{trip_id}/crossings` | 穿越审计结果（可带 `revision` 复现） |

穿越查询参数（均可选）：`zone_id`（只看某危险区）、`since` / `until`（UTC ISO 8601，半开 `[since, until)` 时间过滤）、`revision`（按指定全局修订号复现，取各区截至该号的最后版本）。

时间字符串使用 ISO 8601 且**必须带时区**，推荐以 `Z` 结尾表示 UTC，例如 `2026-09-30T12:00:00Z`。

## 调用示例

### 1) 登记带洞危险区

外边界 100m×100m，中央 20m×20m 为洞（洞及其边界不计入），2026-09-30 全天生效：

```bash
curl -s -X POST http://localhost:8080/api/v1/zones \
  -H 'Content-Type: application/json' \
  -d '{
    "zone_id": "Z-A",
    "exterior": [[0,0],[100,0],[100,100],[0,100]],
    "holes": [[[40,40],[60,40],[60,60],[40,60]]],
    "windows": [
      {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
    ]
  }'
# {"zone_id":"Z-A","status":"created","revision":1,"num_holes":1,"num_windows":1}
```

登记/覆盖响应新增 `revision` 字段：`created` 与 `updated` 都会原子分配一个比此前所有修订号都大的全局新号。可用 `GET /api/v1/revisions/current` 查询当前最新修订号。

环的首尾点无需重复给出；可登记多个生效时段（重叠/相接时段会合并处理）。

### 2) 批量提交轨迹点（允许乱序）

```bash
curl -s -X POST http://localhost:8080/api/v1/trips/truck-7/points \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"point_id":"a", "ts":"2026-09-30T12:00:00Z", "x":-20, "y":50},
      {"point_id":"c", "ts":"2026-09-30T12:02:00Z", "x":120, "y":50},
      {"point_id":"b", "ts":"2026-09-30T12:01:00Z", "x":50,  "y":50}
    ]
  }'
# {"trip_id":"truck-7","received":3,"accepted_new":3,"idempotent_retransmit":0}
```

同点 ID 同内容重传返回 `accepted_new=0, idempotent_retransmit=N`（HTTP 200）；
异内容返回 HTTP 409，例如：

```json
{"error":"conflict","detail":"point_id='a' 已存在且内容不同：已存 (ts_epoch=..., x=-20.0, y=50.0)，新传 (...)"}
```

整批中任何一点失败都会回滚，不写入部分数据。

### 3) 查询穿越审计

```bash
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings"
```

```json
{
  "trip_id": "truck-7",
  "point_count": 3,
  "zones": [
    {
      "zone_id": "Z-A",
      "crossed": true,
      "total_duration_sec": 68.571429,
      "segments": [
        {"start": "2026-09-30T12:00:17.142857Z", "end": "2026-09-30T12:00:51.428571Z", "duration_sec": 34.285714},
        {"start": "2026-09-30T12:01:08.571429Z", "end": "2026-09-30T12:01:42.857143Z", "duration_sec": 34.285714}
      ]
    }
  ]
}
```

车辆以 140m/120s 匀速横穿，进入外边界到进入洞前沿途计入；穿越洞（含其边界）的
约 17.14 秒被剔除，出洞后再次计入，因此得到两段、累计 68.571429 秒。两段之间被
洞隔开，不合并；若轨迹在区域边界处首尾相接，则自动合并为一段。

带时间过滤的查询：

```bash
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings?zone_id=Z-A&since=2026-09-30T12:00:30Z&until=2026-09-30T12:01:30Z"
```

### 4) 按修订号复现历史核算

现场会修订危险区几何或生效时段。每次登记/覆盖都会得到一个全局递增修订号并保存该区的不可变版本；穿越查询带 `revision=N` 时，取**各区截至 N 的最后版本**，沿同样的带洞边界、UTC 半开窗口与相邻点插值规则复现。

```bash
# Z-A 首次登记 -> revision=1
curl -s -X POST http://localhost:8080/api/v1/zones -H 'Content-Type: application/json' -d '{
  "zone_id":"Z-A",
  "exterior":[[0,0],[100,0],[100,100],[0,100]],
  "windows":[{"start":"2026-09-30T00:00:00Z","end":"2026-10-01T00:00:00Z"}]
}'
# {"zone_id":"Z-A","status":"created","revision":1,...}

# 现场把外边界改为窄区 -> 同 ID 覆盖，revision=2
curl -s -X POST http://localhost:8080/api/v1/zones -H 'Content-Type: application/json' -d '{
  "zone_id":"Z-A",
  "exterior":[[40,0],[60,0],[60,100],[40,100]],
  "windows":[{"start":"2026-09-30T00:00:00Z","end":"2026-10-01T00:00:00Z"}]
}'
# {"zone_id":"Z-A","status":"updated","revision":2,...}

# 不带 revision：按最新（窄区）配置
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings?zone_id=Z-A"

# 带 revision=1：按修订 1 时的旧几何复现
curl -s "http://localhost:8080/api/v1/trips/truck-7/crossings?zone_id=Z-A&revision=1"
```

```json
{
  "trip_id": "truck-7",
  "point_count": 3,
  "revision": 1,
  "zones": [
    {
      "zone_id": "Z-A",
      "revision": 1,
      "crossed": true,
      "total_duration_sec": 100.0,
      "segments": [ ... ]
    }
  ]
}
```

- 指定 `revision` 时，顶层与每个参与核算的区都带 `revision`（顶层为请求值，区级为该区实际生效版本号，二者在该区于该号前已登记时相等）。
- 不带 `revision` 的响应**不含**任何 `revision` 字段，路径与语义与历史版本完全一致。
- 不带 `zone_id` 时，只统计在修订 N（含）之前已登记、且截至 N 取最后版本的区；N 时**尚未登记**的区不参与核算。带 `zone_id` 查询一个当时尚未登记的区，返回该区 `zones: []`（不报错）。
- `revision` 超出当前最新号返回 `404 revision_not_found`；`revision<1` 或非整数返回 `422`。

查看某区的全部不可变版本：

```bash
curl -s http://localhost:8080/api/v1/zones/Z-A/revisions
# {"zone_id":"Z-A","current_revision":2,
#  "revisions":[{"revision":1,"recorded_at":"..."},{"revision":2,"recorded_at":"..."}]}
```

### 旧库迁移

升级前已存在的区域，在服务首次以新版本打开数据库时自动迁移为**基线版本**（按 `created_at`、`zone_id` 顺序分配修订号），迁移幂等、只发生一次，不推断迁移前历史。迁移后的首个新登记从下一个修订号继续。

## 错误响应

- `400 validation_error`：几何/时间/坐标语义非法（自交多边形、洞越界、非有限坐标、时区缺失等）；未指定修订号时查询了不存在的危险区。
- `404 not_found`：危险区、车次，或区域历史不存在。
- `404 revision_not_found`：指定的修订号超出当前最新范围。
- `409 conflict`：同点 ID 异内容，或同车次同刻关联了不同点 ID。
- `422`：请求体/查询参数不符合 JSON 模式（类型错误、多余字段、缺字段、`revision<1` 或非整数）。

## 运行测试

```bash
pip install pytest httpx
python -m pytest -q
```

测试覆盖：带洞多边形裁剪、外边界计入、洞与洞边界剔除、零时长接触、自交/洞越界拒绝、
幂等与冲突、批量原子性、乱序重建、生效时段半开裁剪、区间合并、重启持久化，以及
修订号单调分配、按修订号复现几何/时段、未登记区排除、修订号越界报错、并发登记唯一修订号、
旧库基线迁移等。

## 目录结构

```
app/
  config.py       # 环境变量配置
  geometry.py     # 带洞多边形校验与线段裁剪（Shapely）
  timeutils.py    # UTC 解析与半开时间窗运算
  db.py           # SQLite 持久层（事务、唯一约束、索引）
  service.py      # 业务逻辑：登记/幂等批量/穿越计算
  schemas.py      # Pydantic 请求模型
  main.py         # FastAPI 入口
  init_db.py      # 数据库初始化（建表）
tests/            # pytest 测试套件
Dockerfile
docker-compose.yml
docker-entrypoint.sh
requirements.txt
```
