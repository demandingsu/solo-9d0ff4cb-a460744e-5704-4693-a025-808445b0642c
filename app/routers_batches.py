"""调查批次与观测记录接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .csv_io import CSVParseError, parse_observation_csv
from .database import get_db
from .models import Observation, SurveyBatch
from .schemas import (
    BatchCreateIn,
    BatchOut,
    ObservationBulkIn,
    ObservationIn,
    ObservationOut,
    UploadResultOut,
)
from .services import (
    BatchNotFoundError,
    UploadConflictError,
    add_observations,
)

router = APIRouter(tags=["批次与观测"])


@router.post("/batches", response_model=BatchOut, status_code=201,
             summary="创建调查批次")
def create_batch(payload: BatchCreateIn,
                 db: Session = Depends(get_db)) -> SurveyBatch:
    batch = SurveyBatch(name=payload.name.strip(),
                        investigator=(payload.investigator or None))
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/batches", response_model=list[BatchOut], summary="批次列表")
def list_batches(db: Session = Depends(get_db)) -> list[SurveyBatch]:
    return list(db.scalars(select(SurveyBatch).order_by(SurveyBatch.id)))


@router.get("/batches/{batch_id}", response_model=BatchOut, summary="批次详情")
def get_batch(batch_id: int, db: Session = Depends(get_db)) -> SurveyBatch:
    batch = db.get(SurveyBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    return batch


def _upload(db: Session, batch_id: int, items: list[dict]) -> UploadResultOut:
    """JSON/CSV 共用的补传入口：并发互斥与状态复查在服务层完成。"""
    try:
        outcome = add_observations(db, batch_id, items)
    except BatchNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except UploadConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return UploadResultOut(
        received=outcome.received,
        inserted=outcome.inserted,
        duplicates_skipped=outcome.duplicates_skipped,
    )


@router.post("/batches/{batch_id}/observations",
             response_model=UploadResultOut, status_code=201,
             summary="上传观测记录（JSON 批量）")
def upload_observations(batch_id: int, payload: ObservationBulkIn,
                        db: Session = Depends(get_db)) -> UploadResultOut:
    return _upload(db, batch_id, [o.model_dump() for o in payload.observations])


@router.post("/batches/{batch_id}/observations/csv",
             response_model=UploadResultOut, status_code=201,
             summary="上传观测记录（CSV 文件）")
def upload_observations_csv(batch_id: int,
                            file: UploadFile = File(...),
                            db: Session = Depends(get_db)) -> UploadResultOut:
    # 同步端点：FastAPI 会放到线程池执行，等待批次锁时不会阻塞事件循环
    file.file.seek(0)
    try:
        items = parse_observation_csv(file.file.read())
    except CSVParseError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    # 走 Pydantic 做日期与正整数校验
    try:
        validated = ObservationBulkIn(
            observations=[ObservationIn(**it) for it in items]
        )
    except Exception as exc:  # ValidationError
        raise HTTPException(422, detail=f"CSV 数据校验失败: {exc}") from exc
    return _upload(
        db, batch_id, [o.model_dump() for o in validated.observations])


@router.get("/batches/{batch_id}/observations",
            response_model=list[ObservationOut], summary="查看批次原始观测")
def list_observations(batch_id: int,
                      db: Session = Depends(get_db)) -> list[Observation]:
    batch = db.get(SurveyBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"批次 {batch_id} 不存在")
    return list(db.scalars(
        select(Observation).where(Observation.batch_id == batch_id)
        .order_by(Observation.id)))
