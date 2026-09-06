import os
import json
import logging
import uuid
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from app.database import (
    get_db, Project, ProjectTimeline, Recording,
    RecordingProjectLink, MeetingInsight, AsrSegment, PolishedSegment,
)
from app.config import settings
from app.services.project_overview_service import refresh_project_overview

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ============================================================
# 标签（原"项目"）管理：只保留最核心的 CRUD
# 复杂的合并/拆分/状态流转/手动时间轴/AI立项建议已移除
# 录音与标签的关联通过 /api/recordings/{id}/assign_project 完成
# ============================================================


@router.get("")
def list_projects(status: Optional[str] = None, db: Session = Depends(get_db)):
    """标签列表，附带关联录音数和待办统计。"""
    query = db.query(Project)
    if status and status != "all":
        query = query.filter(Project.status == status)
    total = query.count()
    projects = query.order_by(Project.updated_at.desc()).all()

    results = []
    for p in projects:
        linked_rec_ids = [
            link.recording_id
            for link in db.query(RecordingProjectLink).filter(
                RecordingProjectLink.project_id == p.id
            ).all()
        ]
        rec_query = db.query(Recording).filter(
            (Recording.primary_project_id == p.id)
            | (Recording.id.in_(
                db.query(RecordingProjectLink.recording_id).filter(
                    RecordingProjectLink.project_id == p.id
                )
            ))
        )
        recording_count = rec_query.count()
        latest_rec = rec_query.order_by(Recording.created_at.desc()).first()

        # 待办统计
        pending_actions = 0
        completed_actions = 0
        recs = rec_query.all()
        for r in recs:
            if r.insight and r.insight.action_items_json:
                try:
                    items = json.loads(r.insight.action_items_json)
                    for it in items:
                        if isinstance(it, dict):
                            if it.get("status") == "completed":
                                completed_actions += 1
                            else:
                                pending_actions += 1
                except Exception:
                    pass

        results.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "color": p.color,
            "status": p.status,
            "ai_context": p.ai_context,
            "current_summary": p.current_summary,
            "recording_count": recording_count,
            "latest_recording_at": latest_rec.created_at.strftime("%Y-%m-%d %H:%M:%S") if latest_rec and latest_rec.created_at else None,
            "action_item_count": pending_actions + completed_actions,
            "pending_action_count": pending_actions,
            "completed_action_count": completed_actions,
            "created_at": p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else "",
            "updated_at": p.updated_at.strftime("%Y-%m-%d %H:%M:%S") if p.updated_at else "",
        })

    return {"total": total, "items": results}


# 用 Body 参数接收，避免 pydantic 模型依赖
@router.post("")
def create_project(
    name: str = Body(...),
    description: Optional[str] = Body(""),
    color: Optional[str] = Body("#6366f1"),
    ai_context: Optional[str] = Body(""),
    parent_id: Optional[str] = Body(None),
    db: Session = Depends(get_db),
):
    """创建新标签。"""
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="标签名称不能为空")

    project_id = str(uuid.uuid4())
    project = Project(
        id=project_id,
        name=name.strip(),
        description=description or "",
        color=color or "#6366f1",
        ai_context=ai_context or "",
        status="active",
        parent_id=parent_id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    return {
        "id": project.id,
        "name": project.name,
        "color": project.color,
        "message": "标签创建成功",
    }


@router.put("/{project_id}")
def update_project(
    project_id: str,
    name: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
    color: Optional[str] = Body(None),
    ai_context: Optional[str] = Body(None),
    db: Session = Depends(get_db),
):
    """更新标签名称、颜色、描述或背景上下文。"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="未找到该标签")

    if name is not None and name.strip():
        project.name = name.strip()
    if description is not None:
        project.description = description
    if color is not None:
        project.color = color
    if ai_context is not None:
        project.ai_context = ai_context

    db.commit()
    db.refresh(project)

    return {
        "id": project.id,
        "name": project.name,
        "color": project.color,
        "message": "标签更新成功",
    }


@router.delete("/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db)):
    """
    删除标签。只删除标签本身和关联关系，不删除关联的录音数据。
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="未找到该标签")

    project_name = project.name

    # 1. 清除录音的 primary_project_id 指向此标签
    db.query(Recording).filter(Recording.primary_project_id == project_id).update(
        {Recording.primary_project_id: None}, synchronize_session=False
    )

    # 2. 删除关联链接
    db.query(RecordingProjectLink).filter(
        RecordingProjectLink.project_id == project_id
    ).delete(synchronize_session=False)

    # 3. 删除该标签的时间轴事件
    db.query(ProjectTimeline).filter(
        ProjectTimeline.project_id == project_id
    ).delete(synchronize_session=False)

    # 4. 删除标签本身
    db.delete(project)
    db.commit()

    return {"message": f"标签「{project_name}」已删除，关联录音保留"}


