import json
import traceback
from sqlalchemy.orm import Session
from app.database import SessionLocal, Recording, AsrSegment, PolishedSegment, MeetingInsight, Project, ProjectTimeline, RecordingProjectLink
from app.services.audio_service import AudioService
from app.services.asr.base import BaseAsrProvider
from app.services.asr.volc_asr import VolcAsrProvider
from app.services.asr.dashscope_asr import DashscopeAsrProvider
from app.services.asr.gemini_asr import GeminiAsrProvider
from app.services.asr.mock_asr import MockAsrProvider
from app.services.cleaning_service import CleaningService
from app.services.agent_service import MeetingAgentService, AgentAnalysisResult
from app.services.project_overview_service import refresh_project_overview, get_recording_project_ids
from app.config import settings


async def _refresh_linked_project_overviews(db: Session, recording: Recording):
    """Best-effort：刷新该录音关联的所有标签的 AI 进度看板，失败不影响主流程。"""
    try:
        for pid in get_recording_project_ids(db, recording):
            await refresh_project_overview(pid)
    except Exception:
        traceback.print_exc()


def get_asr_provider(provider_name: str) -> BaseAsrProvider:
    name = (provider_name or settings.DEFAULT_ASR_PROVIDER).lower()
    if name == "doubao" or name == "volc":
        if (settings.VOLC_APP_ID or settings.VOLC_ACCESS_KEY) and (settings.VOLC_ACCESS_TOKEN or settings.VOLC_SECRET_KEY):
            return VolcAsrProvider()
        return MockAsrProvider()
    elif name == "dashscope" or name == "qwen":
        if settings.DASHSCOPE_API_KEY:
            return DashscopeAsrProvider()
        return MockAsrProvider()
    elif name == "gemini":
        if settings.GEMINI_API_KEY:
            return GeminiAsrProvider()
        return MockAsrProvider()
    return MockAsrProvider()


def apply_project_updates(db: Session, recording: Recording, insights: AgentAnalysisResult):
    """
    Applies multi-project semantic disaggregation updates to DB:
    - Creates RecordingProjectLink
    - Injects ProjectTimeline events with timestamp links
    - Enforces state machine guardrails (on_hold alerts, archived markers)

    Idempotency: AI-generated timeline events for this recording are rebuilt
    from scratch on every run, so repeated reanalysis never duplicates events.
    """
    def _clear_existing_events(project_id: str):
        db.query(ProjectTimeline).filter(
            ProjectTimeline.project_id == project_id,
            ProjectTimeline.recording_id == recording.id,
        ).delete(synchronize_session=False)

    # 1. User designated primary project
    if recording.primary_project_id:
        existing_link = db.query(RecordingProjectLink).filter(
            RecordingProjectLink.recording_id == recording.id,
            RecordingProjectLink.project_id == recording.primary_project_id
        ).first()
        if not existing_link:
            db.add(RecordingProjectLink(
                recording_id=recording.id,
                project_id=recording.primary_project_id,
                relevance_notes="用户指定归属主项目"
            ))

    # 2. Process multi-project updates detected by DeepSeek / Agent
    for pu in (insights.project_updates or []):
        proj_id = pu.get("project_id")
        if not proj_id:
            continue
        proj = db.query(Project).filter(Project.id == proj_id).first()
        if not proj:
            continue

        # Link recording to project if not already linked
        existing_link = db.query(RecordingProjectLink).filter(
            RecordingProjectLink.recording_id == recording.id,
            RecordingProjectLink.project_id == proj.id
        ).first()
        if not existing_link:
            db.add(RecordingProjectLink(
                recording_id=recording.id,
                project_id=proj.id,
                relevance_notes=pu.get("relevance_summary", "")
            ))

        # Idempotent rebuild of this recording's events in this project
        _clear_existing_events(proj.id)

        # Add timeline events
        events = pu.get("timeline_events") or []
        for ev in events:
            event_title = ev.get("event_title") or "录音进展更新"
            event_detail = ev.get("event_detail") or ""
            event_type = ev.get("event_type") or "update"
            ts_ms = ev.get("audio_timestamp_ms", 0)

            # State guardrails:
            if proj.status == "on_hold":
                event_detail = f"【⏸️ 搁置中项目被重新讨论】{event_detail}（提示：该项目当前为搁置状态，建议评估是否重启）"
            elif proj.status in ["completed", "terminated"]:
                event_detail = f"【🏁 完结/终止项目回溯】{event_detail}"

            db.add(ProjectTimeline(
                project_id=proj.id,
                recording_id=recording.id,
                event_title=event_title,
                event_detail=event_detail,
                event_type=event_type,
                audio_timestamp_ms=ts_ms,
                source_title=recording.title
            ))

        if not proj.current_summary and pu.get("relevance_summary"):
            proj.current_summary = pu.get("relevance_summary")

    # 3. Fallback: if primary project set but not in project_updates
    if recording.primary_project_id:
        has_pu = any(pu.get("project_id") == recording.primary_project_id for pu in (insights.project_updates or []))
        if not has_pu:
            proj = db.query(Project).filter(Project.id == recording.primary_project_id).first()
            if proj:
                existing_event = db.query(ProjectTimeline).filter(
                    ProjectTimeline.project_id == proj.id,
                    ProjectTimeline.recording_id == recording.id,
                ).first()
                if not existing_event:
                    db.add(ProjectTimeline(
                        project_id=proj.id,
                        recording_id=recording.id,
                        event_title=f"录音归档：《{recording.title}》",
                        event_detail=insights.executive_summary[:200] if insights.executive_summary else "已关联该录音对话。",
                        event_type="update",
                        audio_timestamp_ms=0,
                        source_title=recording.title
                    ))

    db.commit()


