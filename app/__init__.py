"""Flask 应用工厂与路由。"""

from __future__ import annotations

import json
import os

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from . import csvout, db as dbmod, mapsvg
from .errors import InputError
from .geometry import STATUS_PENDING
from .service import process_upload

MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 64 MB


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
    app.config["DB_PATH"] = db_path or os.environ.get(
        "DB_PATH", os.path.join(os.getcwd(), "data", "tidalflat.db")
    )

    @app.teardown_appcontext
    def _close(_exc):
        conn = g.pop("db_conn", None)
        if conn is not None:
            conn.close()

    def conn():
        if "db_conn" not in g:
            g.db_conn = dbmod.connect(app.config["DB_PATH"])
            dbmod.init_db(g.db_conn)
        return g.db_conn

    @app.errorhandler(InputError)
    def handle_input_error(err: InputError):
        for msg in err.messages:
            flash(msg, "error")
        return redirect(url_for("index"))

    @app.errorhandler(413)
    def too_large(_e):
        flash("上传文件超过 64MB 上限", "error")
        return redirect(url_for("index"))

    @app.route("/")
    def index():
        batches = dbmod.list_batches(conn())
        current_id = dbmod.get_current_batch_id(conn())
        return render_template(
            "index.html", batches=batches, current_id=current_id
        )

    @app.route("/upload", methods=["POST"])
    def upload():
        files = {}
        for key, label in (
            ("map1", "图一（地类图 CSV）"),
            ("map2", "图二（地类图 CSV）"),
            ("points", "实测点表 CSV"),
        ):
            f = request.files.get(key)
            if f is None or not f.filename:
                raise InputError(f"缺少文件：{label}")
            files[key] = (f.read(), f.filename)
        batch_id, stats = process_upload(
            conn(),
            raw_map_1=files["map1"][0],
            raw_map_2=files["map2"][0],
            raw_points=files["points"][0],
            filenames=(
                files["map1"][1], files["map2"][1], files["points"][1]
            ),
        )
        flash(
            f"批次 #{batch_id} 已生效：分片 {stats['n_pieces']} 个，"
            f"沿用 {stats.get('n_reused', 0)} 个，新核定 {stats.get('n_new', 0)} 个。",
            "ok",
        )
        return redirect(url_for("batch_detail", batch_id=batch_id))

    @app.route("/batches/<int:batch_id>")
    def batch_detail(batch_id: int):
        c = conn()
        batch = dbmod.get_batch(c, batch_id)
        if batch is None:
            abort(404)
        pieces = dbmod.list_pieces(c, batch_id)
        points = dbmod.list_points(c, batch_id)
        counts = dbmod.point_counts_by_piece(c, batch_id)
        stats = json.loads(batch["stats_json"])
        svg = mapsvg.render_svg(
            pieces, points, title=f"批次 #{batch_id} 全部分片"
        )
        return render_template(
            "batch.html",
            batch=batch, stats=stats, pieces=pieces, points=points,
            point_counts=counts, svg=svg,
        )

    @app.route("/batches/<int:batch_id>/pending")
    def pending_view(batch_id: int):
        c = conn()
        batch = dbmod.get_batch(c, batch_id)
        if batch is None:
            abort(404)
        pending = dbmod.list_pieces(c, batch_id, status=STATUS_PENDING)
        points = dbmod.list_points(c, batch_id)
        counts = dbmod.point_counts_by_piece(c, batch_id)
        svg = mapsvg.render_svg(
            pending, points, only_pending=True,
            title=f"批次 #{batch_id} 待补测片边界",
        )
        return render_template(
            "pending.html", batch=batch, pieces=pending,
            point_counts=counts, svg=svg,
        )

    @app.route("/batches/<int:batch_id>/download/ingest.csv")
    def download_ingest(batch_id: int):
        c = conn()
        if dbmod.get_batch(c, batch_id) is None:
            abort(404)
        pieces = dbmod.list_pieces(c, batch_id)
        counts = dbmod.point_counts_by_piece(c, batch_id)
        data = csvout.ingest_csv(pieces, counts)
        return Response(
            data,
            mimetype="text/csv",
            headers={
                "Content-Disposition":
                    f"attachment; filename=ingest_batch_{batch_id}.csv"
            },
        )

    @app.route("/batches/<int:batch_id>/download/pending.csv")
    def download_pending(batch_id: int):
        c = conn()
        if dbmod.get_batch(c, batch_id) is None:
            abort(404)
        pieces = dbmod.list_pieces(c, batch_id, status=STATUS_PENDING)
        counts = dbmod.point_counts_by_piece(c, batch_id)
        data = csvout.pending_csv(pieces, counts)
        return Response(
            data,
            mimetype="text/csv",
            headers={
                "Content-Disposition":
                    f"attachment; filename=pending_batch_{batch_id}.csv"
            },
        )

    return app
