import io
import unittest

from shapely.geometry import box

from app.errors import InputError
from app.geometry import (
    STATUS_ADOPT_1,
    STATUS_ADOPT_2,
    STATUS_AGREED,
    STATUS_PENDING,
    build_pieces,
    parse_map_csv,
    parse_point_csv,
    run_pipeline,
)
from app.wktutil import canonical_wkt, piece_fingerprint


def map_csv(rows, header=("id", "class", "xmin", "ymin", "xmax", "ymax")):
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for r in rows:
        buf.write(",".join(str(v) for v in r) + "\n")
    return buf.getvalue().encode("utf-8")


def point_csv(rows):
    buf = io.StringIO()
    buf.write("id,x,y,class\n")
    for r in rows:
        buf.write(",".join(str(v) for v in r) + "\n")
    return buf.getvalue().encode("utf-8")


# 演示场景：图一左右分(A|B)，图二四象限(A|B|A|B)
MAP1 = [("M1-1", "A", 0, 0, 50, 100), ("M1-2", "B", 50, 0, 100, 100)]
MAP2 = [
    ("M2-1", "A", 0, 0, 50, 50),
    ("M2-2", "B", 50, 0, 100, 50),
    ("M2-3", "A", 50, 50, 100, 100),
    ("M2-4", "B", 0, 50, 50, 100),
]


