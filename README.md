# 建筑无障碍通路核验台

本地 Web 应用，用于核验轮椅使用者在建筑内的无障碍通行路线。基于 Python + FastAPI + SQLite + NetworkX 管理有向路网，浏览器使用原生 JavaScript 呈现楼层图。

## 功能特性

- **路网导入**：导入含楼层坐标、出入口、房间和通道的 JSON 文件；系统拒绝悬空节点和非法跨层连接（跨层仅允许电梯或坡道）。
- **并行通道**：允许同一对起终点之间存在多条 ID 不同的通道。每条通道按各自 ID 独立参与净宽、坡度、楼梯、跨层限制以及封闭时段判断，互不覆盖；示例路网中门厅（n2）与活动室（n14）之间即有 `e27`（快、宽）与 `e28`（慢、窄）两条并行通道。
- **封闭时段登记**：为通道登记带时区的封闭时段，支持跨时区。
- **轮椅路线查询**：按起终点、出发时刻、最小净宽、最大坡度查询路线。楼梯不可走，跨层仅允许电梯或坡道，可在节点等候，但通行全程不得与封闭时段相交。在出发后 24 小时内求最早到达，平局依次按路程和边 ID 序列择优；两条路径汇入同一中途节点后若被迫等到同一时刻通行，候选在标签裁决中均会保留至终点再比较，不会在中途被提前淘汰。
- **楼层图呈现**：逐层标出路线与等待时间，不可达时列出阻断原因。
- **核验记录固化**：每次查询固化路网及封闭时段版本，后续修改不改变旧结果。

## 启动方式

### Docker Compose 一键启动（推荐）

```bash
docker compose up --build
```

启动后访问 <http://localhost:8000>。

### 本地开发启动

```bash
pip install -r backend/requirements.txt
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

访问 <http://localhost:8000>。

## 两种路线查询入口

启动后，轮椅路线查询有两个入口，二者调用同一个后端搜索、裁决完全一致：

1. **页面入口**：浏览器访问 <http://localhost:8000>，在左侧面板导入路网、登记封闭时段，填写「起点 / 终点 / 出发时刻 / 最小净宽 / 最大坡度」后点击「查询路线」，右侧「核验结果」逐层展示选中的逐段通道、每段等待时间与到达时刻；不可达时列出阻断原因。
2. **接口入口**：直接向 `POST http://localhost:8000/api/route/query` 发送 JSON（字段与页面表单一致，见下文示例），响应中的 `steps`、`wait_seconds`、`arrival` 与页面展示同源；每次查询（无论来自页面还是接口）都会固化一条核验记录，可经 `GET /api/records` 与页面「历史核验记录」回看，后续修改路网或封闭时段不会重算旧记录。

## 使用流程

1. **导入路网**：在左侧面板点击「选择文件」，选择示例文件 `data/sample_network.json`（或自行编写的路网 JSON），点击「导入路网」。
2. **登记封闭时段**：在「封闭时段登记」中填写通道 ID、起止时间和时区，点击「登记封闭时段」。
3. **查询路线**：填写起点、终点、出发时刻、最小净宽和最大坡度，点击「查询路线」。
4. **查看结果**：右侧「核验结果」面板会逐层列出路线与等待时间；不可达时列出阻断原因。
5. **查看历史记录**：右侧「历史核验记录」列出所有核验记录，点击可查看当时的路网与结果。

## 路网 JSON 格式

```json
{
  "floors": [
    { "id": "f1", "name": "1F", "level": 0 }
  ],
  "nodes": [
    { "id": "n1", "floor_id": "f1", "x": 0, "y": 0, "kind": "entrance", "name": "主入口" }
  ],
  "edges": [
    {
      "id": "e1",
      "from": "n1",
      "to": "n2",
      "type": "corridor",
      "length": 10,
      "seconds": 12,
      "width": 1.8,
      "slope": 0
    }
  ]
}
```

### 字段说明

- **floors**：楼层列表。`id` 唯一标识，`name` 显示名称，`level` 楼层序号。
- **nodes**：节点列表。
  - `kind`：节点类型，可选 `entrance`（出入口）、`exit`（出口）、`room`（房间）、`junction`（节点）、`elevator`（电梯）。
  - `x` / `y`：楼层图坐标。
