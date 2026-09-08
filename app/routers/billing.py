from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.database import get_db
from app.models import UsageLog
from app.schemas import UsageSummaryOut

router = APIRouter(prefix="/billing", tags=["billing"], dependencies=[Depends(require_admin)])


@router.get("/tenants/{tenant_id}/usage", response_model=list[UsageSummaryOut])
async def get_tenant_usage(
    tenant_id: str, db: AsyncSession = Depends(get_db), limit: int = 100
) -> list[UsageSummaryOut]:
    result = await db.execute(
        select(UsageLog)
        .where(UsageLog.tenant_id == tenant_id)
        .order_by(UsageLog.bucket_minute.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return [
        UsageSummaryOut(
            tenant_id=row.tenant_id,
            api_key_id=row.api_key_id,
            bucket_minute=row.bucket_minute,
            request_count=row.request_count,
            error_count=row.error_count,
            avg_latency_ms=(row.total_latency_ms / row.request_count) if row.request_count else 0.0,
        )
        for row in rows
    ]
