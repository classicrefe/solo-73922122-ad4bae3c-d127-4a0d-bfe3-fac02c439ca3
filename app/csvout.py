"""可下载 CSV 生成。"""

from __future__ import annotations

import csv
import io

from .geometry import ADOPTED_STATUSES, STATUS_PENDING


def _to_csv_bytes(header, rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    # 带 UTF-8 BOM，便于 Excel 直接打开中文
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def ingest_csv(piece_rows, point_counts) -> bytes:
    """可入库地类及 WKT 边界 CSV：一致片 + 已核定采用片，不含待补测。"""
    header = [
        "piece_id", "adopted_class", "status", "source",
        "support_point_count", "area_m2", "wkt",
    ]
    rows = []
    for r in piece_rows:
        if r["status"] not in ADOPTED_STATUSES:
            continue
        rows.append([
            r["piece_code"], r["adopted_class"], r["status"], r["source"],
            point_counts.get(r["piece_code"], 0),
            f"{r['area_m2']:.4f}", r["wkt"],
        ])
    return _to_csv_bytes(header, rows)


def pending_csv(piece_rows, point_counts) -> bytes:
    """待补测片边界 CSV（供外业补测使用）。"""
    header = [
        "piece_id", "class_map1", "class_map2", "inside_point_count",
        "area_m2", "wkt",
    ]
    rows = []
    for r in piece_rows:
        if r["status"] != STATUS_PENDING:
            continue
        rows.append([
            r["piece_code"], r["class_1"], r["class_2"],
            point_counts.get(r["piece_code"], 0),
            f"{r['area_m2']:.4f}", r["wkt"],
        ])
    return _to_csv_bytes(header, rows)
