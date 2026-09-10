import os
import glob
import uuid
import json
import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, BackgroundTasks, Body
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db, Recording, AsrSegment, PolishedSegment, MeetingInsight, Project, RecordingProjectLink, ProjectTimeline
from app.config import RAW_AUDIO_DIR, PROCESSED_AUDIO_DIR, settings
from app.services.audio_service import AudioService
from app.services.pipeline import run_pipeline, run_session_finalization, get_asr_provider
from app.services.asr.volc_asr import VolcStreamAsrProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recordings", tags=["recordings"])



ALLOWED_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".webm", ".mp4", ".amr", ".silk", ".wma", ".opus"}
UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MB


@router.post("/upload")
async def upload_recording(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    source_type: Optional[str] = Form("upload"),
    project_id: Optional[str] = Form(None),
    asr_provider: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="未提供有效文件名")

    ext = os.path.splitext(file.filename)[1].lower() or ".mp3"
    if ext not in ALLOWED_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"不支持的音频格式 {ext}，请上传常见音频文件")

    recording_id = str(uuid.uuid4())
    raw_save_path = str(RAW_AUDIO_DIR / f"{recording_id}_raw{ext}")

    # Stream to disk in chunks with a hard size cap — never buffer the whole
    # file in memory (the server only has ~1GB RAM).
    file_size = 0
    try:
        with open(raw_save_path, "wb") as f:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                file_size += len(chunk)
                if file_size > settings.MAX_CONTENT_LENGTH:
                    raise HTTPException(status_code=413, detail="文件超过 500MB 大小限制")
                f.write(chunk)
    except HTTPException:
        if os.path.exists(raw_save_path):
            os.remove(raw_save_path)
        raise
    except Exception as e:
        if os.path.exists(raw_save_path):
            os.remove(raw_save_path)
        raise HTTPException(status_code=500, detail=f"文件写入失败: {e}")

    rec_title = title.strip() if title and title.strip() else os.path.splitext(file.filename)[0]
    target_proj_id = project_id if project_id and project_id != "global" and project_id.strip() else None

    recording = Recording(
        id=recording_id,
        title=rec_title,
        filename=file.filename,
        raw_file_path=raw_save_path,
        file_size=file_size,
        source_type=source_type,
        primary_project_id=target_proj_id,
        status="pending",
        progress=5,
        status_message="已上传，等待进入处理队列",
    )
    db.add(recording)
    db.commit()
    db.refresh(recording)

    # Launch background task
    background_tasks.add_task(run_pipeline, recording_id, asr_provider)

    return {
        "id": recording.id,
        "title": recording.title,
        "status": recording.status,
        "primary_project_id": recording.primary_project_id,
        "message": "音频文件上传成功，正在后台分析处理中",
    }


