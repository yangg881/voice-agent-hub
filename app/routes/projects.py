import os
import uuid
import json
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Body, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db, Project, ProjectTimeline, RecordingProjectLink, Recording, MeetingInsight

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreatePayload(BaseModel):
    name: str
    description: Optional[str] = ""
    color: Optional[str] = "indigo"
    ai_context: Optional[str] = ""
    parent_id: Optional[str] = None


class ProjectUpdatePayload(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    ai_context: Optional[str] = None
    parent_id: Optional[str] = None


class StatusUpdatePayload(BaseModel):
    status: str  # active, on_hold, reactivated, completed, terminated
    pause_reason: Optional[str] = None


class MergePayload(BaseModel):
    source_project_id: str


class SplitPayload(BaseModel):
    new_name: str
    description: Optional[str] = ""
    ai_context: Optional[str] = ""
    timeline_ids: Optional[List[int]] = []


class AcceptCandidatePayload(BaseModel):
    name: str
    description: Optional[str] = ""
    recording_id: Optional[str] = None
    color: Optional[str] = "emerald"
    initial_summary: Optional[str] = ""
    initial_tasks: Optional[List[str]] = []


class TimelineCreatePayload(BaseModel):
    event_title: str
    event_detail: str
    event_type: Optional[str] = "update"
    audio_timestamp_ms: Optional[int] = 0
    recording_id: Optional[str] = None
    source_title: Optional[str] = ""


class ImportRecordingPayload(BaseModel):
    recording_id: str
    reanalyze: Optional[bool] = True
    relevance_notes: Optional[str] = None


@router.get("")
def list_projects(status: Optional[str] = None, db: Session = Depends(get_db)):
    from sqlalchemy.orm import selectinload
    query = db.query(Project).options(
        selectinload(Project.timelines),
        selectinload(Project.recording_links),
        selectinload(Project.sub_projects),
    )
    if status and status != "all":
        query = query.filter(Project.status == status)

    projects = query.order_by(Project.created_at.desc()).all()
    results = []
    for p in projects:
        timeline_count = len(p.timelines)
        recording_count = len(p.recording_links)
        sub_count = len(p.sub_projects)
        results.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "status": p.status,
            "color": p.color,
            "ai_context": p.ai_context,
            "current_summary": p.current_summary,
            "pause_reason": p.pause_reason,
            "parent_id": p.parent_id,
            "timeline_count": timeline_count,
            "recording_count": recording_count,
            "sub_project_count": sub_count,
            "created_at": p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "",
            "updated_at": p.updated_at.strftime("%Y-%m-%d %H:%M") if p.updated_at else "",
        })
    return {"total": len(results), "items": results}


@router.post("")
def create_project(payload: ProjectCreatePayload, db: Session = Depends(get_db)):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="项目名称不能为空")

    proj_id = str(uuid.uuid4())
    proj = Project(
        id=proj_id,
        name=payload.name.strip(),
        description=payload.description or "",
        color=payload.color or "indigo",
        ai_context=payload.ai_context or "",
        parent_id=payload.parent_id or None,
        status="active",
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return {"id": proj.id, "name": proj.name, "message": "项目创建成功"}


@router.post("/accept_candidate")
def accept_candidate(payload: AcceptCandidatePayload, db: Session = Depends(get_db)):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="项目名称不能为空")

    proj_id = str(uuid.uuid4())
    proj = Project(
        id=proj_id,
        name=payload.name.strip(),
        description=payload.description or payload.initial_summary or "",
        color=payload.color or "emerald",
        current_summary=payload.initial_summary or "",
        status="active",
    )
    db.add(proj)
    db.flush()

    rec_title = ""
    if payload.recording_id:
        rec = db.query(Recording).filter(Recording.id == payload.recording_id).first()
        if rec:
            rec_title = rec.title
            link = RecordingProjectLink(
                recording_id=rec.id,
                project_id=proj.id,
                relevance_notes="立项源头对话"
            )
            db.add(link)

    # Initial milestone
    task_notes = f"\n识别到的初始待办：{', '.join(payload.initial_tasks)}" if payload.initial_tasks else ""
    db.add(ProjectTimeline(
        project_id=proj.id,
        recording_id=payload.recording_id,
        event_title="💡 项目正式立项建档",
        event_detail=f"基于语音对话《{rec_title or '智能立项建议'}》中提炼的核心目标与范围，正式建立独立项目档案。{task_notes}",
        event_type="milestone",
        audio_timestamp_ms=0,
        source_title=rec_title
    ))

    db.commit()
    db.refresh(proj)
    return {"id": proj.id, "name": proj.name, "message": "新项目立项成功！"}