- **edges**：通道列表。
  - `type`：通道类型，可选 `corridor`（通道）、`stairs`（楼梯）、`elevator`（电梯）、`ramp`（坡道）。
  - `length`：长度（米）。
  - `seconds`：通行秒数。
  - `width`：净宽（米）。
  - `slope`：坡度（度）。

### 校验规则

- 节点 ID、通道 ID、楼层 ID 不可重复。
- 通道的起终点必须存在。
- 不允许悬空节点（未连接任何通道的节点）。
- 跨楼层通道的类型必须是 `elevator` 或 `ramp`；同楼层通道类型不可为 `elevator` 或 `ramp`。
- 长度、通行秒数、净宽不可为负；坡度在 0~90 之间。

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/network/import` | 导入路网 JSON |
| GET | `/api/network` | 获取当前路网 |
| GET | `/api/network/versions` | 列出路网版本 |
| GET | `/api/network/{version}` | 获取指定版本路网 |
| POST | `/api/closures` | 登记封闭时段 |
| GET | `/api/closures` | 列出封闭时段 |
| DELETE | `/api/closures/{id}` | 删除封闭时段 |
| POST | `/api/route/query` | 查询轮椅路线 |
| GET | `/api/records` | 列出核验记录 |
| GET | `/api/records/{id}` | 获取核验记录详情 |

### `/api/route/query` 请求与响应示例

以下示例基于 `data/sample_network.json`，先导入路网：

```bash
docker compose up --build
# 另一终端
curl -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  --data-binary @data/sample_network.json
```

查询门厅 `n2` 到活动室 `n14` 的路线（最小净宽 1.0 m）。两点之间有两条并行通道：`e27`（6 秒、净宽 2.0 m）与 `e28`（30 秒、净宽 0.9 m），净宽限制下只允许 `e27`：

```bash
curl -X POST http://localhost:8000/api/route/query \
  -H 'Content-Type: application/json' \
  -d '{
    "start": "n2",
    "end": "n14",
    "departure": "2026-10-01T08:00:00+08:00",
    "min_width": 1.0,
    "max_slope": 8
  }'
```

响应（`steps` 中返回实际选中的通道及其逐段等待/到达信息）：

```json
{
  "reachable": true,
  "arrival": "2026-10-01T00:00:06+00:00",
  "total_seconds": 6.0,
  "total_length": 6.0,
  "wait_seconds": 0.0,
  "steps": [
    {
      "edge_id": "e27",
      "from_node": "n2",
      "to_node": "n14",
      "from_name": "门厅",
      "to_name": "活动室",
      "floor_id": "f1",
      "floor_name": "1F",
      "type": "corridor",
      "length": 6.0,
      "seconds": 6.0,
      "width": 2.0,
      "slope": 0.0,
      "wait_seconds": 0.0,
      "arrival": "2026-10-01T00:00:06+00:00",
      "cross_floor": false
    }
  ],
  "reasons": [],
  "blocking_closures": [],
  "record_id": 1,
  "network_version": 1,
  "closure_version": 0
}
```

先为 `e27` 登记一段封闭（`POST /api/closures`，时间 `2026-10-01T08:00:00+08:00` ～ `2026-10-01T08:10:00+08:00`、时区 `Asia/Shanghai`），再以最小净宽 0.9 m 查询：在 `e27` 处等候到 08:10 需 10 分钟，而改走并行的 `e28` 30 秒即可到达，因此算法选择 `e28`：

```json
{
  "reachable": true,
  "arrival": "2026-10-01T00:00:30+00:00",
  "total_seconds": 30.0,
  "total_length": 7.0,
  "wait_seconds": 0.0,
  "steps": [
    {
      "edge_id": "e28",
      "from_node": "n2",
      "to_node": "n14",
      "from_name": "门厅",
      "to_name": "活动室",
      "floor_id": "f1",
      "floor_name": "1F",
      "type": "corridor",
      "length": 7.0,
      "seconds": 30.0,
      "width": 0.9,
      "slope": 0.0,
      "wait_seconds": 0.0,
      "arrival": "2026-10-01T00:00:30+00:00",
      "cross_floor": false
    }
  ],
  "reasons": [],
  "blocking_closures": []
}
```

#### 封闭等待后的平局裁决（汇流节点 `n15` → `n18`）

示例路网中 `n15`（汇流起点）到 `n16`（汇流点）有两条通道：

- `e37`：直达，通行 8 秒、路程 20 m（边 ID 较大）；
- `e29` + `e30`：经 `n17`（南侧通道节点），共 10 秒、路程 20 m（边 ID 序列较小）。

`n16` 之后只有 `e31` 通往 `n18`（多功能厅，10 秒、5 m）。为 `e31` 登记封闭时段 `2026-10-01T08:00:00+08:00` ～ `08:05:00+08:00`（UTC 00:00～00:05）后，于 08:00 从 `n15` 出发：走 `e37` 08:00:08 就到达 `n16`，走 `e29,e30` 08:00:10 到达；但两者都要在 `n16` 等候到 08:05 才能通行 `e31`，最终同为 08:05:10 到达 `n18`，总路程也同为 25 m。此时按边 ID 序列裁决，`(e29,e30,e31) < (e37,e31)`，算法保留较晚到达汇流点的候选并选中 `e29,e30,e31`，逐段返回 `e31` 前的 290 秒等待：

```bash
curl -X POST http://localhost:8000/api/closures \
  -H 'Content-Type: application/json' \
  -d '{
    "edge_id": "e31",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T08:05:00+08:00",
    "tz": "Asia/Shanghai"
  }'

