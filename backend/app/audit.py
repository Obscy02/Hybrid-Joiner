from __future__ import annotations

from sqlmodel import Session

from .models import AuditLog


def log_action(session: Session, actor: str, action: str, detail: str = "") -> None:
    session.add(AuditLog(actor=actor, action=action, detail=detail))
    session.commit()
