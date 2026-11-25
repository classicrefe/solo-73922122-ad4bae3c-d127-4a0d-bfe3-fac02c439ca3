# 滩涂地类核查入库系统

两份滩涂地类图（各自由内部互不重叠的轴对齐矩形组成、同一米制坐标系）
对不上时，依据带坐标和地类的实测点，核定哪些分片可以先入库。
技术栈：Flask + Shapely + SQLite，Docker Compose 一键启动。

## 业务规则（与实现一一对应）

1. **共同覆盖范围**：只处理两张地图都覆盖的区域，其余忽略。
2. **网格分片**：取两图全部矩形的 x/y 边坐标切轴对齐网格；每个网格
   单元按“图一地类、图二地类”的**有序组合**标记。
3. **共边才相连**：相同有序组合的单元仅在共享一条边（四连通）时合并
   为一个片；只共一个角点的单元仍是不同片。
4. **一致片直接供数**：两图地类相同的片，状态为“一致入库”。
5. **分歧片核定**：片内**至少一个**实测点，且片内**所有**点都支持
   同一张图（点地类等于该图地类），才采用该图地类；否则列为
   “待补测”。点互相矛盾、或点地类两图都不是，同样待补测。
6. **片边点不计**：点的 x 或 y 恰好落在任一切网边坐标上（含片外环
   与内部边）即判定为片边点，不作为任何片的证据；覆盖区外的点单列。
7. **整份重报沿用**：以“规范化 WKT 片界 + 两图有序地类组合”的
   SHA-256 指纹识别未变片，沿用原核定结果，**不重读片内实测点**；
   指纹变化的片才按本次点表重新核定。
8. **一起生效、失败保留原状**：计算全部成功后才开启事务，单事务写入
   新批次并翻转当前标记；任何失败整体回滚，旧批次与历史结论不变。

## CSV 格式

地类图（两张同结构）：

| 列 | 含义 |
|---|---|
| `id` | 矩形编号（图内唯一） |
| `class` | 地类（地类码或名称均可） |
| `xmin,ymin,xmax,ymax` | 米制坐标下的轴对齐边界，要求 xmax>xmin、ymax>ymin |

实测点表：`id,x,y,class`（x/y 为同一米制坐标，class 为实测地类）。

兼容中文表头（如 `编号/地类/最小x/最小y/最大x/最大y`、
`点号/坐标x/坐标y/地类`），UTF-8（含 BOM）、UTF-16、GB18030 编码自动识别。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `tidalflat_data`（容器内 `/data/tidalflat.db`），
容器重建数据不丢；生产环境请修改 `SECRET_KEY`。

### 本地运行

```bash
python3 -m venv .venv --without-pip   # 无 ensurepip 的系统先 bootstrap pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m app                # 开发服务器，端口 8000
# 或与容器一致的 WSGI 方式：
.venv/bin/gunicorn --bind 0.0.0.0:8000 run:app
```

数据库路径由环境变量 `DB_PATH` 控制，默认 `./data/tidalflat.db`。

## 页面与接口

| 路径 | 说明 |
|---|---|
| `GET /` | 上传表单 + 历史批次列表 |
| `POST /upload` | 上传三份 CSV（multipart：`map1`、`map2`、`points`） |
| `GET /batches/<id>` | 批次详情：状态统计、SVG 分片图、分片/点清单 |
| `GET /batches/<id>/pending` | 待补测片边界图与清单 |
| `GET /batches/<id>/download/ingest.csv` | **可入库地类 + WKT 边界**（一致片与已采用片） |
| `GET /batches/<id>/download/pending.csv` | 待补测片 WKT 边界（供外业补测） |

下载 CSV 为带 BOM 的 UTF-8，`wkt` 列为规范化 WKT
（`POLYGON`/`MULTIPOLYGON`，坐标保留 9 位小数，多部件按坐标排序）。

## 样例与测试

```bash
.venv/bin/python samples/generate_samples.py     # 生成 samples/data/*.csv
.venv/bin/python -m unittest discover -s tests   # 18 个单元/端到端测试
```

## 目录结构

```
app/
  geometry.py   解析、矩形重叠校验、网格分片、四连通合并、点归属与核定
  wktutil.py    规范化 WKT 与片指纹（重报沿用依据）
  db.py         SQLite schema、批次事务、历史结论表
  service.py    计算→沿用判定→事务落库 的编排
  mapsvg.py     分片边界 SVG 渲染（状态着色 + 实测点）
  csvout.py     可入库 / 待补测 CSV 生成
  templates/    Jinja2 页面
tests/          单元测试与 Flask 端到端测试
samples/        演示数据生成脚本
```
