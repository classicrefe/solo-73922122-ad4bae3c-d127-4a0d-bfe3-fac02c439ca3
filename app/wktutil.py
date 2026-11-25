"""WKT 规范化与分片指纹。

指纹用于“整份重报”时识别“片界与两图各自地类均未变”的分片，
从而沿用原核定结果、无需重读片内实测点。
"""

from __future__ import annotations

import hashlib

from shapely import wkt as shapely_wkt

# 米制坐标保留到纳米（9 位小数），输出稳定且远高于实测精度需求
COORD_QUANTIZE = 9


def _fmt(value: float) -> str:
    return f"{value:.{COORD_QUANTIZE}f}".rstrip("0").rstrip(".")


def canonical_wkt(geom) -> str:
    """规范化 WKT：消除共线冗余点、四舍五入坐标、MultiPolygon 部件排序。

    网格细分后再合并会在边界上留下共线顶点（如边中点），simplify(0)
    在保持拓扑与面积不变的前提下去掉这些点，使“同一边界”无论来自
    多少个网格单元都得到相同字符串。
    """
    g = geom.simplify(0, preserve_topology=False)
    if g.geom_type == "Polygon":
        return "POLYGON " + _polygon_wkt(g)
    if g.geom_type == "MultiPolygon":
        parts = sorted(_polygon_wkt(p) for p in g.geoms)
        return "MULTIPOLYGON (" + ", ".join(parts) + ")"
    # 兜底：经 shapely 自身输出后再量化
    return shapely_wkt.dumps(g, rounding_precision=COORD_QUANTIZE)


def _ring_wkt(coords) -> str:
    pts = ", ".join(f"{_fmt(x)} {_fmt(y)}" for x, y in coords)
    return f"({pts})"


def _norm_ring(coords, ccw: bool = True):
    """环规范化：闭合环去掉重复末点，统一方向，并旋转到字典序最小起点。

    外环 ccw=True（逆时针），内环 ccw=False（顺时针），符合 OGC 约定。
    """
    pts = [(float(x), float(y)) for x, y in coords]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    # 有向面积：正为逆时针
    area2 = sum(
        pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
        for i in range(len(pts) - 1)
    ) + pts[-1][0] * pts[0][1] - pts[0][0] * pts[-1][1]
    want_positive = ccw
    if (area2 > 0) != want_positive:
        pts.reverse()
    # 旋转到字典序最小的顶点作为起点
    start = min(range(len(pts)), key=lambda k: pts[k])
    rotated = pts[start:] + pts[:start]
    rotated.append(rotated[0])  # 重新闭合
    return rotated


def _polygon_wkt(poly) -> str:
    rings = [_norm_ring(poly.exterior.coords, ccw=True)]
    rings.extend(_norm_ring(r.coords, ccw=False) for r in poly.interiors)
    return "(" + ", ".join(_ring_wkt(r) for r in rings) + ")"


def piece_fingerprint(class_1: str, class_2: str, geom) -> str:
    """片界 + 两图有序地类组合的稳定指纹。"""
    payload = f"{class_1}\x1f{class_2}\x1f{canonical_wkt(geom)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def area_of(geom) -> float:
    return float(geom.area)