async def run_pipeline(recording_id: str, asr_provider_name: str = None):
    """
    异步管道编排：
    1. 音频格式转码与预处理 (FFmpeg)
    2. ASR 语音识别与说话人角色分离 (保留原汁原味原始语气词) -> 存库 ①
    3. 口语规整与抛光 (Flash LLM 剔除冗余，绝对保留业务事实) -> 存库 ②
    4. 深度 Agent 分析 (全局纪要、决议、待办、多项目拆解、新项目立项建议) -> 存库 ③
    5. 多项目时间线与待办增量自动归流
    """
    db: Session = SessionLocal()
    try:
        recording = db.query(Recording).filter(Recording.id == recording_id).first()
        if not recording:
            return

        # Stage 1: Audio Preprocessing
        recording.status = "processing_audio"
        recording.progress = 15
        recording.status_message = "正在进行音频格式转换与降噪增强..."
        db.commit()

        processed_path, duration = AudioService.process_and_normalize(recording.raw_file_path, recording.id)
        recording.processed_file_path = processed_path
        recording.duration_seconds = duration
        db.commit()

        # Stage 2: ASR Transcription & Diarization
        recording.status = "asr_running"
        recording.progress = 40
        provider = get_asr_provider(asr_provider_name)
        recording.status_message = "正在调用 ASR 引擎识别语音并分离说话人角色..."
        db.commit()

        def on_asr_progress(status_msg: str):
            try:
                rec_cur = db.query(Recording).filter(Recording.id == recording_id).first()
                if rec_cur:
                    rec_cur.status_message = status_msg
                    db.commit()
            except Exception:
                pass

        raw_segments_data = await provider.transcribe(processed_path, recording.id, progress_callback=on_asr_progress)

        # Dual Track Store 1: Save raw verbatim segments
        db.query(AsrSegment).filter(AsrSegment.recording_id == recording.id).delete()
        for s in raw_segments_data:
            seg = AsrSegment(
                recording_id=recording.id,
                speaker_id=s.speaker_id,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                raw_text=s.raw_text,
                seq_order=s.seq_order,
            )
            db.add(seg)
        db.commit()

        # Stage 3: LLM Text Polishing
        recording.status = "cleaning"
        recording.progress = 65
        recording.status_message = f"正在调用大模型分批规整口语文本 (共 {len(raw_segments_data)} 个对话片段)..."
        db.commit()

        polished_segments_data = await CleaningService.polish_segments(raw_segments_data)

        # Dual Track Store 2: Save polished segments
        db.query(PolishedSegment).filter(PolishedSegment.recording_id == recording.id).delete()
        for p in polished_segments_data:
            p_seg = PolishedSegment(
                recording_id=recording.id,
                speaker_id=p.speaker_id,
                start_ms=p.start_ms,
                end_ms=p.end_ms,
                polished_text=p.polished_text,
                seq_order=p.seq_order,
            )
            db.add(p_seg)
        db.commit()

        # Stage 4: Meeting Agent Deep Insights with Project Knowledge
        recording.status = "analyzing"
        recording.progress = 85
        recording.status_message = "智能体正在提炼全局纪要、进行多项目交叉归流与潜在新项目识别..."
        db.commit()

        # Fetch projects context
        projects = db.query(Project).all()
        projects_meta = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description or "",
                "status": p.status,
                "ai_context": p.ai_context or "",
            }
            for p in projects
        ]

        insights = await MeetingAgentService.analyze(
            polished_segments_data,
            title=recording.title,
            projects_meta=projects_meta,
            primary_project_id=recording.primary_project_id
        )

        # Store 3: Save Agent outputs
        db.query(MeetingInsight).filter(MeetingInsight.recording_id == recording.id).delete()
        insight_obj = MeetingInsight(
            recording_id=recording.id,
            executive_summary=insights.executive_summary,
            topics_json=json.dumps(insights.topics, ensure_ascii=False),
            decisions_json=json.dumps(insights.decisions, ensure_ascii=False),
            action_items_json=json.dumps(insights.action_items, ensure_ascii=False),
            risks_json=json.dumps(insights.risks, ensure_ascii=False),
            project_updates_json=json.dumps(insights.project_updates, ensure_ascii=False),
            new_projects_json=json.dumps(insights.new_project_candidates, ensure_ascii=False),
            full_report_md=insights.full_report_md,
        )
        db.add(insight_obj)
        db.commit()

        # Stage 5: Apply project updates & timelines
        apply_project_updates(db, recording, insights)

        # Stage 6: Refresh AI progress dashboards for linked projects (best-effort)
        await _refresh_linked_project_overviews(db, recording)

        # Finish pipeline
        recording.status = "completed"
        recording.progress = 100
        recording.status_message = "处理全部完成"
        db.commit()

    except Exception as e:
        traceback.print_exc()
        if recording:
            recording.status = "failed"
            recording.status_message = "处理遇到异常"
            recording.error_message = str(e)
            db.commit()
    finally:
        db.close()