# ============================================================
# 标签进度看板（AI 自动汇总，随新录音实时更新）
# ============================================================


@router.get("/{project_id}/overview")
def get_project_overview(project_id: str, db: Session = Depends(get_db)):
    """
    标签整体进度看板：
    - AI 综述（overall / progress / pending / risks，来自 current_summary）
    - 关联录音列表（按时间倒序）
    - 时间线大事记（来自 project_timelines）
    - 待办聚合统计
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="未找到该标签")

    # 关联录音（主归属 + 链接），按时间倒序
    recs = db.query(Recording).filter(
        (Recording.primary_project_id == project_id)
        | (Recording.id.in_(
            db.query(RecordingProjectLink.recording_id).filter(
                RecordingProjectLink.project_id == project_id
            )
        ))
    ).order_by(Recording.created_at.desc()).all()

    recordings = []
    pending_actions = 0
    completed_actions = 0
    for r in recs:
        if r.insight and r.insight.action_items_json:
            try:
                for it in json.loads(r.insight.action_items_json):
                    if isinstance(it, dict):
                        if it.get("status") == "completed":
                            completed_actions += 1
                        else:
                            pending_actions += 1
            except Exception:
                pass
        recordings.append({
            "id": r.id,
            "title": r.title,
            "source_type": r.source_type,
            "status": r.status,
            "duration_seconds": r.duration_seconds or 0,
            "summary": r.insight.executive_summary if r.insight else "",
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        })

    # 时间线大事记（倒序，最多 50 条）
    events = db.query(ProjectTimeline).filter(
        ProjectTimeline.project_id == project_id
    ).order_by(ProjectTimeline.created_at.desc()).limit(50).all()
    timeline = [
        {
            "id": e.id,
            "event_title": e.event_title,
            "event_detail": e.event_detail,
            "event_type": e.event_type,
            "source_title": e.source_title or "",
            "recording_id": e.recording_id or "",
            "created_at": e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else "",
        }
        for e in events
    ]

    # AI 综述：新格式是 JSON，老数据可能是纯文本，做兼容
    overview = None
    if project.current_summary:
        try:
            parsed = json.loads(project.current_summary)
            if isinstance(parsed, dict) and parsed.get("overall"):
                overview = parsed
        except Exception:
            pass
        if overview is None:
            overview = {"overall": project.current_summary, "progress": [], "pending": [], "risks": []}

    return {
        "id": project.id,
        "name": project.name,
        "color": project.color,
        "description": project.description or "",
        "overview": overview,
        "recording_count": len(recordings),
        "pending_action_count": pending_actions,
        "completed_action_count": completed_actions,
        "recordings": recordings,
        "timeline": timeline,
        "updated_at": project.updated_at.strftime("%Y-%m-%d %H:%M:%S") if project.updated_at else "",
    }


@router.post("/{project_id}/refresh_overview")
async def refresh_overview(project_id: str, db: Session = Depends(get_db)):
    """手动触发一次 AI 看板重新汇总（前端"刷新"按钮用）。"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="未找到该标签")
    ok = await refresh_project_overview(project_id)
    if not ok:
        raise HTTPException(status_code=500, detail="AI 汇总失败，请检查大模型配置后重试")
    return {"message": "看板已更新"}