class PipelineTests(unittest.TestCase):
    def test_four_quadrant_pieces_and_statuses(self):
        # 象限（y 向上）：左下 A/A 一致，右下 B/B 一致，
        # 右上 B/A 分歧，左上 A/B 分歧。B/A 片两点都支持图一 B。
        points = point_csv([
            ("P1", 70, 75, "B"), ("P2", 85, 80, "B"),
            ("P3", 25, 50, "A"),   # 片边点，不计
            ("P4", 150, 150, "A"),  # 覆盖区外
            ("P5", 25, 25, "A"),
        ])
        pieces, pts, stats = run_pipeline(map_csv(MAP1), map_csv(MAP2), points)
        self.assertEqual(len(pieces), 4)
        by_combo = {(p.class_1, p.class_2): p for p in pieces}
        self.assertEqual(by_combo[("A", "A")].status, STATUS_AGREED)
        self.assertEqual(by_combo[("B", "B")].status, STATUS_AGREED)
        self.assertEqual(by_combo[("B", "A")].status, STATUS_ADOPT_1)
        self.assertEqual(by_combo[("B", "A")].adopted_class, "B")
        self.assertEqual(by_combo[("A", "B")].status, STATUS_PENDING)
        self.assertIsNone(by_combo[("A", "B")].adopted_class)

        placement = {p.point_id: p.placement for p in pts}
        self.assertEqual(placement["P3"], "boundary")   # 片边不计
        self.assertEqual(placement["P4"], "outside")
        self.assertEqual(placement["P1"], "interior")
        # 待补测片没有片内点
        self.assertEqual(by_combo[("A", "B")].point_ids, [])
        self.assertEqual(stats["point_placement"]["boundary"], 1)
        self.assertEqual(stats["point_placement"]["outside"], 1)

    def test_adopt_map2_when_all_points_support_map2(self):
        points = point_csv([("P1", 70, 75, "A"), ("P2", 85, 80, "A")])
        pieces, _, _ = run_pipeline(map_csv(MAP1), map_csv(MAP2), points)
        ba = next(p for p in pieces if (p.class_1, p.class_2) == ("B", "A"))
        self.assertEqual(ba.status, STATUS_ADOPT_2)
        self.assertEqual(ba.adopted_class, "A")

    def test_conflicting_points_means_pending(self):
        points = point_csv([("P1", 70, 75, "B"), ("P2", 85, 80, "A")])
        pieces, _, _ = run_pipeline(map_csv(MAP1), map_csv(MAP2), points)
        ba = next(p for p in pieces if (p.class_1, p.class_2) == ("B", "A"))
        self.assertEqual(ba.status, STATUS_PENDING)

    def test_points_support_neither_map_means_pending(self):
        points = point_csv([("P1", 70, 75, "C"), ("P2", 85, 80, "C")])
        pieces, _, _ = run_pipeline(map_csv(MAP1), map_csv(MAP2), points)
        ba = next(p for p in pieces if (p.class_1, p.class_2) == ("B", "A"))
        self.assertEqual(ba.status, STATUS_PENDING)

    def test_only_common_coverage_is_processed(self):
        m1 = map_csv([("R1", "A", 0, 0, 100, 100)])
        m2 = map_csv([("R2", "X", 40, 40, 140, 140)])
        pts = point_csv([("P1", 70, 70, "A")])
        pieces, _, _ = run_pipeline(m1, m2, pts)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].geom.bounds, (40.0, 40.0, 100.0, 100.0))

    def test_corner_touch_not_connected_but_edge_touch_connected(self):
        # 仅角接触：两个 A/X 单元在 (10,10) 共点不共边 -> 2 个片
        m1_corner = map_csv([
            ("R1", "A", 0, 0, 10, 10), ("R2", "A", 10, 10, 20, 20),
        ])
        m2 = map_csv([("R3", "X", 0, 0, 20, 20)])
        pieces, *_ = build_pieces(parse_map_csv(m1_corner, "图一"),
                                  parse_map_csv(m2, "图二"))
        self.assertEqual(len(pieces), 2)

        # 共边：两个 A/X 单元共边 -> 合并为 1 个片
        m1_edge = map_csv([
            ("R1", "A", 0, 0, 10, 20), ("R2", "A", 10, 0, 20, 20),
        ])
        pieces, *_ = build_pieces(parse_map_csv(m1_edge, "图一"),
                                  parse_map_csv(m2, "图二"))
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].geom.bounds, (0.0, 0.0, 20.0, 20.0))

    def test_overlapping_rects_rejected(self):
        bad = map_csv([
            ("R1", "A", 0, 0, 10, 10), ("R2", "A", 5, 5, 15, 15),
        ])
        with self.assertRaises(InputError):
            parse_map_csv(bad, "图一")

    def test_bad_coords_and_missing_columns_rejected(self):
        with self.assertRaises(InputError):
            parse_map_csv(map_csv([("R1", "A", 10, 10, 5, 20)]), "图一")
        with self.assertRaises(InputError):
            parse_map_csv(b"foo,bar\n1,2\n", "图一")
        with self.assertRaises(InputError):
            parse_point_csv(b"id,x,y\n1,2,3\n")

    def test_chinese_headers_accepted(self):
        raw = "编号,地类,最小x,最小y,最大x,最大y\nR1,A,0,0,10,10\n".encode()
        rects = parse_map_csv(raw, "图一")
        self.assertEqual(rects[0].land_class, "A")
        rawp = "点号,坐标x,坐标y,地类\nP1,5,5,A\n".encode()
        pts = parse_point_csv(rawp)
        self.assertEqual(pts[0].point_id, "P1")

    def test_canonical_wkt_and_fingerprint_stable(self):
        m1 = map_csv([("R1", "A", 0, 0, 10, 20), ("R2", "A", 10, 0, 20, 20)])
        m2 = map_csv([("R3", "X", 0, 0, 20, 20)])
        pieces, *_ = build_pieces(parse_map_csv(m1, "图一"),
                                  parse_map_csv(m2, "图二"))
        w1 = canonical_wkt(pieces[0].geom)
        # 同一几何重复构造，WKT 与指纹必须稳定
        fp1 = piece_fingerprint("A", "X", pieces[0].geom)
        fp2 = piece_fingerprint("A", "X", pieces[0].geom)
        self.assertEqual(fp1, fp2)
        self.assertTrue(w1.startswith("POLYGON"))
        # 任一地类变化都会改变指纹
        self.assertNotEqual(fp1, piece_fingerprint("A", "Y", pieces[0].geom))

    def test_multipolygon_canonical_form(self):
        from shapely.geometry import MultiPolygon
        from app.wktutil import canonical_wkt
        mp = MultiPolygon([
            box(0, 0, 10, 10), box(100, 100, 120, 120),
        ])
        w1 = canonical_wkt(mp)
        self.assertTrue(w1.startswith("MULTIPOLYGON"))
        # 部件顺序调换，规范化 WKT 必须相同
        mp2 = MultiPolygon([
            box(100, 100, 120, 120), box(0, 0, 10, 10),
        ])
        self.assertEqual(w1, canonical_wkt(mp2))
        # WKT 可被 shapely 无损读回，面积一致
        from shapely import wkt as swkt
        self.assertAlmostEqual(swkt.loads(w1).area, 500.0)

    def test_canonical_wkt_invariant_under_subdivision(self):
        """同一边界无论由几个共边网格单元合并，规范化 WKT/指纹必须相同。"""
        from shapely import wkt as swkt
        from app.wktutil import canonical_wkt, piece_fingerprint
        one = map_csv([("R1", "A", 0, 0, 100, 100)])
        two = map_csv([
            ("R1", "A", 0, 0, 50, 100), ("R2", "A", 50, 0, 100, 100),
        ])
        base = map_csv([("R3", "B", 0, 0, 100, 100)])
        g1 = build_pieces(parse_map_csv(one, "图一"),
                          parse_map_csv(base, "图二"))[0][0].geom
        g2 = build_pieces(parse_map_csv(two, "图一"),
                          parse_map_csv(base, "图二"))[0][0].geom
        self.assertEqual(canonical_wkt(g1), canonical_wkt(g2))
        self.assertEqual(
            piece_fingerprint("A", "B", g1),
            piece_fingerprint("A", "B", g2),
        )
        # 输出 WKT 仍可被标准解析器读回且面积正确
        self.assertAlmostEqual(swkt.loads(canonical_wkt(g2)).area, 10000.0)

    def test_no_common_coverage_rejected(self):
        m1 = map_csv([("R1", "A", 0, 0, 10, 10)])
        m2 = map_csv([("R2", "X", 100, 100, 110, 110)])
        with self.assertRaises(InputError):
            run_pipeline(m1, m2, point_csv([("P1", 5, 5, "A")]))


if __name__ == "__main__":
    unittest.main()