async def run_session_finalization(recording_id: str):
    """
    Finalizes an ongoing voice session (e.g. WeChat talk):
    1. Runs DeepSeek / Flash text cleaning on all AsrSegments
    2. Runs DeepSeek / Pro MeetingAgent analysis with project awareness
    3. Links to projects & timelines
    4. Marks recording as 'completed'
    """
    db: Session = SessionLocal()
    try:
        recording = db.query(Recording).filter(Recording.id == recording_id).first()
        if not recording:
            return

        recording.status = "analyzing"
        recording.progress = 60
        recording.status_message = "正在整合所有语音切片并调用 DeepSeek 进行深度清洗与多项目归流..."
        db.commit()

        raw_segs = db.query(AsrSegment).filter(AsrSegment.recording_id == recording_id).order_by(AsrSegment.seq_order).all()
        if not raw_segs:
            recording.status = "completed"
            recording.progress = 100
            recording.status_message = "无录音切片内容"
            db.commit()
            return

        # Filter out silence or system prompt placeholders before LLM polish and analysis
        valid_raw_segs = [
            s for s in raw_segs
            if s.raw_text
            and not s.raw_text.startswith("【")
            and not s.raw_text.startswith("（静音")
            and not s.raw_text.startswith("（短语音未识别")
            and not s.raw_text.startswith("（未检测到")
        ]
        target_segs = valid_raw_segs if valid_raw_segs else raw_segs

        from app.services.asr.base import AsrSegmentData as SegmentData
        seg_data_list = [
            SegmentData(
                speaker_id=s.speaker_id,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                raw_text=s.raw_text,
                seq_order=s.seq_order
            )
            for s in target_segs
        ]

        polished_data_list = await CleaningService.polish_segments(seg_data_list)
        db.query(PolishedSegment).filter(PolishedSegment.recording_id == recording_id).delete()
        for p in polished_data_list:
            p_seg = PolishedSegment(
                recording_id=recording_id,
                speaker_id=p.speaker_id,
                start_ms=p.start_ms,
                end_ms=p.end_ms,
                polished_text=p.polished_text,
                seq_order=p.seq_order,
            )
            db.add(p_seg)
        db.commit()

        # Fetch projects context
        projects = db.query(Project).all()
        projects_meta = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description or "",
                "status": p.status,
                "ai_context": p.ai_context or "",
            }
            for p in projects
        ]

        insights = await MeetingAgentService.analyze(
            polished_data_list,
            title=recording.title,
            projects_meta=projects_meta,
            primary_project_id=recording.primary_project_id
        )

        db.query(MeetingInsight).filter(MeetingInsight.recording_id == recording_id).delete()
        insight_obj = MeetingInsight(
            recording_id=recording.id,
            executive_summary=insights.executive_summary,
            topics_json=json.dumps(insights.topics, ensure_ascii=False),
            decisions_json=json.dumps(insights.decisions, ensure_ascii=False),
            action_items_json=json.dumps(insights.action_items, ensure_ascii=False),
            risks_json=json.dumps(insights.risks, ensure_ascii=False),
            project_updates_json=json.dumps(insights.project_updates, ensure_ascii=False),
            new_projects_json=json.dumps(insights.new_project_candidates, ensure_ascii=False),
            full_report_md=insights.full_report_md,
        )
        db.add(insight_obj)
        db.commit()

        # Apply project updates
        apply_project_updates(db, recording, insights)

        # Refresh AI progress dashboards for linked projects (best-effort)
        await _refresh_linked_project_overviews(db, recording)

        recording.status = "completed"
        recording.progress = 100
        recording.status_message = "处理全部完成"
        db.commit()

    except Exception as e:
        traceback.print_exc()
        if recording:
            recording.status = "failed"
            recording.status_message = "会话总结异常"
            recording.error_message = str(e)
            db.commit()
    finally:
        db.close()