@router.post("/stream_chunk")
async def upload_stream_chunk(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    speaker_id: Optional[str] = Form("发言人 1"),
    project_id: Optional[str] = Form(None),
    asr_provider: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    from datetime import datetime
    now_str = datetime.now().strftime("%m-%d %H:%M")

    # 1. Locate or create Session Recording
    recording = None
    if session_id:
        recording = db.query(Recording).filter(Recording.id == session_id).first()

    if not recording:
        rec_id = session_id or str(uuid.uuid4())
        rec_title = title.strip() if title and title.strip() else f"实时语音速记 {now_str}"
        target_proj_id = project_id if project_id and project_id != "global" and project_id.strip() else None
        recording = Recording(
            id=rec_id,
            title=rec_title,
            filename=f"{rec_title}.wav",
            raw_file_path=str(RAW_AUDIO_DIR / f"{rec_id}_raw.wav"),
            file_size=0,
            source_type="wechat",
            primary_project_id=target_proj_id,
            status="recording",
            progress=20,
            status_message="正在实时语音记录中...",
        )
        db.add(recording)
        db.commit()
        db.refresh(recording)

    # 2. Count existing segments to determine sequence number
    existing_count = db.query(AsrSegment).filter(AsrSegment.recording_id == recording.id).count()
    seq_order = existing_count + 1

    # 3. Save raw chunk file
    content = await file.read()
    raw_ext = os.path.splitext(file.filename or "")[1].lower() or ".webm"
    if raw_ext == ".wav":
        raw_chunk_path = str(RAW_AUDIO_DIR / f"{recording.id}_chunk_{seq_order:03d}_raw.wav")
    else:
        raw_chunk_path = str(RAW_AUDIO_DIR / f"{recording.id}_chunk_{seq_order:03d}{raw_ext}")
    with open(raw_chunk_path, "wb") as f:
        f.write(content)


    # 4. Transcode to 16kHz mono WAV
    wav_chunk_path = str(RAW_AUDIO_DIR / f"{recording.id}_chunk_{seq_order:03d}.wav")
    duration_sec = AudioService.convert_to_wav(raw_chunk_path, wav_chunk_path)
    if duration_sec <= 0.0:
        duration_sec = 2.0

    # 5. Fast ASR transcription on chunk
    provider_name = (asr_provider or settings.DEFAULT_ASR_PROVIDER).lower()
    recognized_text = ""
    asr_error = None

    try:
        provider = get_asr_provider(provider_name)
        segments = await provider.transcribe(wav_chunk_path, recording.id)
        if segments:
            recognized_text = " ".join(s.raw_text for s in segments).strip()
    except Exception as e:
        logger.warning("ASR provider %s error: %s", provider_name, e)
        asr_error = str(e)
        if "20000003" in asr_error or "no valid speech" in asr_error.lower() or "silence" in asr_error.lower():
            recognized_text = "（静音或未检测到清晰人声）"
        elif "requested resource not granted" in asr_error:
            recognized_text = "【火山引擎403: 资源未授权，请检查控制台开通服务与Resource ID】"
        else:
            recognized_text = f"【ASR识别提示: {asr_error[:80]}】"

    if not recognized_text:
        recognized_text = "（静音或未检测到清晰人声）"


    # 6. Calculate continuous timeline
    last_seg = (
        db.query(AsrSegment)
        .filter(AsrSegment.recording_id == recording.id)
        .order_by(AsrSegment.end_ms.desc())
        .first()
    )
    start_ms = (last_seg.end_ms + 200) if last_seg else 0
    end_ms = start_ms + int(duration_sec * 1000)

    # 7. Insert AsrSegment & PolishedSegment
    seg = AsrSegment(
        recording_id=recording.id,
        speaker_id=speaker_id or "发言人 1",
        start_ms=start_ms,
        end_ms=end_ms,
        raw_text=recognized_text,
        seq_order=seq_order,
    )
    db.add(seg)

    p_seg = PolishedSegment(
        recording_id=recording.id,
        speaker_id=speaker_id or "发言人 1",
        start_ms=start_ms,
        end_ms=end_ms,
        polished_text=recognized_text,
        seq_order=seq_order,
    )
    db.add(p_seg)

    recording.duration_seconds = end_ms / 1000.0
    recording.file_size += len(content)
    recording.status_message = f"已记录 {seq_order} 条语音"
    db.commit()
    db.refresh(seg)

    return {
        "session_id": recording.id,
        "title": recording.title,
        "asr_error": asr_error,
        "segment": {
            "id": seg.id,
            "seq_order": seg.seq_order,
            "speaker_id": seg.speaker_id,
            "start_ms": seg.start_ms,
            "end_ms": seg.end_ms,
            "duration_seconds": round(duration_sec, 1),
            "text": seg.raw_text,
            "audio_url": f"/api/recordings/{recording.id}/chunks/{seq_order}",
        }
    }


@router.get("/{recording_id}/chunks/{seq_order}")
def get_chunk_audio(recording_id: str, seq_order: int):
    wav_chunk_path = str(RAW_AUDIO_DIR / f"{recording_id}_chunk_{seq_order:03d}.wav")
    if os.path.exists(wav_chunk_path):
        return FileResponse(wav_chunk_path, media_type="audio/wav")

    for ext in [".webm", ".mp4", ".ogg", ".aac", ".m4a"]:
        p = str(RAW_AUDIO_DIR / f"{recording_id}_chunk_{seq_order:03d}{ext}")
        if os.path.exists(p):
            return FileResponse(p)

    raise HTTPException(status_code=404, detail="未找到该音频切片")


class FinalizeSessionPayload(BaseModel):
    project_id: Optional[str] = None


class AssignProjectPayload(BaseModel):
    project_id: Optional[str] = None  # Single project (legacy/quick mode)
    project_ids: Optional[list[str]] = None  # Multiple projects
    reanalyze: Optional[bool] = True


@router.post("/{session_id}/finalize")
def finalize_session(
    session_id: str,
    background_tasks: BackgroundTasks,
    payload: Optional[FinalizeSessionPayload] = None,
    project_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    recording = db.query(Recording).filter(Recording.id == session_id).first()
    if not recording:
        raise HTTPException(status_code=404, detail="未找到该会话")

    # If project_id provided, bind it to this recording
    target_proj_id = None
    if payload and payload.project_id:
        target_proj_id = payload.project_id
    elif project_id:
        target_proj_id = project_id

    if target_proj_id and target_proj_id != "global":
        recording.primary_project_id = target_proj_id
        link = db.query(RecordingProjectLink).filter(
            RecordingProjectLink.project_id == target_proj_id,
            RecordingProjectLink.recording_id == recording.id
        ).first()
        if not link:
            db.add(RecordingProjectLink(
                project_id=target_proj_id,
                recording_id=recording.id,
                relevance_notes="实时语音会话结束指定归属项目"
            ))

    # 1. Collect all chunk WAV files
    chunks = (
        db.query(AsrSegment)
        .filter(AsrSegment.recording_id == session_id)
        .order_by(AsrSegment.seq_order.asc())
        .all()
    )
    chunk_paths = []
    for c in chunks:
        wav_path = str(RAW_AUDIO_DIR / f"{session_id}_chunk_{c.seq_order:03d}.wav")
        if os.path.exists(wav_path):
            chunk_paths.append(wav_path)

    # 2. Concat into master processed MP3
    output_mp3 = str(PROCESSED_AUDIO_DIR / f"{session_id}_processed.mp3")
    if chunk_paths:
        total_dur = AudioService.concat_audio_files(chunk_paths, output_mp3)
        recording.processed_file_path = output_mp3
        recording.raw_file_path = chunk_paths[0] if chunk_paths else output_mp3
        recording.duration_seconds = total_dur
    else:
        recording.status = "failed"
        recording.error_message = "没有接收到有效的音频分片"
        db.commit()
        raise HTTPException(status_code=400, detail="未收到音频数据，无法生成完整录音")

    # 3. Concatenate polished segments text
    polished_list = (
        db.query(PolishedSegment)
        .filter(PolishedSegment.recording_id == session_id)
        .order_by(PolishedSegment.seq_order.asc())
        .all()
    )
    full_transcript = " ".join(p.polished_text for p in polished_list if p.polished_text).strip()
    if not full_transcript:
        # Fallback to raw text
        full_transcript = " ".join(c.raw_text for c in chunks if c.raw_text).strip()

    # 4. Generate a smart title if default
    if full_transcript and recording.title.startswith("实时语音"):
        first_sentence = full_transcript.split("。")[0].split("，")[0][:25]
        if first_sentence:
            recording.title = f"实时语音: {first_sentence}"

    recording.status = "analyzing"
    recording.progress = 60
    recording.status_message = "正在进行项目知识流匹配与深度商业洞察..."
    db.commit()

    # 5. Launch background task for LLM deep analysis and project distribution
    background_tasks.add_task(run_session_finalization, session_id)

    return {
        "status": "ok",
        "recording_id": session_id,
        "duration_seconds": recording.duration_seconds,
        "message": "已结束对讲会话，AI 正在提炼深度纪要与待办清单",
    }


def refresh_project_summary(proj: Project, db: Session):
    """
    当项目关联的录音、动态或待办发生变动（添加或移出）时，联动更新项目的执行摘要与最新进展
    """
    remaining_links = db.query(RecordingProjectLink).filter(RecordingProjectLink.project_id == proj.id).all()
    if remaining_links:
        rec_ids = [l.recording_id for l in remaining_links]
        latest_rec = (
            db.query(Recording)
            .filter(Recording.id.in_(rec_ids), Recording.status == "completed")
            .order_by(Recording.created_at.desc())
            .first()
        )
        if latest_rec and latest_rec.insight and latest_rec.insight.executive_summary:
            proj.current_summary = latest_rec.insight.executive_summary[:300]
            return

    # If no completed recordings with executive summary, check latest timeline event
    latest_tl = (
        db.query(ProjectTimeline)
        .filter(ProjectTimeline.project_id == proj.id)
        .order_by(ProjectTimeline.created_at.desc())
        .first()
    )
    if latest_tl:
        proj.current_summary = f"【最新动态】{latest_tl.event_title}：{latest_tl.event_detail[:200]}"
    else:
        proj.current_summary = "暂无关联语音与历史动态，等待录入或导入。"


@router.post("/{recording_id}/assign_project")
def assign_recording_project(
    recording_id: str,
    payload: AssignProjectPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    将录音归属/导入到指定单个或多个项目，或重置为项目总览全局录音
    包含关联变动时的待办、时间轴动态与项目摘要联动清理与更新
    """
    recording = db.query(Recording).filter(Recording.id == recording_id).first()
    if not recording:
        raise HTTPException(status_code=404, detail="未找到该录音记录")

    # Determine target project IDs
    target_ids = []
    if payload.project_ids is not None:
        target_ids = [pid for pid in payload.project_ids if pid and pid != "global"]
    elif payload.project_id:
        if payload.project_id != "global":
            target_ids = [payload.project_id]

    # Collect previously linked project IDs
    existing_links = db.query(RecordingProjectLink).filter(RecordingProjectLink.recording_id == recording.id).all()
    old_proj_ids = set([l.project_id for l in existing_links])
    if recording.primary_project_id:
        old_proj_ids.add(recording.primary_project_id)

    if not target_ids:
        recording.primary_project_id = None
        # 1. Cascading delete all links for this recording
        db.query(RecordingProjectLink).filter(RecordingProjectLink.recording_id == recording.id).delete(synchronize_session=False)
        # 2. Cascading delete all timeline events for this recording across any project
        db.query(ProjectTimeline).filter(ProjectTimeline.recording_id == recording.id).delete(synchronize_session=False)
        # 3. Cascading refresh summary for all affected projects
        for pid in old_proj_ids:
            proj = db.query(Project).filter(Project.id == pid).first()
            if proj:
                refresh_project_summary(proj, db)

        db.commit()
        return {"status": "ok", "message": "已解除全部项目归属，转为项目总览全局录音，相关待办与动态已同步清理！"}

    # Fetch valid projects
    projects = db.query(Project).filter(Project.id.in_(target_ids)).all()
    if not projects:
        raise HTTPException(status_code=404, detail="未找到有效的目标项目")

    valid_proj_ids = set([p.id for p in projects])
    proj_names = [p.name for p in projects]

    # Determine unlinked projects
    unlinked_proj_ids = old_proj_ids - valid_proj_ids
    if unlinked_proj_ids:
        # 1. Delete links for unlinked projects
        db.query(RecordingProjectLink).filter(
            RecordingProjectLink.recording_id == recording.id,
            RecordingProjectLink.project_id.in_(unlinked_proj_ids)
        ).delete(synchronize_session=False)
        # 2. Cascading delete timeline events for unlinked projects
        db.query(ProjectTimeline).filter(
            ProjectTimeline.recording_id == recording.id,
            ProjectTimeline.project_id.in_(unlinked_proj_ids)
        ).delete(synchronize_session=False)
        # 3. Cascading refresh summary for unlinked projects
        for pid in unlinked_proj_ids:
            proj = db.query(Project).filter(Project.id == pid).first()
            if proj:
                refresh_project_summary(proj, db)

    # Add new links and timelines
    for proj in projects:
        link = db.query(RecordingProjectLink).filter(
            RecordingProjectLink.project_id == proj.id,
            RecordingProjectLink.recording_id == recording.id
        ).first()
        if not link:
            link = RecordingProjectLink(
                project_id=proj.id,
                recording_id=recording.id,
                relevance_notes="手动指定项目归属"
            )
            db.add(link)

        # Add timeline event if missing
        tl = db.query(ProjectTimeline).filter(
            ProjectTimeline.project_id == proj.id,
            ProjectTimeline.recording_id == recording.id
        ).first()
        if not tl:
            event_summary = ""
            if recording.insight and recording.insight.executive_summary:
                event_summary = recording.insight.executive_summary[:250]
            else:
                event_summary = f"成功将语音《{recording.title}》导入项目【{proj.name}】进行统一管理。"

            tl = ProjectTimeline(
                project_id=proj.id,
                recording_id=recording.id,
                event_title=f"📥 导入语音：《{recording.title}》",
                event_detail=event_summary,
                event_type="update",
                audio_timestamp_ms=0,
                source_title=recording.title,
            )
            db.add(tl)

        # If project had no summary, update it with this recording's summary
        if not proj.current_summary or proj.current_summary == "暂无关联语音与历史动态，等待录入或导入。":
            if recording.insight and recording.insight.executive_summary:
                proj.current_summary = recording.insight.executive_summary[:300]

    # Set primary project: keep current if valid, else pick first valid
    if recording.primary_project_id not in valid_proj_ids:
        recording.primary_project_id = [p.id for p in projects][0]

    db.commit()

    if payload.reanalyze:
        recording.status = "analyzing"
        recording.progress = 60
        recording.status_message = f"正在结合项目【{' / '.join(proj_names)}】背景提炼专属纪要与待办..."
        db.commit()
        if recording.source_type == "wechat":
            background_tasks.add_task(run_session_finalization, recording.id)
        else:
            background_tasks.add_task(run_pipeline, recording.id)

    msg = f"成功更新归属！已关联至【{' / '.join(proj_names)}】"
    if unlinked_proj_ids:
        msg += "，已从取消的项目中移出并同步联动清理相关待办与动态！"
    return {
        "status": "ok",
        "message": msg,
        "reanalyzing": payload.reanalyze
    }


@router.post("/{recording_id}/retranscribe")
async def retranscribe_recording(
    recording_id: str,
    background_tasks: BackgroundTasks,
    asr_provider: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    重新转写指定会话的所有音频切片，并在后台重新触发 DeepSeek 清洗、纪要提炼与项目归流。
    """
    recording = db.query(Recording).filter(Recording.id == recording_id).first()
    if not recording:
        raise HTTPException(status_code=404, detail="未找到该录音记录")

    chunks = db.query(AsrSegment).filter(AsrSegment.recording_id == recording_id).order_by(AsrSegment.seq_order.asc()).all()

    # For uploaded whole audio files or recordings with 0 segments, re-run full pipeline
    if recording.source_type == "upload" or not chunks:
        recording.status = "pending"
        recording.progress = 5
        recording.status_message = "正在重新触发全流程流水线处理录音..."
        recording.error_message = None
        db.commit()
        background_tasks.add_task(run_pipeline, recording_id, asr_provider)
        return {
            "status": "ok",
            "recording_id": recording_id,
            "retranscribed_count": 0,
            "total_chunks": len(chunks),
            "message": "已成功重新触发全流程流水线处理录音文件！",
        }

    provider = get_asr_provider(asr_provider or settings.DEFAULT_ASR_PROVIDER)

    retranscribed_count = 0
    errors = []

    for c in chunks:
        wav_path = str(RAW_AUDIO_DIR / f"{recording_id}_chunk_{c.seq_order:03d}.wav")
        if not os.path.exists(wav_path) and recording.raw_file_path and os.path.exists(recording.raw_file_path):
            wav_path = recording.raw_file_path

        if os.path.exists(wav_path):
            try:
                segs = await provider.transcribe(wav_path, recording_id)
                if segs:
                    new_text = " ".join(s.raw_text for s in segs).strip()
                    if new_text and not new_text.startswith("【"):
                        c.raw_text = new_text
                        retranscribed_count += 1
            except Exception as e:
                errors.append(f"分片 {c.seq_order}: {e}")

    db.commit()

    recording.status = "analyzing"
    recording.progress = 60
    recording.status_message = f"重转写完成 ({retranscribed_count}/{len(chunks)} 条)，正在由 DeepSeek 提炼项目纪要..."
    db.commit()

    # Launch background task for LLM polishing and deep analysis
    background_tasks.add_task(run_session_finalization, recording_id)

    return {
        "status": "ok",
        "recording_id": recording_id,
        "retranscribed_count": retranscribed_count,
        "total_chunks": len(chunks),
        "errors": [str(e) for e in errors[:3]],
        "message": f"成功重新转写 {retranscribed_count}/{len(chunks)} 条语音分片，后台正在进行项目流归集与更新！",
    }


@router.get("/search")
def search_recordings(q: str = "", limit: int = 50, db: Session = Depends(get_db)):
    """全文检索：标题 / 摘要 / 讨论主题 / 待办 / 决议 / 逐字稿"""
    from sqlalchemy import or_

    kw = (q or "").strip()
    if not kw:
        return {"query": kw, "count": 0, "items": []}

    like = "%" + kw + "%"
    low = kw.lower()
    hits = {}

    def touch(rid, kind, snippet):
        if not rid:
            return
        if rid in hits:
            if kind not in hits[rid]["match_types"]:
                hits[rid]["match_types"].append(kind)
            if snippet and not hits[rid]["snippet"]:
                hits[rid]["snippet"] = snippet
            return
        rec = db.query(Recording).filter(Recording.id == rid).first()
        if not rec:
            return
        hits[rid] = {
            "id": rid,
            "title": rec.title,
            "created_at": rec.created_at,
            "status": rec.status,
            "duration_seconds": rec.duration_seconds,
            "source_type": rec.source_type,
            "match_types": [kind],
            "snippet": snippet or "",
        }

    for rec in db.query(Recording).filter(Recording.title.ilike(like)).limit(limit).all():
        touch(rec.id, "title", "")

    for seg in db.query(PolishedSegment).filter(PolishedSegment.polished_text.ilike(like)).limit(limit * 4).all():
        text = seg.polished_text or ""
        pos = text.lower().find(low)
        if pos >= 0:
            snip = text[max(0, pos - 24): pos + 72]
        else:
            snip = text[:96]
        touch(seg.recording_id, "transcript", snip)

    rows = db.query(MeetingInsight).filter(or_(
        MeetingInsight.executive_summary.ilike(like),
        MeetingInsight.topics_json.ilike(like),
        MeetingInsight.action_items_json.ilike(like),
        MeetingInsight.decisions_json.ilike(like),
        MeetingInsight.risks_json.ilike(like),
    )).limit(limit).all()
    for ins in rows:
        touch(ins.recording_id, "insight", (ins.executive_summary or "")[:96])

    items = sorted(hits.values(), key=lambda x: str(x.get("created_at") or ""), reverse=True)[:limit]
    return {"query": kw, "count": len(items), "items": items}


class BatchIdsPayload(BaseModel):
    ids: list = []


class BatchAssignPayload(BaseModel):
    ids: list = []
    project_ids: list = []


@router.post("/batch_delete")
def batch_delete_recordings(payload: BatchIdsPayload, db: Session = Depends(get_db)):
    """批量删除录音：逐条复用单条删除的完整磁盘+数据库清理逻辑"""
    deleted = 0
    failed = []
    for rid in (payload.ids or []):
        if not rid:
            continue
        exists = db.query(Recording).filter(Recording.id == rid).first()
        if not exists:
            continue
        try:
            delete_recording(rid, db)
            deleted += 1
        except Exception as e:
            db.rollback()
            failed.append({"id": rid, "error": str(e)[:120]})
    return {"status": "ok", "deleted": deleted, "failed": failed}


@router.post("/batch_assign_project")
def batch_assign_recordings(
    payload: BatchAssignPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """批量打标签：逐条复用单条归属逻辑（project_ids 为空则清空标签）"""
    updated = 0
    failed = []
    inner = AssignProjectPayload(project_ids=list(payload.project_ids or []), reanalyze=False)
    for rid in (payload.ids or []):
        if not rid:
            continue
        exists = db.query(Recording).filter(Recording.id == rid).first()
        if not exists:
            continue
        try:
            assign_recording_project(rid, inner, background_tasks, db)
            updated += 1
        except Exception as e:
            db.rollback()
            failed.append({"id": rid, "error": str(e)[:120]})
    return {"status": "ok", "updated": updated, "failed": failed}


@router.get("")
def list_recordings(skip: int = 0, limit: int = 50, project_id: Optional[str] = None, db: Session = Depends(get_db)):
    from sqlalchemy.orm import selectinload
    query = db.query(Recording).options(
        selectinload(Recording.insight),
        selectinload(Recording.project_links).selectinload(RecordingProjectLink.project),
        selectinload(Recording.primary_project),
    )
    if project_id and project_id != "all":
        # Filter recordings linked to or primary of this project
        query = query.filter(
            (Recording.primary_project_id == project_id) |
            (Recording.id.in_(
                db.query(RecordingProjectLink.recording_id).filter(RecordingProjectLink.project_id == project_id)
            ))
        )
    total = query.count()
    records = query.order_by(Recording.created_at.desc()).offset(skip).limit(limit).all()
    results = []
    for r in records:
        summary_preview = ""
        if r.insight and r.insight.executive_summary:
            summary_preview = r.insight.executive_summary[:120] + "..." if len(r.insight.executive_summary) > 120 else r.insight.executive_summary

        linked_projects = [
            {"id": link.project.id, "name": link.project.name, "color": link.project.color}
            for link in r.project_links if link.project
        ]

        results.append({
            "id": r.id,
            "title": r.title,
            "filename": r.filename,
            "duration_seconds": round(r.duration_seconds, 1),
            "file_size": r.file_size,
            "source_type": r.source_type,
            "status": r.status,
            "progress": r.progress,
            "status_message": r.status_message,
            "error_message": r.error_message,
            "summary_preview": summary_preview,
            "primary_project_id": r.primary_project_id,
            "primary_project_name": r.primary_project.name if r.primary_project else None,
            "linked_projects": linked_projects,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        })
    return {"total": total, "items": results}


@router.get("/{recording_id}/status")
def get_recording_status(recording_id: str, db: Session = Depends(get_db)):
    """Lightweight status payload for progress polling (avoids refetching full detail)."""
    r = db.query(Recording).filter(Recording.id == recording_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="未找到该录音记录")
    return {
        "id": r.id,
        "status": r.status,
        "progress": r.progress,
        "status_message": r.status_message,
        "error_message": r.error_message,
    }


@router.get("/{recording_id}")
def get_recording_detail(recording_id: str, db: Session = Depends(get_db)):
    r = db.query(Recording).filter(Recording.id == recording_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="未找到该录音记录")

    raw_segments = [
        {
            "id": s.id,
            "speaker_id": s.speaker_id,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "raw_text": s.raw_text,
            "seq_order": s.seq_order,
        }
        for s in r.raw_segments
    ]

    polished_segments = [
        {
            "id": p.id,
            "speaker_id": p.speaker_id,
            "start_ms": p.start_ms,
            "end_ms": p.end_ms,
            "polished_text": p.polished_text,
            "seq_order": p.seq_order,
        }
        for p in r.polished_segments
    ]

    insight_data = None
    if r.insight:
        insight_data = {
            "executive_summary": r.insight.executive_summary,
            "topics": json.loads(r.insight.topics_json or "[]"),
            "decisions": json.loads(r.insight.decisions_json or "[]"),
            "action_items": json.loads(r.insight.action_items_json or "[]"),
            "risks": json.loads(r.insight.risks_json or "[]"),
            "project_updates": json.loads(r.insight.project_updates_json or "[]"),
            "new_projects": json.loads(r.insight.new_projects_json or "[]"),
            "full_report_md": r.insight.full_report_md,
        }

    linked_projects = [
        {
            "id": link.project.id,
            "name": link.project.name,
            "color": link.project.color,
            "status": link.project.status,
            "relevance_notes": link.relevance_notes
        }
        for link in r.project_links if link.project
    ]

    return {
        "id": r.id,
        "title": r.title,
        "filename": r.filename,
        "duration_seconds": round(r.duration_seconds, 1),
        "source_type": r.source_type,
        "status": r.status,
        "progress": r.progress,
        "status_message": r.status_message,
        "error_message": r.error_message,
        "primary_project_id": r.primary_project_id,
        "primary_project_name": r.primary_project.name if r.primary_project else None,
        "linked_projects": linked_projects,
        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        "raw_segments": raw_segments,
        "polished_segments": polished_segments,
        "insight": insight_data,
    }


@router.post("/{recording_id}/link_project")
def link_recording_project(recording_id: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="缺少 project_id")

    rec = db.query(Recording).filter(Recording.id == recording_id).first()
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not rec or not proj:
        raise HTTPException(status_code=404, detail="未找到录音或项目")

    existing = db.query(RecordingProjectLink).filter(
        RecordingProjectLink.recording_id == recording_id,
        RecordingProjectLink.project_id == project_id
    ).first()
    if not existing:
        link = RecordingProjectLink(
            recording_id=recording_id,
            project_id=project_id,
            relevance_notes=payload.get("relevance_notes", "手动关联")
        )
        db.add(link)
        db.commit()

    return {"message": f"录音已成功关联至项目【{proj.name}】"}


@router.post("/{recording_id}/retry")
def retry_pipeline(
    recording_id: str,
    background_tasks: BackgroundTasks,
    asr_provider: Optional[str] = None,
    db: Session = Depends(get_db),
):
    r = db.query(Recording).filter(Recording.id == recording_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="未找到该录音记录")

    r.status = "pending"
    r.progress = 5
    r.status_message = "正在重新进入处理队列..."
    r.error_message = None
    db.commit()

    background_tasks.add_task(run_pipeline, recording_id, asr_provider)
    return {"message": "已重新触发分析处理任务"}


@router.delete("/{recording_id}")
def delete_recording(recording_id: str, db: Session = Depends(get_db)):
    r = db.query(Recording).filter(Recording.id == recording_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="未找到该录音记录")

    # 1. Clean up audio files and chunks on disk
    patterns_to_clean = [
        str(RAW_AUDIO_DIR / f"{recording_id}*"),
        str(PROCESSED_AUDIO_DIR / f"{recording_id}*"),
    ]
    for pat in patterns_to_clean:
        for f in glob.glob(pat):
            try:
                os.remove(f)
            except Exception:
                pass

    for path in [r.raw_file_path, r.processed_file_path]:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    # 2. Clean up ProjectTimeline events associated with this recording
    db.query(ProjectTimeline).filter(ProjectTimeline.recording_id == recording_id).delete(synchronize_session=False)

    # 3. Clean up database relationships
    db.query(RecordingProjectLink).filter(RecordingProjectLink.recording_id == recording_id).delete(synchronize_session=False)
    db.query(AsrSegment).filter(AsrSegment.recording_id == recording_id).delete(synchronize_session=False)
    db.query(PolishedSegment).filter(PolishedSegment.recording_id == recording_id).delete(synchronize_session=False)
    db.query(MeetingInsight).filter(MeetingInsight.recording_id == recording_id).delete(synchronize_session=False)

    # 4. Delete recording record
    db.delete(r)
    db.commit()
    return {"message": "录音及所有关联分析数据已彻底删除"}


@router.get("/{recording_id}/audio")
def stream_audio(recording_id: str, download: bool = False, db: Session = Depends(get_db)):
    """Audio streaming endpoint for web audio player with auto-healing fallback and download support"""
    r = db.query(Recording).filter(Recording.id == recording_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="未找到该录音记录")

    target_path = r.processed_file_path if (r.processed_file_path and os.path.exists(r.processed_file_path)) else r.raw_file_path

    # Defensive Auto-Healing: If target_path is missing, locate existing chunk audio files
    if not target_path or not os.path.exists(target_path):
        import glob
        chunks = sorted(glob.glob(str(RAW_AUDIO_DIR / f"{recording_id}_chunk_*.wav")))
        if not chunks:
            chunks = sorted(glob.glob(str(RAW_AUDIO_DIR / f"{recording_id}_chunk_*.webm")))
        if not chunks:
            chunks = sorted(glob.glob(str(RAW_AUDIO_DIR / f"{recording_id}*")))

        if chunks:
            output_mp3 = str(PROCESSED_AUDIO_DIR / f"{recording_id}_processed.mp3")
            try:
                AudioService.concat_audio_files(chunks, output_mp3)
                if os.path.exists(output_mp3):
                    r.processed_file_path = output_mp3
                    r.raw_file_path = chunks[0]
                    db.commit()
                    target_path = output_mp3
                else:
                    target_path = chunks[0]
            except Exception:
                target_path = chunks[0]
        else:
            raise HTTPException(status_code=404, detail="音频文件不存在")

    media_type = "audio/mpeg"
    ext = ".mp3"
    if target_path.endswith(".wav"):
        media_type = "audio/wav"
        ext = ".wav"
    elif target_path.endswith(".m4a"):
        media_type = "audio/mp4"
        ext = ".m4a"
    elif target_path.endswith(".webm"):
        media_type = "audio/webm"
        ext = ".webm"
    elif target_path.endswith(".ogg"):
        media_type = "audio/ogg"
        ext = ".ogg"

    out_filename = None
    if download:
        clean_title = (r.title or "录音").replace('/', '_').replace('\\', '_')
        out_filename = f"{clean_title}{ext}"

    return FileResponse(target_path, media_type=media_type, filename=out_filename)


@router.patch("/{recording_id}/action_items")
def update_action_items(recording_id: str, items: list = Body(..., embed=False), db: Session = Depends(get_db)):
    """Update checkbox status of action items"""
    r = db.query(Recording).filter(Recording.id == recording_id).first()
    if not r or not r.insight:
        raise HTTPException(status_code=404, detail="未找到记录或纪要")

    r.insight.action_items_json = json.dumps(items, ensure_ascii=False)
    db.commit()
    return {"message": "待办状态更新成功"}


class RecordingTitleUpdate(BaseModel):
    title: str


@router.patch("/{recording_id}/title")
def update_recording_title(recording_id: str, body: RecordingTitleUpdate, db: Session = Depends(get_db)):
    r = db.query(Recording).filter(Recording.id == recording_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="录音不存在")
    r.title = body.title.strip() or r.title
    db.commit()
    db.refresh(r)
    return {"id": r.id, "title": r.title}


@router.get("/{recording_id}/export")
def export_markdown(recording_id: str, db: Session = Depends(get_db)):
    r = db.query(Recording).filter(Recording.id == recording_id).first()
    if not r or not r.insight:
        raise HTTPException(status_code=404, detail="未找到记录或纪要")

    import urllib.parse
    md_content = r.insight.full_report_md or "# 会议纪要"
    filename = f"{r.title}_会议纪要.md"
    quoted_filename = urllib.parse.quote(filename)

    return Response(
        content=md_content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted_filename}"}
    )
