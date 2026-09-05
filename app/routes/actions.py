import hashlib
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload
from app.database import get_db, Recording, MeetingInsight, RecordingProjectLink

router = APIRouter(prefix="/api/actions", tags=["actions"])


def make_action_id(recording_id: str, task: str) -> str:
    """Stable, deterministic id for an action item derived from its content."""
    raw = f"{recording_id}::{(task or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


@router.get("/global")
def list_global_actions(status: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Aggregates action items from every recording's meeting insight in the SQL layer.
    Fixes the bug where the global action pool was always empty because the
    recordings list endpoint never serialized action_items.
    """
    recordings = (
        db.query(Recording)
        .options(
            selectinload(Recording.insight),
            selectinload(Recording.project_links).selectinload(RecordingProjectLink.project),
            selectinload(Recording.primary_project),
        )
        .order_by(Recording.created_at.desc())
        .all()
    )

    items = []
    for r in recordings:
        if not r.insight or not r.insight.action_items_json:
            continue
        try:
            action_items = json.loads(r.insight.action_items_json)
        except Exception:
            continue
        project_names = [l.project.name for l in r.project_links if l.project]
        if not project_names and r.primary_project:
            project_names = [r.primary_project.name]
        for it in action_items:
            if not isinstance(it, dict):
                continue
            task = (it.get("task") or "").strip()
            if not task:
                continue
            items.append({
                "id": make_action_id(r.id, task),
                "task": task,
                "owner": it.get("owner") or "",
                "due_date": it.get("due_date") or "",
                "status": it.get("status") or "pending",
                "source_recording_id": r.id,
                "source_recording_title": r.title,
                "source_project_name": project_names[0] if project_names else None,
                "project_names": project_names,
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
            })

    if status == "pending":
        items = [i for i in items if i["status"] != "completed"]
    elif status == "completed":
        items = [i for i in items if i["status"] == "completed"]

    pending_count = sum(1 for i in items if i["status"] != "completed")
    return {"total": len(items), "pending": pending_count, "items": items}


@router.patch("/{action_id}/toggle")
def toggle_action(action_id: str, db: Session = Depends(get_db)):
    """
    Toggles an action item's status by its stable id (sha1 of recording_id + task).
    Replaces the fragile match-by-task-text approach.
    """
    insights = db.query(MeetingInsight).all()
    for insight in insights:
        if not insight.action_items_json:
            continue
        try:
            items = json.loads(insight.action_items_json)
        except Exception:
            continue
        changed = False
        new_status = None
        for it in items:
            if not isinstance(it, dict):
                continue
            task = (it.get("task") or "").strip()
            if make_action_id(insight.recording_id, task) == action_id:
                new_status = "pending" if it.get("status") == "completed" else "completed"
                it["status"] = new_status
                changed = True
                break
        if changed:
            insight.action_items_json = json.dumps(items, ensure_ascii=False)
            db.commit()
            return {"id": action_id, "status": new_status, "message": "待办状态已更新"}

    raise HTTPException(status_code=404, detail="未找到该待办事项")