@router.post("/{project_id}/timelines")
def add_project_timeline(project_id: str, payload: TimelineCreatePayload, db: Session = Depends(get_db)):
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(status_code=404, detail="未找到该项目")

    tl = ProjectTimeline(
        project_id=proj.id,
        recording_id=payload.recording_id,
        event_title=payload.event_title,
        event_detail=payload.event_detail,
        event_type=payload.event_type or "update",
        audio_timestamp_ms=payload.audio_timestamp_ms or 0,
        source_title=payload.source_title or "",
    )
    db.add(tl)
    db.commit()
    db.refresh(tl)
    return {"id": tl.id, "message": "动态已添加"}


@router.get("/{project_id}")
def get_project_detail(project_id: str, db: Session = Depends(get_db)):
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(status_code=404, detail="未找到该项目")

    # Aggregated timelines
    timelines = [
        {
            "id": t.id,
            "event_title": t.event_title,
            "event_detail": t.event_detail,
            "event_type": t.event_type,
            "recording_id": t.recording_id,
            "source_title": t.source_title,
            "audio_timestamp_ms": t.audio_timestamp_ms,
            "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
        }
        for t in proj.timelines
    ]

    # Aggregated action items from all linked recordings
    aggregated_action_items = []
    for link in proj.recording_links:
        r = link.recording
        if r and r.insight and r.insight.action_items_json:
            try:
                items = json.loads(r.insight.action_items_json)
                for item in items:
                    item["source_recording_id"] = r.id
                    item["source_recording_title"] = r.title
                    aggregated_action_items.append(item)
            except Exception:
                pass

    # Linked recordings summary
    recordings = [
        {
            "id": link.recording.id,
            "title": link.recording.title,
            "duration_seconds": link.recording.duration_seconds,
            "status": link.recording.status,
            "created_at": link.recording.created_at.strftime("%Y-%m-%d %H:%M") if link.recording.created_at else "",
        }
        for link in proj.recording_links if link.recording
    ]

    # Sub projects
    subs = [
        {"id": sp.id, "name": sp.name, "status": sp.status, "color": sp.color}
        for sp in proj.sub_projects
    ]

    return {
        "id": proj.id,
        "name": proj.name,
        "description": proj.description,
        "status": proj.status,
        "color": proj.color,
        "ai_context": proj.ai_context,
        "current_summary": proj.current_summary,
        "pause_reason": proj.pause_reason,
        "parent_id": proj.parent_id,
        "parent_name": proj.parent.name if proj.parent else None,
        "sub_projects": subs,
        "timelines": timelines,
        "action_items": aggregated_action_items,
        "recordings": recordings,
        "created_at": proj.created_at.strftime("%Y-%m-%d %H:%M") if proj.created_at else "",
    }


@router.put("/{project_id}")
def update_project(project_id: str, payload: ProjectUpdatePayload, db: Session = Depends(get_db)):
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(status_code=404, detail="未找到该项目")

    if payload.name is not None and payload.name.strip():
        proj.name = payload.name.strip()
    if payload.description is not None:
        proj.description = payload.description
    if payload.color is not None:
        proj.color = payload.color
    if payload.ai_context is not None:
        proj.ai_context = payload.ai_context
    if payload.parent_id is not None:
        proj.parent_id = payload.parent_id if payload.parent_id != "" else None

    db.commit()
    return {"message": "项目信息更新成功"}


@router.patch("/{project_id}/status")
def update_project_status(project_id: str, payload: StatusUpdatePayload, db: Session = Depends(get_db)):
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(status_code=404, detail="未找到该项目")

    valid_statuses = ["active", "on_hold", "reactivated", "completed", "terminated"]
    if payload.status not in valid_statuses:
        raise HTTPException(status_code=400, detail="无效的项目状态")

    old_status = proj.status
    proj.status = payload.status

    # Record state transition in timeline
    status_names = {
        "active": "进行中",
        "on_hold": "搁置暂停",
        "reactivated": "重启激活",
        "completed": "已完结",
        "terminated": "已终止",
    }
    event_detail = f"状态变更：从【{status_names.get(old_status, old_status)}】变更为【{status_names.get(payload.status, payload.status)}】。"
    if payload.pause_reason:
        proj.pause_reason = payload.pause_reason
        event_detail += f" 原因说明：{payload.pause_reason}"

    timeline = ProjectTimeline(
        project_id=proj.id,
        event_title=f"项目状态更新为：{status_names.get(payload.status, payload.status)}",
        event_detail=event_detail,
        event_type="status_change",
    )
    db.add(timeline)
    db.commit()

    return {"message": f"项目状态已更新为 {status_names.get(payload.status)}"}


