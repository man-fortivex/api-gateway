from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLogEntry


async def record_audit(
    db: AsyncSession,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    detail: dict | None = None,
) -> None:
    """Insert an audit log row. Caller is responsible for committing
    (usually as part of the same transaction as the action being logged,
    so the audit entry and the action succeed or fail together)."""
    entry = AuditLogEntry(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail or {},
    )
    db.add(entry)
