"""批次发布、观测补传与结果汇总服务。

并发约定（与 `app/locking.py` 配套）：
- 发布与补传都持有同一把“批次锁”，二者的提交顺序唯一确定；
- 发布把“读观测/分类表”和“写结果”拆成两个短事务，中间的名称解析
  完全在内存进行、不持有数据库事务，因此一个批次解析期间，
  其他批次的补传可以正常提交（SQLite 读事务不会阻塞它们）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .locking import batch_lock
from .models import (
    BatchStatus,
    Observation,
    PublishIssue,
    ResolvedObservation,
    SurveyBatch,
    TaxonRow,
    TaxonomyVersion,
)
from .taxonomy import build_name_index, resolve_all


class PublishError(Exception):
    """发布前置条件不满足（对应 HTTP 4xx）。"""


class BatchNotFoundError(Exception):
    """批次不存在（对应 HTTP 404）。"""


class UploadConflictError(Exception):
    """批次当前状态不允许补传观测（对应 HTTP 409）。"""


# 只有这些状态允许继续上传/修改原始观测
UPLOADABLE_STATUSES = {BatchStatus.DRAFT, BatchStatus.NEEDS_REVIEW}


@dataclass
class PublishOutcome:
    success: bool
    status: BatchStatus
    taxonomy_version_id: int
    message: str
    issues: list[dict]


@dataclass
class UploadOutcome:
    received: int
    inserted: int
    duplicates_skipped: int


def publish_batch(
    db: Session, batch: SurveyBatch, taxonomy_version_id: int
) -> PublishOutcome:
    """按指定分类表版本发布批次。

    并发策略：整段发布持有批次锁（与补传互斥，其他批次不受影响）。
    先在只读短事务中取出观测与分类表快照并释放事务，再在内存中完成
    整批解析，最后在第二个短事务中原子写入结果。任何异常都会回滚，
    旧结果（如有）继续对外提供。
    """
    batch_id = batch.id
    with batch_lock(batch_id):
        # ---- 读阶段（短事务，取完即释放，解析期间不占用数据库锁）----
        version = db.get(TaxonomyVersion, taxonomy_version_id)
        if version is None:
            raise PublishError(f"分类表版本 {taxonomy_version_id} 不存在")

        observations = db.execute(
            select(
                Observation.plot_code,
                Observation.observed_on,
                Observation.raw_species_name,
                Observation.quantity,
            )
            .where(Observation.batch_id == batch_id)
            .order_by(Observation.id)
        ).all()
        if not observations:
            raise PublishError("批次没有任何观测记录，无法发布")

        taxon_rows = db.execute(
            select(TaxonRow.name, TaxonRow.accepted_name, TaxonRow.synonyms_json)
            .where(TaxonRow.taxonomy_version_id == version.id)
        ).all()
        version_id, version_no = version.id, version.version
        # 结束只读事务：名称解析完全在内存中进行，期间不持有数据库事务，
        # 其他批次的写入（补传/发布）不会被本批次的解析拖住。
        db.rollback()

        # ---- 内存解析（批次锁保护下，观测快照不会被同批补传改变）----
        index = build_name_index(taxon_rows)
        resolutions = resolve_all(
            [o.raw_species_name for o in observations], index)
        problems = [r for r in resolutions if r.issue_type is not None]

        # ---- 写阶段（第二个短事务，原子提交）----
        # 锁内重新取出批次行，拿到最新状态后再改写
        batch = db.get(SurveyBatch, batch_id)
        if batch is None:
            raise PublishError(f"批次 {batch_id} 不存在")

        # 每次发布都重置问题清单，反映最近一次尝试（直接查表，避免关系缓存陈旧）
        for old_issue in db.scalars(select(PublishIssue).where(
                PublishIssue.batch_id == batch_id)).all():
            db.delete(old_issue)

        if problems:
            for res in problems:
                db.add(PublishIssue(
                    batch_id=batch_id,
                    issue_type=res.issue_type,
                    raw_species_name=res.raw_name,
                    detail=res.detail,
                    candidates_json=json.dumps(res.candidates, ensure_ascii=False),
                ))
            batch.status = BatchStatus.NEEDS_REVIEW
            batch.last_publish_error = (
                f"{len(problems)} 条记录无法解析（unknown/ambiguous/cycle），整批待核；"
                "旧结果（如有）继续可用"
            )
            db.commit()
            return PublishOutcome(
                success=False,
                status=batch.status,
                taxonomy_version_id=version_id,
                message=batch.last_publish_error,
                issues=[_issue_dict(p) for p in problems],
            )

        # 整批合法：替换旧结果（原子提交，失败则回滚保留上一份完整版本）
        for old_resolved in db.scalars(select(ResolvedObservation).where(
                ResolvedObservation.batch_id == batch_id)).all():
            db.delete(old_resolved)
        db.flush()

        for obs, res in zip(observations, resolutions):
            db.add(ResolvedObservation(
                batch_id=batch_id,
                taxonomy_version_id=version_id,
                plot_code=obs.plot_code,
                accepted_name=res.accepted_name,
                raw_species_name=obs.raw_species_name,
                observed_on=obs.observed_on,
                quantity=obs.quantity,
            ))

        batch.status = BatchStatus.PUBLISHED
        batch.published_taxonomy_version_id = version_id
        batch.published_at = datetime.now(timezone.utc)
        batch.last_publish_error = None
        db.commit()

        return PublishOutcome(
            success=True,
            status=batch.status,
            taxonomy_version_id=version_id,
            message=f"发布成功：{len(observations)} 条记录已按分类表 v{version_no} 解析",
            issues=[],
        )


def _issue_dict(res) -> dict:
    return {
        "issue_type": res.issue_type,
        "raw_species_name": res.raw_name,
        "detail": res.detail,
        "candidates": res.candidates,
    }


# ---------------- 观测补传 ----------------

def add_observations(
    db: Session, batch_id: int, items: list[dict]
) -> UploadOutcome:
    """向批次补传观测记录（JSON/CSV 共用）。

    与发布共用批次锁：锁内重新读取批次并复查状态后再插入提交。
    - 若发布已先完成（状态变为 PUBLISHED/STALE），抛 UploadConflictError，
      一条记录都不会插入；
    - 若补传先提交，随后的发布会读到这些新记录并纳入解析与汇总。
    """
    with batch_lock(batch_id):
        batch = db.get(SurveyBatch, batch_id)
        if batch is None:
            raise BatchNotFoundError(f"批次 {batch_id} 不存在")
        if batch.status not in UPLOADABLE_STATUSES:
            raise UploadConflictError(
                f"批次状态为 {batch.status.value}，不允许再上传观测记录；"
                "如需修订请基于新批次重新开始"
            )
        inserted, duplicates = _insert_observations(db, batch_id, items)
        db.commit()
    return UploadOutcome(
        received=len(items), inserted=inserted, duplicates_skipped=duplicates
    )


def _insert_observations(
    db: Session, batch_id: int, items: list[dict]
) -> tuple[int, int]:
    """去重后插入观测（不提交，由调用方统一提交），返回 (插入数, 跳过数)。"""
    # 预查已有记录，避免依赖 IntegrityError 回滚（回滚会丢掉本请求已插入的数据）
    existing = {
        (r.plot_code, r.observed_on, r.raw_species_name)
        for r in db.scalars(select(Observation).where(
            Observation.batch_id == batch_id))
    }
    seen_in_request: set[tuple[str, str, str]] = set()
    inserted = duplicates = 0
    for item in items:
        key = (
            item["plot_code"].strip(),
            item["observed_on"].isoformat()
            if hasattr(item["observed_on"], "isoformat")
            else str(item["observed_on"]),
            item["raw_species_name"].strip(),
        )
        if key in existing or key in seen_in_request:
            duplicates += 1
            continue
        seen_in_request.add(key)
        db.add(Observation(
            batch_id=batch_id,
            plot_code=key[0],
            observed_on=key[1],
            raw_species_name=key[2],
            quantity=item["quantity"],
        ))
        inserted += 1
    return inserted, duplicates


# ---------------- 结果汇总 ----------------

def aggregate_resolved(records: list[ResolvedObservation]) -> list[dict]:
    """按样地 + 接受种汇总，保留原名集合与日期集合。"""
    groups: dict[tuple[str, str], dict] = {}
    for r in records:
        key = (r.plot_code, r.accepted_name)
        g = groups.get(key)
        if g is None:
            g = {
                "plot_code": r.plot_code,
                "accepted_name": r.accepted_name,
                "total_quantity": 0,
                "record_count": 0,
                "_raw": set(),
                "_dates": set(),
            }
            groups[key] = g
        g["total_quantity"] += r.quantity
        g["record_count"] += 1
        g["_raw"].add(r.raw_species_name)
        g["_dates"].add(r.observed_on)

    out = []
    for g in groups.values():
        out.append({
            "plot_code": g["plot_code"],
            "accepted_name": g["accepted_name"],
            "total_quantity": g["total_quantity"],
            "record_count": g["record_count"],
            "raw_names": sorted(g["_raw"]),
            "observed_dates": sorted(g["_dates"]),
        })
    out.sort(key=lambda x: (x["plot_code"], x["accepted_name"]))
    return out
