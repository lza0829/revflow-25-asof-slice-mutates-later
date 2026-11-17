from fastapi import APIRouter, Query

from app.modules import asof_balance as svc

router = APIRouter(tags=["as-of 切片"])


@router.get("/asof-slices")
def get_slices():
    return svc.list_slices()


@router.post("/asof-slices")
def post_slice(
    cutoff_at: str | None = Query(None, description="截止入账时刻 T，如 2026-11-16T18:00:00Z"),
):
    return svc.generate_slice(cutoff_at)


@router.get("/asof-slices/{slice_id}")
def get_slice(slice_id: int):
    return svc.get_slice(slice_id)
