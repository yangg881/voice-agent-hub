import json
import logging
from typing import List, Optional

import httpx

from app.config import settings
from app.database import (
    SessionLocal, Project, Recording, MeetingInsight, RecordingProjectLink,
)

logger = logging.getLogger(__name__)

# 送进 AI 的最近录音条数上限（防止 prompt 过长）
MAX_RECORDINGS_FOR_CONTEXT = 12


def get_recording_project_ids(db, recording: Recording) -> List[str]:
    """返回一条录音关联的所有标签 ID（主标签 + 链接标签）。"""
    ids = []
    if recording.primary_project_id:
        ids.append(recording.primary_project_id)
    for link in db.query(RecordingProjectLink).filter(
        RecordingProjectLink.recording_id == recording.id
    ).all():
        if link.project_id and link.project_id not in ids:
            ids.append(link.project_id)
    return ids


def _build_context_block(rec: Recording) -> str:
    """把一条录音的 AI 分析结果压缩成给模型看的文本块。"""
    lines = [f"■ 《{rec.title}》（{rec.created_at.strftime('%Y-%m-%d %H:%M') if rec.created_at else '时间未知'}）"]
    ins = rec.insight
    if not ins:
        lines.append("  （本条尚未完成 AI 分析）")
        return "\n".join(lines)

    if ins.executive_summary:
        lines.append(f"  摘要：{ins.executive_summary}")
    try:
        decisions = json.loads(ins.decisions_json or "[]")
        for d in decisions[:5]:
            txt = d if isinstance(d, str) else (d.get("decision") or d.get("content") or "")
            if txt:
                lines.append(f"  决议：{txt}")
    except Exception:
        pass
    try:
        actions = json.loads(ins.action_items_json or "[]")
        for a in actions[:6]:
            if isinstance(a, dict):
                status = "已完成" if a.get("status") == "completed" else "待办"
                owner = f"（{a.get('owner')}）" if a.get("owner") else ""
                lines.append(f"  [{status}] {a.get('task', '')}{owner}")
    except Exception:
        pass
    try:
        risks = json.loads(ins.risks_json or "[]")
        for r in risks[:3]:
            txt = r if isinstance(r, str) else (r.get("risk") or "")
            if txt:
                lines.append(f"  风险：{txt}")
    except Exception:
        pass
    return "\n".join(lines)


def _build_prompt(project: Project, prev_overview: Optional[dict], blocks: List[str]) -> str:
    prev_str = "（暂无历史综述，这是首次生成）"
    if prev_overview and prev_overview.get("overall"):
        prev_str = json.dumps(prev_overview, ensure_ascii=False, indent=2)

    return f"""你是一名项目进度分析师。请根据标签「{project.name}」下各条语音纪要的 AI 分析结果，生成一份该标签的【整体进度看板】。

【标签信息】
- 名称：{project.name}
- 描述：{project.description or '（无）'}
- 背景上下文：{project.ai_context or '（无）'}

【上一份进度综述】（请在其基础上做增量更新，保留仍然有效的结论，淘汰已被推翻的内容）
{prev_str}

【该标签下的语音纪要内容】（按时间从新到旧）
{chr(10).join(blocks)}

【输出要求】
以严格 JSON 输出（不要 Markdown 包裹），结构如下：
{{
  "overall": "一段话总体进度综述（80-150字），说明这个标签/项目目前整体推进到哪一步、最近在忙什么",
  "progress": ["已经推进/敲定的事项，每条一句话，最多6条"],
  "pending": ["尚未完成、需要继续推进的事项，每条一句话，最多6条"],
  "risks": ["当前存在的风险或悬而未决的问题，每条一句话，最多4条，没有则为空数组"]
}}

注意：语言要口语化、说人话，让完全不了解背景的人也能一眼看懂进度；不要堆砌术语；每条都要具体（带时间/人物/事项），不要写"持续推进中"这种空话。"""


async def refresh_project_overview(project_id: str) -> bool:
    """
    调用 AI 重新生成某标签的整体进度看板，写回 projects.current_summary（JSON 字符串）。
    失败静默降级：保留旧综述，返回 False。绝不抛出异常影响主流程。
    """
    if not settings.DEEPSEEK_API_KEY:
        logger.warning("[overview] DEEPSEEK_API_KEY 未配置，跳过看板生成")
        return False

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return False

        recs = db.query(Recording).filter(
            (Recording.primary_project_id == project_id)
            | (Recording.id.in_(
                db.query(RecordingProjectLink.recording_id).filter(
                    RecordingProjectLink.project_id == project_id
                )
            ))
        ).order_by(Recording.created_at.desc()).limit(MAX_RECORDINGS_FOR_CONTEXT).all()

        if not recs:
            return False

        prev_overview = None
        if project.current_summary:
            try:
                prev_overview = json.loads(project.current_summary)
            except Exception:
                prev_overview = {"overall": project.current_summary}

        blocks = [_build_context_block(r) for r in recs]
        prompt = _build_prompt(project, prev_overview, blocks)

        url = f"{settings.DEEPSEEK_BASE_URL}/chat/completions"
        payload = {
            "model": settings.DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code != 200:
                logger.error(f"[overview] DeepSeek 调用失败: {resp.status_code} {resp.text[:200]}")
                return False

            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "{}").strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

            data = json.loads(content.strip())

        overview = {
            "overall": str(data.get("overall") or ""),
            "progress": [str(x) for x in (data.get("progress") or [])][:6],
            "pending": [str(x) for x in (data.get("pending") or [])][:6],
            "risks": [str(x) for x in (data.get("risks") or [])][:4],
        }
        if not overview["overall"]:
            return False

        project.current_summary = json.dumps(overview, ensure_ascii=False)
        db.commit()
        logger.info(f"[overview] 标签「{project.name}」看板已更新")
        return True

    except Exception as e:
        logger.error(f"[overview] 生成看板异常 project={project_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return False
    finally:
        db.close()