curl -X POST http://localhost:8000/api/route/query \
  -H 'Content-Type: application/json' \
  -d '{
    "start": "n15",
    "end": "n18",
    "departure": "2026-10-01T08:00:00+08:00",
    "min_width": 0,
    "max_slope": 90
  }'
```

```json
{
  "reachable": true,
  "arrival": "2026-10-01T00:05:10+00:00",
  "total_seconds": 310.0,
  "total_length": 25.0,
  "wait_seconds": 290.0,
  "steps": [
    { "edge_id": "e29", "from_node": "n15", "to_node": "n17", "wait_seconds": 0.0, "arrival": "2026-10-01T00:00:05+00:00", "...": "..." },
    { "edge_id": "e30", "from_node": "n17", "to_node": "n16", "wait_seconds": 0.0, "arrival": "2026-10-01T00:00:10+00:00", "...": "..." },
    { "edge_id": "e31", "from_node": "n16", "to_node": "n18", "wait_seconds": 290.0, "arrival": "2026-10-01T00:05:10+00:00", "...": "..." }
  ]
}
```

无封闭时同一查询仍选更早到达的 `e37,e31`（08:00:18 到达）。裁决顺序始终是：24 小时窗口内先比最终到达时刻，再比总路程，最后逐位比较完整的边 ID 序列。

若把 `e27` 封闭超过出发后 24 小时，同时最小净宽仍为 1.0 m（`e28` 不满足），则不可达，响应给出阻断原因与命中的封闭时段：

```json
{
  "reachable": false,
  "reasons": ["路径被封闭时段阻断，出发后 24 小时内无法到达"],
  "blocking_closures": [
    {
      "edge_id": "e27",
      "from_node": "n2",
      "to_node": "n14",
      "start": "2026-10-01T00:00:00+00:00",
      "end": "2026-10-03T00:00:00+00:00"
    }
  ]
}
```

## 技术栈

- 后端：Python 3.12、FastAPI、SQLite、NetworkX
- 前端：原生 JavaScript、SVG
- 部署：Docker、Docker Compose

## 核验脚本

`tests/` 目录提供裁决正确性核验（需 `pip install httpx`）：

```bash
python3 tests/verify_fix.py      # 平局裁决、并行通道、不可达诊断、跨层、记录固化（46 项）
python3 tests/diff_targeted.py   # 汇流等待场景与穷举最优路径的差分比对（186 例）
python3 tests/diff_random.py     # 随机路网与穷举最优路径的差分比对（424 例）
```

脚本全部退出码为 0 表示当前搜索裁决与穷举结果完全一致；在修复前的代码上 `verify_fix.py` 有 8 项失败、`diff_targeted.py` 有 10 例不一致。