@router.post("/{project_id}/merge")
def merge_project(project_id: str, payload: MergePayload, db: Session = Depends(get_db)):
    """Merges source project into target project"""
    target = db.query(Project).filter(Project.id == project_id).first()
    source = db.query(Project).filter(Project.id == payload.source_project_id).first()
    if not target or not source:
        raise HTTPException(status_code=404, detail="未找到目标或来源项目")

    if target.id == source.id:
        raise HTTPException(status_code=400, detail="不能合并同一个项目")

    # Move timelines
    for t in source.timelines:
        t.project_id = target.id
        t.event_detail = f"[原属于 {source.name}] " + t.event_detail

    # Move recording links (avoid duplicates)
    target_rec_ids = {link.recording_id for link in target.recording_links}
    for link in source.recording_links:
        if link.recording_id not in target_rec_ids:
            link.project_id = target.id
        else:
            db.delete(link)

    # Fuse AI context
    if source.ai_context and source.ai_context not in target.ai_context:
        target.ai_context = (target.ai_context + "\n" + f"【合并自 {source.name} 的背景】：\n" + source.ai_context).strip()

    # Add merge event to target timeline
    db.add(ProjectTimeline(
        project_id=target.id,
        event_title=f"合并项目：吸收【{source.name}】",
        event_detail=f"成功将项目【{source.name}】的所有历史动态、待办与录音数据完整合并入本项目。",
        event_type="milestone",
    ))

    # Mark source project as merged / terminated
    source.status = "terminated"
    source.description = (source.description or "") + f" (已于 {datetime.now().strftime('%Y-%m-%d')} 合并入【{target.name}】)"

    db.commit()
    return {"message": f"成功将【{source.name}】合并入【{target.name}】"}


@router.post("/{project_id}/split")
def split_sub_project(project_id: str, payload: SplitPayload, db: Session = Depends(get_db)):
    """Derives a new sub-project from parent project with context inheritance"""
    parent = db.query(Project).filter(Project.id == project_id).first()
    if not parent:
        raise HTTPException(status_code=404, detail="未找到母体项目")

    new_id = str(uuid.uuid4())
    inherited_context = f"【衍生自母项目 {parent.name}】\n{parent.ai_context}\n" + (payload.ai_context or "")
    
    child = Project(
        id=new_id,
        name=payload.new_name.strip(),
        description=payload.description or f"衍生自【{parent.name}】的独立子项目",
        parent_id=parent.id,
        status="active",
        color="emerald",
        ai_context=inherited_context.strip(),
    )
    db.add(child)
    db.flush()

    # Move selected timelines if provided
    if payload.timeline_ids:
        for tid in payload.timeline_ids:
            t = db.query(ProjectTimeline).filter(ProjectTimeline.id == tid).first()
            if t and t.project_id == parent.id:
                t.project_id = child.id

    # Record split event in parent and child
    db.add(ProjectTimeline(
        project_id=parent.id,
        event_title=f"业务拆分：衍生出独立子项目【{child.name}】",
        event_detail=f"将相关业务独立划归至新子项目【{child.name}】跟进。",
        event_type="milestone",
    ))

    db.add(ProjectTimeline(
        project_id=child.id,
        event_title=f"子项目立项：衍生自【{parent.name}】",
        event_detail=f"承接母体项目【{parent.name}】关于该业务板块的前置决策，开启独立追踪。",
        event_type="milestone",
    ))

    db.commit()
    return {"id": child.id, "name": child.name, "message": f"成功从【{parent.name}】衍生出新项目【{child.name}】"}


@router.delete("/{project_id}/timelines/{timeline_id}")
def delete_project_timeline(project_id: str, timeline_id: int, db: Session = Depends(get_db)):
    tl = db.query(ProjectTimeline).filter(
        ProjectTimeline.id == timeline_id,
        ProjectTimeline.project_id == project_id
    ).first()
    if not tl:
        raise HTTPException(status_code=404, detail="未找到该动态记录")
    db.delete(tl)
    db.commit()
    return {"message": "动态已删除"}


