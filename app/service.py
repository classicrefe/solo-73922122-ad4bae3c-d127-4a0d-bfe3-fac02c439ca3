"""应用服务：把纯计算流水线、结果沿用与事务持久化串起来。"""

from __future__ import annotations

from . import db as dbmod
from .geometry import run_pipeline
from .wktutil import piece_fingerprint


def process_upload(
    conn,
    *,
    raw_map_1: bytes,
    raw_map_2: bytes,
    raw_points: bytes,
    filenames: tuple[str, str, str],
) -> tuple[int, dict]:
    """处理一次完整上传，返回 (新批次ID, 统计信息)。

    步骤：
      1. 纯计算分片并按本次实测点核定（不触碰数据库）——任何输入错误
         在此抛出，库内原状不变；
      2. 以指纹查历史结论：片界与两图地类均未变的片沿用原结果
         （即使本次实测点变化也不重读），其余保持本次核定；
      3. 单事务写入新批次并翻转 current；写入失败回滚，旧批次保留。
    """
    # 1) 计算（InputError 等直接冒泡，事务尚未开始）
    pieces, points, stats = run_pipeline(raw_map_1, raw_map_2, raw_points)

    # 2) 沿用判定
    for piece in pieces:
        piece.fingerprint = piece_fingerprint(  # type: ignore[attr-defined]
            piece.class_1, piece.class_2, piece.geom
        )
    prior = dbmod.lookup_results(
        conn, [p.fingerprint for p in pieces]  # type: ignore[attr-defined]
    )
    n_reused = dbmod.apply_reuse(pieces, prior)
    stats["n_reused"] = n_reused
    stats["n_new"] = len(pieces) - n_reused

    # 3) 事务落库
    batch_id = dbmod.save_batch(
        conn,
        filenames=filenames,
        pieces=pieces,
        points=points,
        stats=stats,
    )
    return batch_id, stats
