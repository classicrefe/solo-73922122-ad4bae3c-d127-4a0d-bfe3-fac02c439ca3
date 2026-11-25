"""核心领域逻辑：地图矩形解析、共同覆盖区网格分片、四连通合并、实测点核定。

约定
----
* 两张地图均由内部互不重叠的轴对齐矩形（AABB）组成；
* 两图使用同一米制坐标系；
* 仅处理两图都覆盖的区域；
* 按两图地类的有序组合分片，同一组合仅在共边时相连（角接触不相连）；
* 落在片边界上的实测点不计入该片的核定依据。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from shapely.geometry import Point, box
from shapely.strtree import STRtree
from shapely.geometry import MultiPolygon, Polygon

from .errors import InputError

# 网格单元数上限，防止异常输入（海量碎边）导致内存/时间失控。
MAX_GRID_CELLS = 2_000_000

# 状态常量（同时作为数据库与页面展示的统一词表）
STATUS_AGREED = "一致入库"          # 两图地类一致，直接供数
STATUS_ADOPT_1 = "采用图一"         # 分歧片，实测点全部支持图一
STATUS_ADOPT_2 = "采用图二"         # 分歧片，实测点全部支持图二
STATUS_PENDING = "待补测"           # 分歧片，证据不足或证据互相矛盾

ADOPTED_STATUSES = (STATUS_AGREED, STATUS_ADOPT_1, STATUS_ADOPT_2)

# CSV 表头别名（统一小写后匹配），允许中英文表头混用
MAP_ALIASES = {
    "id": ("id", "rect_id", "fid", "编号", "序号", "图斑编号"),
    "class": ("class", "class_name", "land_class", "landclass", "dlbm",
              "dlmc", "地类", "地类码", "地类名称", "类型"),
    "xmin": ("xmin", "minx", "left", "x0", "最小x", "左边界x"),
    "ymin": ("ymin", "miny", "bottom", "y0", "最小y", "下边界y"),
    "xmax": ("xmax", "maxx", "right", "x1", "最大x", "右边界x"),
    "ymax": ("ymax", "maxy", "top", "y1", "最大y", "上边界y"),
}
POINT_ALIASES = {
    "id": ("id", "point_id", "fid", "编号", "序号", "点号"),
    "x": ("x", "xcoord", "coord_x", "lon", "坐标x", "横坐标"),
    "y": ("y", "ycoord", "coord_y", "lat", "坐标y", "纵坐标"),
    "class": ("class", "class_name", "land_class", "landclass", "dlbm",
              "dlmc", "地类", "地类码", "地类名称", "实测地类"),
}


@dataclass(frozen=True)
class Rect:
    """一张地图中的一个轴对齐矩形。"""

    rect_id: str
    land_class: str
    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @property
    def geom(self):
        return box(self.xmin, self.ymin, self.xmax, self.ymax)


@dataclass
class SurveyPoint:
    point_id: str
    x: float
    y: float
    land_class: str
    # 归属结果：interior(片内)/boundary(片边)/outside(共同覆盖区外)
    placement: str | None = None
    piece_id: str | None = None


@dataclass
class Piece:
    """一个最终分片：有序地类组合相同且四连通的网格单元并集。"""

    piece_id: str
    class_1: str
    class_2: str
    geom: Polygon | MultiPolygon
    point_ids: list[str] = field(default_factory=list)
    status: str | None = None
    adopted_class: str | None = None
    source: str = "new"  # new=本次重新核定；reused=沿用原结果

    @property
    def is_agreement(self) -> bool:
        return self.class_1 == self.class_2


# --------------------------------------------------------------------------
# CSV 解析
# --------------------------------------------------------------------------

def decode_csv_bytes(raw: bytes) -> str:
    """按常见编码尝试解码为文本（UTF-8 含 BOM、UTF-16、GBK）。"""
    for enc in ("utf-8-sig", "utf-8", "utf-16", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _resolve_headers(fieldnames, aliases, label):
    """把实际表头映射到标准字段名，返回 dict: 标准名 -> 实际列名。"""
    if not fieldnames:
        raise InputError(f"{label}：文件为空或缺少表头行")
    normalized = { (name or "").strip().lower(): name for name in fieldnames }
    resolved = {}
    for std, names in aliases.items():
        for cand in names:
            if cand in normalized:
                resolved[std] = normalized[cand]
                break
    missing = [std for std in aliases if std not in resolved]
    if missing:
        raise InputError(
            f"{label}：缺少必需列 {', '.join(missing)}；"
            f"识别到的表头为 {', '.join(fieldnames)}"
        )
    return resolved


def _to_float(value, row_no, col, label):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise InputError(f"{label}第 {row_no} 行：列 {col} 的值 {value!r} 不是有效数字")


def parse_map_csv(raw: bytes, label: str) -> list[Rect]:
    """解析一张地类图 CSV，返回 Rect 列表（并校验内部互不重叠）。"""
    text = decode_csv_bytes(raw)
    reader = csv.DictReader(io.StringIO(text))
    cols = _resolve_headers(reader.fieldnames, MAP_ALIASES, label)
    rects: list[Rect] = []
    seen_ids: set[str] = set()
    for row_no, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue  # 跳过空行
        rid = (row.get(cols["id"]) or "").strip() or f"{label}-R{row_no - 1}"
        if rid in seen_ids:
            raise InputError(f"{label}第 {row_no} 行：矩形编号 {rid} 重复")
        seen_ids.add(rid)
        lclass = (row.get(cols["class"]) or "").strip()
        if not lclass:
            raise InputError(f"{label}第 {row_no} 行：地类为空")
        xmin = _to_float(row.get(cols["xmin"]), row_no, cols["xmin"], label)
        ymin = _to_float(row.get(cols["ymin"]), row_no, cols["ymin"], label)
        xmax = _to_float(row.get(cols["xmax"]), row_no, cols["xmax"], label)
        ymax = _to_float(row.get(cols["ymax"]), row_no, cols["ymax"], label)
        if not (xmax > xmin and ymax > ymin):
            raise InputError(
                f"{label}第 {row_no} 行：矩形必须满足 xmax>xmin 且 ymax>ymin"
            )
        rects.append(Rect(rid, lclass, xmin, ymin, xmax, ymax))
    if not rects:
        raise InputError(f"{label}：没有任何有效矩形数据行")
    _validate_non_overlapping(rects, label)
    return rects


def parse_point_csv(raw: bytes) -> list[SurveyPoint]:
    """解析实测点 CSV，返回 SurveyPoint 列表。"""
    text = decode_csv_bytes(raw)
    reader = csv.DictReader(io.StringIO(text))
    cols = _resolve_headers(reader.fieldnames, POINT_ALIASES, "实测点表")
    points: list[SurveyPoint] = []
    seen_ids: set[str] = set()
    for row_no, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        pid = (row.get(cols["id"]) or "").strip() or f"P{row_no - 1}"
        if pid in seen_ids:
            raise InputError(f"实测点表第 {row_no} 行：点编号 {pid} 重复")
        seen_ids.add(pid)
        lclass = (row.get(cols["class"]) or "").strip()
        if not lclass:
            raise InputError(f"实测点表第 {row_no} 行：地类为空")
        x = _to_float(row.get(cols["x"]), row_no, cols["x"], "实测点表")
        y = _to_float(row.get(cols["y"]), row_no, cols["y"], "实测点表")
        points.append(SurveyPoint(pid, x, y, lclass))
    if not points:
        raise InputError("实测点表：没有任何有效点数据行")
    return points


def _validate_non_overlapping(rects: list[Rect], label: str):
    """校验同一地图内部矩形互不重叠（仅边界相接是允许的）。"""
    geoms = [r.geom for r in rects]
    tree = STRtree(geoms)
    for i, rect in enumerate(rects):
        hits = tree.query(geoms[i])
        for j in hits:
            if j <= i:
                continue
            inter = geoms[i].intersection(geoms[j])
            # 面积为 0 的交集（共边/共点）不算重叠
            if not inter.is_empty and inter.area > 0:
                raise InputError(
                    f"{label}：矩形 {rect.rect_id} 与 {rects[j].rect_id} "
                    f"存在面积大于 0 的重叠，违反“内部互不重叠”约定"
                )


# --------------------------------------------------------------------------
# 网格分片
# --------------------------------------------------------------------------

def _class_in_rects(rects, tree, xmin, ymin, xmax, ymax):
    """返回覆盖整个网格单元的矩形地类。

    单元由真实矩形边切出，单元内部不可能再发生矩形切换，因此只需在
    单元内部取一个点查询；STRtree 先按外接框粗筛，再用不等式精确确认。
    """
    cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
    for k in tree.query(Point(cx, cy)):
        r = rects[k]
        if r.xmin <= cx <= r.xmax and r.ymin <= cy <= r.ymax:
            return r.land_class
    return None


def build_pieces(rects_1: list[Rect], rects_2: list[Rect]) -> list[Piece]:
    """以两图所有矩形边坐标切网，按有序地类组合四连通合并为分片。"""
    xs = sorted({v for r in rects_1 + rects_2 for v in (r.xmin, r.xmax)})
    ys = sorted({v for r in rects_1 + rects_2 for v in (r.ymin, r.ymax)})
    nx, ny = len(xs) - 1, len(ys) - 1
    if nx <= 0 or ny <= 0:
        raise InputError("两张地图的覆盖范围无法形成有效网格（共同覆盖区可能为空）")
    if nx * ny > MAX_GRID_CELLS:
        raise InputError(
            f"切分后网格单元数为 {nx * ny}，超过上限 {MAX_GRID_CELLS}，"
            "请检查地图边坐标是否异常细碎"
        )

    tree_1 = STRtree([r.geom for r in rects_1])
    tree_2 = STRtree([r.geom for r in rects_2])

    # cells[j][i] = (class_1, class_2) 或 None（不在共同覆盖区）
    cells: list[list[tuple[str, str] | None]] = [
        [None] * nx for _ in range(ny)
    ]
    n_cells = 0
    for j in range(ny):
        for i in range(nx):
            c1 = _class_in_rects(
                rects_1, tree_1, xs[i], ys[j], xs[i + 1], ys[j + 1]
            )
            if c1 is None:
                continue
            c2 = _class_in_rects(
                rects_2, tree_2, xs[i], ys[j], xs[i + 1], ys[j + 1]
            )
            if c2 is None:
                continue
            cells[j][i] = (c1, c2)
            n_cells += 1
    if n_cells == 0:
        raise InputError("两张地图没有共同覆盖范围，无可处理分片")

    # 四连通洪水填充：同 (c1,c2) 且共边的单元合并
    visited = [[False] * nx for _ in range(ny)]
    pieces: list[Piece] = []
    cell_owner: dict[tuple[int, int], str] = {}
    seq = 0
    for j0 in range(ny):
        for i0 in range(nx):
            if visited[j0][i0] or cells[j0][i0] is None:
                continue
            seq += 1
            piece_code = f"PIECE-{seq:04d}"
            combo = cells[j0][i0]
            stack = [(i0, j0)]
            visited[j0][i0] = True
            member_cells = []
            while stack:
                i, j = stack.pop()
                member_cells.append((i, j))
                cell_owner[(i, j)] = piece_code
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ni, nj = i + di, j + dj
                    if 0 <= ni < nx and 0 <= nj < ny and not visited[nj][ni]:
                        if cells[nj][ni] == combo:
                            visited[nj][ni] = True
                            stack.append((ni, nj))
            geom = unary_union_cells(member_cells, xs, ys)
            pieces.append(
                Piece(
                    piece_id=piece_code,
                    class_1=combo[0],
                    class_2=combo[1],
                    geom=geom,
                )
            )
    return pieces, cells, xs, ys, cell_owner


def unary_union_cells(member_cells, xs, ys):
    """把网格单元并集规范化为 Polygon 或 MultiPolygon（自动消除内部边）。"""
    from shapely.ops import unary_union

    polys = [box(xs[i], ys[j], xs[i + 1], ys[j + 1]) for i, j in member_cells]
    geom = unary_union(polys)
    if geom.geom_type == "Polygon":
        return geom
    if geom.geom_type == "MultiPolygon":
        return geom
    # 理论上 box 的并集只会是 Polygon/MultiPolygon，防御性兜底
    return geom.buffer(0)


# --------------------------------------------------------------------------
# 实测点归属与核定
# --------------------------------------------------------------------------

def _grid_index(edges, value):
    """返回 value 在切网中的位置。

    * 落在某条边上 -> ("edge", 边索引)
    * 落在两条边之间 -> ("inside", 单元索引)
    * 超出边范围 -> ("outside", None)
    """
    import bisect

    pos = bisect.bisect_left(edges, value)
    if pos < len(edges) and edges[pos] == value:
        return "edge", pos
    if 0 < pos < len(edges):
        return "inside", pos - 1
    return "outside", None


def assign_points(points, pieces, cells, xs, ys, cell_owner):
    """把实测点分配到分片。

    直接基于切网边坐标的索引判定：
      * 点的 x 或 y 等于任一网格边坐标（含片的外环与内部边）=> 落在片边，不计；
      * 点位于某个有效网格单元内部 => 归属该单元所属片；
      * 点不在共同覆盖区（无有效单元）=> 覆盖区外。
    “片边点不计”由精确坐标相等判定，不依赖浮点几何谓词。
    就地更新 point.placement / point.piece_id 与 piece.point_ids，返回统计。
    """
    piece_by_id = {p.piece_id: p for p in pieces}
    stats = {"interior": 0, "boundary": 0, "outside": 0}
    for pt in points:
        sx, sy = _grid_index(xs, pt.x), _grid_index(ys, pt.y)
        if sx[0] == "outside" or sy[0] == "outside":
            pt.placement, pt.piece_id = "outside", None
            stats["outside"] += 1
            continue
        if sx[0] == "edge" or sy[0] == "edge":
            pt.placement, pt.piece_id = "boundary", None
            stats["boundary"] += 1
            continue
        i, j = sx[1], sy[1]
        owner = cell_owner.get((i, j))
        if owner is None:
            # 点在包围盒网格内但该单元不属于共同覆盖区
            pt.placement, pt.piece_id = "outside", None
            stats["outside"] += 1
            continue
        pt.placement, pt.piece_id = "interior", owner
        piece_by_id[owner].point_ids.append(pt.point_id)
        stats["interior"] += 1
    return stats


def adjudicate(pieces: list[Piece], points: list[SurveyPoint]) -> None:
    """按规则为每个分片核定状态（就地写 piece.status / adopted_class）。

    * 一致片（两图地类相同）：直接供数；
    * 分歧片：片内至少 1 个实测点，且全部支持同一张图，才采用该图地类；
      无点、点互相矛盾、或点支持的地类两图都不是 => 待补测。
    """
    by_id = {p.point_id: p for p in points}
    for piece in pieces:
        if piece.is_agreement:
            piece.status = STATUS_AGREED
            piece.adopted_class = piece.class_1
            continue
        inside = [by_id[pid] for pid in piece.point_ids]
        if not inside:
            piece.status = STATUS_PENDING
            piece.adopted_class = None
            continue
        support_1 = all(p.land_class == piece.class_1 for p in inside)
        support_2 = all(p.land_class == piece.class_2 for p in inside)
        if support_1 and not support_2:
            piece.status = STATUS_ADOPT_1
            piece.adopted_class = piece.class_1
        elif support_2 and not support_1:
            piece.status = STATUS_ADOPT_2
            piece.adopted_class = piece.class_2
        else:
            # 包含：点互相矛盾、点地类两图都不是、或两图地类同名（已在一致分支处理）
            piece.status = STATUS_PENDING
            piece.adopted_class = None


def run_pipeline(raw_map_1: bytes, raw_map_2: bytes, raw_points: bytes) -> tuple[
    list[Piece], list[SurveyPoint], dict
]:
    """纯计算流水线：三份 CSV 字节 -> 分片（含核定结果）、点、统计。"""
    rects_1 = parse_map_csv(raw_map_1, "图一")
    rects_2 = parse_map_csv(raw_map_2, "图二")
    points = parse_point_csv(raw_points)
    pieces, cells, xs, ys, cell_owner = build_pieces(rects_1, rects_2)
    point_stats = assign_points(points, pieces, cells, xs, ys, cell_owner)
    adjudicate(pieces, points)
    status_counts: dict[str, int] = {}
    for p in pieces:
        status_counts[p.status] = status_counts.get(p.status, 0) + 1
    stats = {
        "n_rects_1": len(rects_1),
        "n_rects_2": len(rects_2),
        "n_points": len(points),
        "n_pieces": len(pieces),
        "status_counts": status_counts,
        "point_placement": point_stats,
    }
    return pieces, points, stats
