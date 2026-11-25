"""服务端 SVG 渲染：分片边界按状态着色，叠加实测点。"""

from __future__ import annotations

import html

from shapely import wkt as shapely_wkt

STATUS_COLORS = {
    "一致入库": "#2e7d32",   # 绿
    "采用图一": "#1565c0",   # 蓝
    "采用图二": "#6a1b9a",   # 紫
    "待补测": "#c62828",     # 红
}
POINT_COLORS = {
    "interior": "#111111",
    "boundary": "#ef6c00",
    "outside": "#9e9e9e",
}


def _bounds(piece_rows):
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    geoms = []
    for row in piece_rows:
        g = shapely_wkt.loads(row["wkt"])
        geoms.append(g)
        b = g.bounds
        minx, miny = min(minx, b[0]), min(miny, b[1])
        maxx, maxy = max(maxx, b[2]), max(maxy, b[3])
    return geoms, (minx, miny, maxx, maxy)


def _transform(bounds, width, height, pad=30):
    minx, miny, maxx, maxy = bounds
    span_x = max(maxx - minx, 1e-9)
    span_y = max(maxy - miny, 1e-9)
    scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)

    def tx(x):
        return pad + (x - minx) * scale

    def ty(y):
        # SVG y 轴向下，地理 y 向上，需翻转
        return height - pad - (y - miny) * scale

    return tx, ty, scale


def _ring_path(coords, tx, ty):
    pts = " ".join(f"{tx(x):.2f},{ty(y):.2f}" for x, y in coords)
    return f"M {pts} Z"


def _geom_path(geom, tx, ty):
    paths = []
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    for poly in polys:
        paths.append(_ring_path(poly.exterior.coords, tx, ty))
        for interior in poly.interiors:
            # 内环用反向填充规则（fill-rule=evenodd）自动挖洞
            paths.append(_ring_path(interior.coords, tx, ty))
    return " ".join(paths)


def render_svg(piece_rows, point_rows, *, width=980, height=720,
               only_pending=False, title="分片边界图"):
    """生成自包含 SVG 字符串。piece_rows / point_rows 为 sqlite Row 序列。"""
    if not piece_rows:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}"><text x="20" y="40">暂无分片数据</text></svg>'
        )
    geoms, bounds = _bounds(piece_rows)
    tx, ty, scale = _transform(bounds, width, height)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" font-family="sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#fafafa"/>',
        f'<text x="16" y="26" font-size="16" font-weight="bold">{html.escape(title)}</text>',
    ]
    # 分片
    for row, geom in zip(piece_rows, geoms):
        color = STATUS_COLORS.get(row["status"], "#555555")
        d = _geom_path(geom, tx, ty)
        label_x, label_y = tx(geom.representative_point().x), ty(
            geom.representative_point().y
        )
        parts.append(
            f'<g><path d="{d}" fill="{color}" fill-opacity="0.18" '
            f'stroke="{color}" stroke-width="1.4" fill-rule="evenodd">'
            f'<title>{html.escape(row["piece_code"])}｜{html.escape(row["status"])}'
            f'｜图一:{html.escape(row["class_1"])} 图二:{html.escape(row["class_2"])}'
            f'</title></path>'
            f'<text x="{label_x:.1f}" y="{label_y:.1f}" font-size="10" '
            f'text-anchor="middle" fill="#222">{html.escape(row["piece_code"])}</text></g>'
        )
    # 实测点（待补测视图中点可能不属于任何片，仍弱化展示）
    for pt in point_rows:
        cx, cy = tx(pt["x"]), ty(pt["y"])
        color = POINT_COLORS.get(pt["placement"], "#333")
        r = 3.2 if pt["placement"] == "interior" else 2.4
        parts.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" fill="{color}">'
            f'<title>{html.escape(pt["point_code"])}｜{html.escape(pt["land_class"])}'
            f'｜{html.escape(pt["placement"])}</title></circle>'
        )
    # 图例
    lx, ly = width - 200, 46
    parts.append('<g font-size="11">')
    parts.append(f'<rect x="{lx - 10}" y="{ly - 16}" width="190" height="150" '
                 f'fill="white" stroke="#ccc" opacity="0.92"/>')
    for k, (status, color) in enumerate(STATUS_COLORS.items()):
        parts.append(
            f'<rect x="{lx}" y="{ly + k * 20}" width="14" height="14" '
            f'fill="{color}" fill-opacity="0.25" stroke="{color}"/>'
            f'<text x="{lx + 20}" y="{ly + 11 + k * 20}">{html.escape(status)}</text>'
        )
    base_y = ly + len(STATUS_COLORS) * 20 + 6
    for k, (place, label, color) in enumerate((
        ("interior", "片内点", POINT_COLORS["interior"]),
        ("boundary", "片边点(不计)", POINT_COLORS["boundary"]),
        ("outside", "覆盖区外点", POINT_COLORS["outside"]),
    )):
        parts.append(
            f'<circle cx="{lx + 7}" cy="{base_y + k * 18 + 4}" r="3.4" fill="{color}"/>'
            f'<text x="{lx + 20}" y="{base_y + 8 + k * 18}">{html.escape(label)}</text>'
        )
    parts.append("</g></svg>")
    return "".join(parts)
