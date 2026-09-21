# 🏛️ 博物馆展品光照剂量审计 Web 应用

基于 **Python FastAPI + SQLite + 原生前端（HTML/CSS/Canvas，无 JS 框架）**
构建的展柜光照剂量审计工具。按相邻照度读数做**梯形积分**累计
`lux·hour`，明确标记跨传感器断点的剂量区间；模拟把展品**移入另一展柜**后，
依据两柜历史照度曲线**预测达到年度剂量上限的日期**。

- 默认只监听 `127.0.0.1`，本机访问，不依赖任何第三方账号或外部服务
- 数据存储于本地 SQLite（Docker volume）
- 页面并列显示：**累计剂量曲线**、**断点证据**、**轮换前后对比**
- 一键导出完整审计 JSON（含 SHA-256 校验和）

## 一条命令启动

```bash
docker compose up --build
```

然后浏览器打开 <http://localhost:8000>。

**本机访问语义**：容器内服务监听 `0.0.0.0:8000`（Docker 端口转发的要求），
但 `docker-compose.yml` 只把端口发布到宿主机回环地址
`127.0.0.1:8000:8000`，因此局域网其他机器无法访问。需要局域网访问时，把
compose 的端口行改成 `"8000:8000"` 后重新 `docker compose up -d`。

停止：`Ctrl+C`（后台运行用 `docker compose up -d --build`，停止用
`docker compose down`；加 `-v` 同时清除数据卷）。

> 不想用 Docker？也可以直接本地运行：
> ```bash
> pip install -r requirements.txt
> uvicorn app.main:app --host 127.0.0.1 --port 8000
> ```

## 30 秒试用

1. 打开页面，点击 **「载入示例数据（两柜 + 丝织卷轴）」**
   - `case-a`：约 50 lux 日间照明，2026-09-03 有一次 7 小时传感器断电（断点证据）
   - `case-b`：约 20 lux，数据完整
   - 展品 `silk-scroll`（丝织品，年度上限 **50 000 lux·h**），2026-08-01 起在 A 柜
2. 点击 **「计算累计剂量」**：查看累计曲线、跨断点区间与覆盖缺口
3. 在「轮换模拟」中目标展柜选 `case-b`，点击
   **「预测达到年度上限日期」**：对比留柜 / 换柜的预测触限日期
4. 点击 **「导出审计 JSON」** 下载完整审计包

## 剂量计算方法

- **积分规则**：相邻读数 `(t0, lux0)`、`(t1, lux1)` 之间用梯形公式
  `dose = (lux0 + lux1) / 2 × (t1 − t0)`，单位 `lux·hour`（读数间照度线性插值）
- **只计在柜时间**：剂量仅在展品 occupancy（在柜时间段）与读数覆盖的交集内累计
- **断点标记**：同一展柜相邻读数间隔超过该柜 `gap_threshold_minutes`
  （默认 60 分钟）时，该梯形区间标记为「跨断点」：
  - 页面与导出同时给出 **含断点总量** 和 **扣除断点段的审计量**
  - 断点证据逐条列出：两侧时间戳、间隔时长、阈值、两端 lux 及该段贡献的 lux·h
- **覆盖缺口**：展品在柜但该柜没有读数（或超出读数时间范围）的时段单独列出，
  不计入剂量但作为数据质量证据披露
- **年度窗口**：默认 `as_of` 所在公历年 1 月 1 日（UTC）起 365 天，可自定义
- 所有时间戳统一按 **UTC** 处理（无时区信息的时间戳视为 UTC）

## 轮换预测说明

- 年度窗口内的历史部分用**真实读数梯形积分**
- `as_of`（默认最新读数时刻）之后按该柜历史「在柜期间平均照度」
  （= lux·h / 有覆盖的在柜小时）以 24 h/天在柜线性外推
- 输出剩余预算、剂量速率、预计达到年度上限的日期；
  若预测日期**越过年度重置点**会明确打标
- 换柜时刻（默认 `as_of`）之后把在柜关系切换到目标展柜

## 数据上传格式

### 照度读数 JSONL

多柜自动路由（推荐；每行一个 JSON 对象）：

```jsonl
{"ts":"2026-08-01T08:00:00Z","lux":112.4,"case":"case-a"}
{"ts":"2026-08-01T08:00:00Z","lux":76.1,"case":"case-b"}
{"ts":"2026-08-01T08:30:00Z","lux":113.0,"case":"case-a"}
```

- `ts`：ISO-8601 时间戳（建议带 `Z`）
- `lux`：非负浮点数
- `case`：展柜名；整行缺省该字段时使用上传表单的「默认展柜」；
  未知名会自动创建（默认 60 分钟断点阈值）
- 同一 `(展柜, 时间戳)` 重复上传为 upsert

也可在页面选择某一展柜后上传不带 `case` 字段的单柜文件。