@router.post("/{project_id}/import_recording")
def import_recording_to_project(
    project_id: str,
    payload: ImportRecordingPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    将指定的录音（已有微信对讲或外部录音）导入到项目中：
    1. 建立 RecordingProjectLink 关联
    2. 设置 recording.primary_project_id
    3. 在项目时间线生成导入动态
    4. 可选：后台触发 DeepSeek 结合当前项目业务背景重新提炼大事记与待办
    """
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(status_code=404, detail="未找到目标项目")

    rec = db.query(Recording).filter(Recording.id == payload.recording_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="未找到指定的录音记录")

    # 1. Ensure RecordingProjectLink exists
    link = db.query(RecordingProjectLink).filter(
        RecordingProjectLink.project_id == proj.id,
        RecordingProjectLink.recording_id == rec.id
    ).first()
    if not link:
        link = RecordingProjectLink(
            project_id=proj.id,
            recording_id=rec.id,
            relevance_notes=payload.relevance_notes or "手动导入到项目"
        )
        db.add(link)

    # 2. Update primary_project_id
    rec.primary_project_id = proj.id

    # 3. Add Timeline event if none exists yet for this recording in this project
    tl = db.query(ProjectTimeline).filter(
        ProjectTimeline.project_id == proj.id,
        ProjectTimeline.recording_id == rec.id
    ).first()
    if not tl:
        event_summary = ""
        if rec.insight and rec.insight.executive_summary:
            event_summary = rec.insight.executive_summary[:250]
        else:
            event_summary = f"成功将语音《{rec.title}》导入项目【{proj.name}】进行统一管理与追踪。"

        tl = ProjectTimeline(
            project_id=proj.id,
            recording_id=rec.id,
            event_title=f"📥 导入语音对话：《{rec.title}》",
            event_detail=event_summary,
            event_type="update",
            audio_timestamp_ms=0,
            source_title=rec.title,
        )
        db.add(tl)

    db.commit()

    # 4. Trigger AI reanalysis with project background if requested
    if payload.reanalyze:
        from app.services.pipeline import run_session_finalization, run_pipeline
        rec.status = "analyzing"
        rec.progress = 60
        rec.status_message = f"正在结合项目【{proj.name}】业务背景提炼专属纪要与待办..."
        db.commit()
        if rec.source_type == "wechat":
            background_tasks.add_task(run_session_finalization, rec.id)
        else:
            background_tasks.add_task(run_pipeline, rec.id)

    return {
        "status": "ok",
        "message": f"成功将语音《{rec.title}》导入项目【{proj.name}】！",
        "project_id": proj.id,
        "recording_id": rec.id,
        "reanalyzing": payload.reanalyze,
    }


@router.delete("/{project_id}/recordings/{recording_id}")
def unlink_recording_from_project(project_id: str, recording_id: str, db: Session = Depends(get_db)):
    """解除录音与项目的关联，并联动清理该项目下的相关时间轴动态与待办，刷新项目摘要"""
    link = db.query(RecordingProjectLink).filter(
        RecordingProjectLink.project_id == project_id,
        RecordingProjectLink.recording_id == recording_id
    ).first()
    if link:
        db.delete(link)

    rec = db.query(Recording).filter(Recording.id == recording_id).first()
    if rec and rec.primary_project_id == project_id:
        other_link = db.query(RecordingProjectLink).filter(
            RecordingProjectLink.recording_id == recording_id,
            RecordingProjectLink.project_id != project_id
        ).first()
        rec.primary_project_id = other_link.project_id if other_link else None

    # Cascading delete timeline events for this recording in this project
    db.query(ProjectTimeline).filter(
        ProjectTimeline.project_id == project_id,
        ProjectTimeline.recording_id == recording_id
    ).delete(synchronize_session=False)

    # Cascading refresh project executive summary
    proj = db.query(Project).filter(Project.id == project_id).first()
    if proj:
        from app.routes.recordings import refresh_project_summary
        refresh_project_summary(proj, db)

    db.commit()
    return {"status": "ok", "message": "已成功将该录音从本项目中移出，相关待办与动态已同步联动清理！"}


@router.delete("/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db)):
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        raise HTTPException(status_code=404, detail="未找到该项目")

    # 1. Clear primary_project_id in recordings that pointed to this project
    db.query(Recording).filter(Recording.primary_project_id == project_id).update({Recording.primary_project_id: None})

    # 2. Reset parent_id for any sub-projects
    db.query(Project).filter(Project.parent_id == project_id).update({Project.parent_id: None})

    # 3. Clean up timeline events and links explicitly
    db.query(ProjectTimeline).filter(ProjectTimeline.project_id == project_id).delete(synchronize_session=False)
    db.query(RecordingProjectLink).filter(RecordingProjectLink.project_id == project_id).delete(synchronize_session=False)

    # 4. Delete the project
    db.delete(proj)
    db.commit()
    return {"message": "项目档案已彻底删除"}
