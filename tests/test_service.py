import io
import os
import tempfile
import unittest

from app import create_app
from app import db as dbmod
from app.geometry import STATUS_ADOPT_1, STATUS_PENDING
from app.service import process_upload


def map_csv(rows):
    buf = io.StringIO()
    buf.write("id,class,xmin,ymin,xmax,ymax\n")
    for r in rows:
        buf.write(",".join(str(v) for v in r) + "\n")
    return buf.getvalue().encode("utf-8")


def point_csv(rows):
    buf = io.StringIO()
    buf.write("id,x,y,class\n")
    for r in rows:
        buf.write(",".join(str(v) for v in r) + "\n")
    return buf.getvalue().encode("utf-8")


MAP1 = [("M1-1", "A", 0, 0, 50, 100), ("M1-2", "B", 50, 0, 100, 100)]
MAP2 = [
    ("M2-1", "A", 0, 0, 50, 50),
    ("M2-2", "B", 50, 0, 100, 50),
    ("M2-3", "A", 50, 50, 100, 100),
    ("M2-4", "B", 0, 50, 50, 100),
]


class ReuseAndTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.conn = dbmod.connect(self.db_path)
        dbmod.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _upload(self, pts, names=("m1.csv", "m2.csv", "p.csv")):
        return process_upload(
            self.conn,
            raw_map_1=map_csv(MAP1),
            raw_map_2=map_csv(MAP2),
            raw_points=point_csv(pts),
            filenames=names,
        )

    def test_unchanged_piece_reuses_result_without_rereading_points(self):
        # 首报：分歧片 B/A 凭两点采用图一
        bid1, stats1 = self._upload([("P1", 70, 75, "B"), ("P2", 85, 80, "B")])
        rows1 = {r["piece_code"]: r for r in dbmod.list_pieces(self.conn, bid1)}
        ba1 = next(r for r in rows1.values()
                   if r["class_1"] == "B" and r["class_2"] == "A")
        self.assertEqual(ba1["status"], STATUS_ADOPT_1)
        self.assertEqual(ba1["source"], "new")

        # 重报：地图完全不变，点表改成与图二一致（且删掉一个点）。
        # 未变片必须沿用“采用图一”，不被新点改写。
        bid2, stats2 = self._upload([("P1", 70, 75, "A")],
                                    names=("m1b.csv", "m2b.csv", "pb.csv"))
        self.assertEqual(stats2["n_reused"], 4)
        self.assertEqual(stats2["n_new"], 0)
        rows2 = {r["piece_code"]: r for r in dbmod.list_pieces(self.conn, bid2)}
        ba2 = next(r for r in rows2.values()
                   if r["class_1"] == "B" and r["class_2"] == "A")
        self.assertEqual(ba2["status"], STATUS_ADOPT_1)
        self.assertEqual(ba2["source"], "reused")
        # 旧批次仍是完整历史，当前批次已翻转
        self.assertIsNone(dbmod.get_batch(self.conn, bid1)["is_current"] or None)
        self.assertEqual(dbmod.get_current_batch_id(self.conn), bid2)

    def test_changed_piece_is_readjudicated_while_others_reused(self):
        self._upload([("P1", 70, 75, "B"), ("P2", 85, 80, "B")])
        # 改图一右半的地类：B -> C，所有片指纹变化，重新核定
        map1_c = [("M1-1", "A", 0, 0, 50, 100),
                  ("M1-2", "C", 50, 0, 100, 100)]
        bid, stats = process_upload(
            self.conn,
            raw_map_1=map_csv(map1_c),
            raw_map_2=map_csv(MAP2),
            raw_points=point_csv([("P1", 70, 75, "A")]),
            filenames=("m1.csv", "m2.csv", "p.csv"),
        )
        # 左半 (A,A)/(A,B) 的几何与地类组合未变 -> 沿用；右半为新组合
        self.assertGreaterEqual(stats["n_reused"], 2)
        self.assertGreaterEqual(stats["n_new"], 1)
        ca = next(r for r in dbmod.list_pieces(self.conn, bid)
                  if r["class_1"] == "C" and r["class_2"] == "A")
        # 新点支持图二 A
        self.assertEqual(ca["status"], "采用图二")

    def test_failed_report_keeps_previous_batch_intact(self):
        bid1, _ = self._upload([("P1", 70, 75, "B")])
        before = dbmod.list_pieces(self.conn, bid1)

        # 用无法绑定的参数对象触发保存阶段异常
        original = dbmod.now_iso
        dbmod.now_iso = lambda: object()  # type: ignore[assignment]
        try:
            with self.assertRaises(Exception):
                self._upload([("P1", 70, 75, "B")])
        finally:
            dbmod.now_iso = original  # type: ignore[assignment]

        # 当前批次仍是旧批次，数据完好，没有产生半截新批次
        self.assertEqual(dbmod.get_current_batch_id(self.conn), bid1)
        after = dbmod.list_pieces(self.conn, bid1)
        self.assertEqual([dict(r) for r in before],
                         [dict(r) for r in after])
        self.assertEqual(
            len(self.conn.execute("SELECT * FROM batches").fetchall()), 1
        )

    def test_input_error_before_transaction_leaves_db_untouched(self):
        from app.errors import InputError
        bid1, _ = self._upload([("P1", 70, 75, "B")])
        bad_map = map_csv([("R1", "A", 0, 0, 10, 10),
                           ("R2", "A", 5, 5, 20, 20)])  # 互相重叠
        with self.assertRaises(InputError):
            process_upload(
                self.conn, raw_map_1=bad_map, raw_map_2=map_csv(MAP2),
                raw_points=point_csv([("P1", 1, 1, "A")]),
                filenames=("x", "y", "z"),
            )
        self.assertEqual(dbmod.get_current_batch_id(self.conn), bid1)

    def test_subdivided_rematch_reuses_by_fingerprint_not_piece_code(self):
        # 首报：整块 A/B 分歧，点支持图一
        m1 = [("R1", "A", 0, 0, 100, 100)]
        m2 = [("R2", "B", 0, 0, 100, 100)]
        bid1, _ = process_upload(
            self.conn, raw_map_1=map_csv(m1), raw_map_2=map_csv(m2),
            raw_points=point_csv([("P1", 50, 50, "A")]),
            filenames=("m1.csv", "m2.csv", "p.csv"),
        )
        first = dbmod.list_pieces(self.conn, bid1)[0]
        self.assertEqual(first["status"], "采用图一")

        # 重报：图二拆成两个共边矩形（同地类 B），网格多一条内部边，
        # 但片界与组合未变；点表故意全部支持图二，原结论仍须沿用
        m2_split = [("R2", "B", 0, 0, 50, 100),
                    ("R3", "B", 50, 0, 100, 100)]
        bid2, stats = process_upload(
            self.conn, raw_map_1=map_csv(m1), raw_map_2=map_csv(m2_split),
            raw_points=point_csv([("P1", 25, 25, "B"),
                                  ("P2", 75, 75, "B")]),
            filenames=("m1.csv", "m2.csv", "p2.csv"),
        )
        self.assertEqual(stats["n_reused"], 1)
        again = dbmod.list_pieces(self.conn, bid2)[0]
        self.assertEqual(again["source"], "reused")
        self.assertEqual(again["status"], "采用图一")
        self.assertEqual(again["fingerprint"], first["fingerprint"])


class FlaskEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "web.db")
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, points_rows):
        return self.client.post(
            "/upload",
            data={
                "map1": (io.BytesIO(map_csv(MAP1)), "map1.csv"),
                "map2": (io.BytesIO(map_csv(MAP2)), "map2.csv"),
                "points": (io.BytesIO(point_csv(points_rows)), "points.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    def test_full_flow_pages_and_downloads(self):
        resp = self._post([("P1", 70, 75, "B"), ("P2", 85, 80, "B"),
                           ("P3", 25, 50, "A")])
        self.assertEqual(resp.status_code, 200)
        self.assertIn("批次 #1 已生效".encode(), resp.data)

        # 待补测页
        resp = self.client.get("/batches/1/pending")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("待补测".encode(), resp.data)

        # 可入库 CSV：含一致片与采用片，不含待补测，WKT 列存在
        resp = self.client.get("/batches/1/download/ingest.csv")
        self.assertEqual(resp.status_code, 200)
        text = resp.data.decode("utf-8-sig")
        self.assertIn("adopted_class", text)
        self.assertIn("POLYGON", text)
        self.assertNotIn(STATUS_PENDING, text)
        ingest_rows = [ln for ln in text.strip().split("\n")[1:] if ln]
        self.assertEqual(len(ingest_rows), 3)  # 2 一致 + 1 采用

        # 待补测 CSV：仅 1 个 A/B 片
        resp = self.client.get("/batches/1/download/pending.csv")
        pending_text = resp.data.decode("utf-8-sig")
        pending_rows = [ln for ln in pending_text.strip().split("\n")[1:] if ln]
        self.assertEqual(len(pending_rows), 1)

        # SVG 地图
        resp = self.client.get("/batches/1")
        self.assertIn(b"<svg", resp.data)
        self.assertIn("采用图一".encode(), resp.data)

        # 重报后沿用
        resp = self._post([("P1", 70, 75, "A")])
        self.assertIn("沿用 4".encode(), resp.data)

    def test_missing_file_flashes_error(self):
        resp = self.client.post(
            "/upload",
            data={"map1": (io.BytesIO(map_csv(MAP1)), "map1.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("缺少文件".encode(), resp.data)


if __name__ == "__main__":
    unittest.main()