### 在柜时间段 JSONL

```jsonl
{"exhibit":"silk-scroll","case":"case-a","start_ts":"2026-08-01T00:00:00Z","end_ts":null,"note":"仍在柜"}
{"exhibit":"silk-scroll","case":"case-b","start_ts":"2026-11-01T00:00:00Z","end_ts":"2026-12-01T00:00:00Z"}
```

`end_ts` 为 `null` 表示开放区间（展品仍在该柜）。展品/展柜名不存在时自动创建
（新展品默认年度上限 50 000 lux·h，可在页面调整）。

## 主要 HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/cases` | 新建展柜（含断点阈值） |
| GET  | `/api/cases` | 展柜列表及读数统计 |
| POST | `/api/exhibits` | 新建展品（含年度剂量上限） |
| GET  | `/api/exhibits` | 展品列表 |
| POST | `/api/occupancies` | 添加在柜时间段（JSON） |
| GET  | `/api/occupancies?exhibit_id=` | 在柜时间段列表 |
| POST | `/api/readings/import` | 多柜照度 JSONL 导入（multipart，可选 `default_case`） |
| POST | `/api/cases/{id}/readings/upload` | 单柜照度 JSONL 上传 |
| POST | `/api/occupancies/import` | 在柜段 JSONL 导入 |
| POST/GET | `/api/audit` | 审计：总量、分柜量、断点证据、覆盖缺口、累计曲线 |
| POST/GET | `/api/simulate` | 换柜预测（`exhibit_id`、`target_case_id`、可选 `move_ts`、`annual_start`、`as_of`） |
| POST | `/api/audit/export?exhibit_id=...[&simulate_target_case_id=...]` | 导出审计 JSON（含 `checksum_sha256`） |
| POST | `/api/admin/seed` | 生成并导入示例数据（已有数据时需 `overwrite=true`） |
| GET  | `/health` | 健康检查 |

交互式 API 文档：启动后访问 <http://localhost:8000/docs>（FastAPI 自带 Swagger UI）。

### curl 示例

```bash
# 示例数据
curl -X POST http://localhost:8000/api/admin/seed

# 运行审计（exhibit_id=1）
curl "http://localhost:8000/api/audit?exhibit_id=1" | python3 -m json.tool

# 换入 2 号展柜并预测
curl "http://localhost:8000/api/simulate?exhibit_id=1&target_case_id=2" | python3 -m json.tool

# 导出含模拟结果的审计包
curl -X POST "http://localhost:8000/api/audit/export?exhibit_id=1&simulate_target_case_id=2" -o audit.json
```

## 导出的审计 JSON 结构（节选）

```jsonc
{
  "exhibit": { "id": 1, "name": "silk-scroll",
               "annual_limit_lux_hours": 50000.0 },
  "window": { "start_ts": "...", "end_ts": "..." },
  "occupancies": [ ... ],
  "totals": {
    "total_lux_hours": 0.0,        // 含跨断点段
    "audited_lux_hours": 0.0,      // 扣除跨断点段
    "breakpoint_lux_hours": 0.0,
    "occupied_hours": 0.0,
    "covered_hours": 0.0
  },
  "by_case": [ ... ],
  "breakpoint_evidence": [
    { "case_name": "case-a", "reading_start": "...", "reading_end": "...",
      "gap_hours": 7.0, "threshold_hours": 1.0,
      "lux_start": 95.1, "lux_end": 101.2,
      "lux_hours_in_occupancy": 0.0 }
  ],
  "coverage_gaps": [ ... ],
  "cumulative_curve": [ { "ts": "...", "cumulative_lux_hours": 0.0 } ],
  "simulation": { "stay": { "projected_limit_ts": "..." },
                  "move": { "projected_limit_ts": "..." } },
  "exported_at": "...",
  "checksum_sha256": "..."
}
```

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、JSONL 上传、审计/模拟/导出
│   ├── audit.py       # 梯形积分、断点标记、累计曲线、换柜预测
│   ├── db.py          # SQLite 连接与表结构
│   └── seed.py        # 确定性示例数据（含一次传感器断电）
├── static/
│   ├── index.html     # 单页界面
│   ├── styles.css
│   └── app.js         # 原生 JS + Canvas 2D 自绘图表
├── sample_data/       # 由 /api/admin/seed 生成
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 常用剂量上限参考（UK / CIE 保护等级，年曝光量）

| 等级 | 典型材质 | 年上限 |
|---|---|---|
| 高敏感 | 丝织品、纸质水彩、天然染料 | 50 000 lux·h |
| 中敏感 | 油画、骨角、木器 | 150 000 lux·h |
| 低敏感 | 石材、金属、陶瓷 | 不限制（可用 700 000 lux·h 管理值） |

应用不对材质做强制约束，上限按展品单独配置。
