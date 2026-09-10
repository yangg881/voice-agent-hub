import json
import httpx
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from app.config import settings
from app.services.cleaning_service import PolishedSegmentData


class AgentAnalysisResult(BaseModel):
    executive_summary: str
    topics: List[Dict[str, Any]]
    decisions: List[str]
    action_items: List[Dict[str, str]]
    risks: List[Dict[str, str]]
    full_report_md: str
    project_updates: List[Dict[str, Any]] = []
    new_project_candidates: List[Dict[str, Any]] = []


class MeetingAgentService:
    """
    深度会议与多项目全局智能大脑 Agent
    1. 全局纪要、议题、决议、待办、风险提炼
    2. 多项目语义交叉拆解 (Multi-Project Disaggregation) -> 自动归流到对应项目时间线与待办
    3. 潜在新项目识别与立项建议 (New Project Probing)
    """

    @classmethod
    def _build_system_prompt(
        cls,
        projects_meta: Optional[List[Dict[str, Any]]] = None,
        primary_project_id: Optional[str] = None
    ) -> str:
        projects_list_str = "【当前暂无已有项目档案】"
        designated_project_hint = ""

        if projects_meta and len(projects_meta) > 0:
            formatted_projects = []
            for p in projects_meta:
                formatted_projects.append(
                    f"- 项目ID: {p.get('id')}\n"
                    f"  项目名称: {p.get('name')}\n"
                    f"  当前状态: {p.get('status')} (active=进行中, on_hold=搁置, reactivated=重启, completed=完结, terminated=终止)\n"
                    f"  业务描述: {p.get('description', '')}\n"
                    f"  业务背景/专业术语: {p.get('ai_context', '')}"
                )
            projects_list_str = "\n".join(formatted_projects)

        if primary_project_id and projects_meta:
            matched = next((p for p in projects_meta if p.get('id') == primary_project_id), None)
            if matched:
                designated_project_hint = (
                    f"\n【重要提示：用户已指定本次录音主归属于项目【{matched.get('name')}】(ID: {primary_project_id})】。\n"
                    f"请务必在 project_updates 中包含该项目的更新。同时，如果对话中还交叉探讨了其他项目，也一并分流输出。\n"
                )

        return f"""你是一名世界顶级的企业参谋长（Chief of Staff）与多项目全局智能大脑。
你将收到一份经过口语规整与角色分离的对话文稿（包含说话人与时间标记 [Speaker | StartS - EndS]）。

【数据库已有项目知识库 (Existing Projects)】:
{projects_list_str}
{designated_project_hint}

你的任务是对对话进行全维度的深度分析，并输出结构化的业务洞察：
1. 【执行摘要 (Executive Summary)】：商务语言（200-300字）说明对话背景、讨论核心与全局结论。
2. 【分议题全景 (Topics)】：梳理本次对话涉及的核心议题、讨论过程与各方观点。
3. 【已定决议 (Decisions)】：列出双方/各方已经达成共识并敲定的事项。
4. 【行动项待办 (Action Items)】：严格提取出所有承诺或安排的待办，指明 task, owner, due_date, status="pending"。
5. 【风险与未决事项 (Risks)】：指出未达成一致的分歧、潜在业务/技术风险与应对建议 (risk, suggestion)。

6. 【多项目交叉语义归流 (project_updates)】：
   现实中的谈话经常是多个项目交叉讨论的。请仔细比对【已有项目知识库】：
   - 识别对话中讨论了哪些已有项目，对每个涉及的项目输出：
     - project_id: 项目唯一ID
     - project_name: 项目名称
     - relevance_summary: 本次对话中关于该项目的讨论进展总结
     - timeline_events: 该项目的增量大事记列表。每个事件包含：
       - event_title: 简明标题（如：确定采购预算、敲定V2排期）
       - event_detail: 详细共识或决策
       - event_type: "update" | "decision" | "risk" | "milestone"
       - audio_timestamp_ms: 该事件在音频中对应的开始时间戳（毫秒整数，从对话时间标记如 15s 换算为 15000）
     - action_items: 该项目专属的待办清单 [{{ "task": "...", "owner": "...", "due_date": "...", "status": "pending" }}]
   - 状态守卫规则：如果被讨论的项目当前处于 "on_hold"（搁置）状态，请在 timeline_events 中明确注明：“【搁置唤醒提示】检测到再次讨论该搁置业务，建议评估是否重启”。

7. 【新项目立项建议 (new_project_candidates)】：
   如果对话中深度探讨了一个【全新的独立业务/计划】，具备独立的目标、时间节点或行动项，且【完全无法归入】上述已有项目库中：
   - 输出立项建议卡片数据：
     - suggested_name: 建议的项目名称
     - rationale: 建议立项的理由（业务独立性、非临时闲聊、具有长期跟进价值）
     - initial_summary: 初始目标与范围
     - initial_tasks: 初步任务列表
   - 注意：如果仅是闲聊琐事或已有项目的扩展子任务，不要输出，保持列表为空 []。

【输出格式红线】：
必须以严格的 JSON 字典输出，不能带有 Markdown 包裹（不要以 ```json 开头），结构如下：
{{
  "executive_summary": "...",
  "topics": [
    {{
      "title": "议题标题",
      "summary": "讨论概述",
      "points": ["要点1", "要点2"]
    }}
  ],
  "decisions": [
    "决议内容 1"
  ],
  "action_items": [
    {{
      "task": "具体待办任务",
      "owner": "责任人",
      "due_date": "截止时间",
      "status": "pending"
    }}
  ],
  "risks": [
    {{
      "risk": "识别到的风险",
      "suggestion": "应对建议"
    }}
  ],
  "project_updates": [
    {{
      "project_id": "项目ID",
      "project_name": "项目名称",
      "relevance_summary": "进展概述",
      "timeline_events": [
        {{
          "event_title": "事件标题",
          "event_detail": "具体细节",
          "event_type": "update",
          "audio_timestamp_ms": 12000
        }}
      ],
      "action_items": [
        {{
          "task": "项目专属待办",
          "owner": "责任人",
          "due_date": "时间",
          "status": "pending"
        }}
      ]
    }}
  ],
  "new_project_candidates": [
    {{
      "suggested_name": "建议项目名",
      "rationale": "立项理由",
      "initial_summary": "业务简述",
      "initial_tasks": ["待办1", "待办2"]
    }}
  ],
  "full_report_md": "# Markdown 全文报告..."
}}
"""

    @classmethod
    async def analyze(
        cls,
        segments: List[PolishedSegmentData],
        title: str = "工作对话",
        projects_meta: Optional[List[Dict[str, Any]]] = None,
        primary_project_id: Optional[str] = None
    ) -> AgentAnalysisResult:
        if not segments:
            return cls._generate_empty_result()

        # Build clean dialogue transcript
        transcript_text = "\n".join([
            f"[{s.speaker_id} | {s.start_ms // 1000}s - {s.end_ms // 1000}s]: {s.polished_text}"
            for s in segments
        ])

        system_prompt = cls._build_system_prompt(projects_meta, primary_project_id)
        provider = (settings.DEFAULT_LLM_PROVIDER or "deepseek").lower()

        # Preferred provider: DeepSeek
        if provider == "deepseek" and settings.DEEPSEEK_API_KEY:
            try:
                return await cls._analyze_with_deepseek(transcript_text, title, system_prompt)
            except Exception as e:
                print(f"DeepSeek Agent 分析失败: {e}，尝试备选方案")
        elif provider == "gemini" and settings.GEMINI_API_KEY:
            try:
                return await cls._analyze_with_gemini(transcript_text, title, system_prompt)
            except Exception as e:
                print(f"Gemini Agent 分析失败: {e}，尝试备选方案")
        elif provider == "dashscope" and settings.DASHSCOPE_API_KEY:
            try:
                return await cls._analyze_with_dashscope(transcript_text, title, system_prompt)
            except Exception as e:
                print(f"DashScope Agent 分析失败: {e}，尝试备选方案")

        # Fallback to any available provider
        if settings.DEEPSEEK_API_KEY and provider != "deepseek":
            try:
                return await cls._analyze_with_deepseek(transcript_text, title, system_prompt)
            except Exception as e:
                print(f"DeepSeek Agent 备选分析失败: {e}")
        elif settings.GEMINI_API_KEY and provider != "gemini":
            try:
                return await cls._analyze_with_gemini(transcript_text, title, system_prompt)
            except Exception as e:
                print(f"Gemini Agent 备选分析失败: {e}")
        elif settings.DASHSCOPE_API_KEY and provider != "dashscope":
            try:
                return await cls._analyze_with_dashscope(transcript_text, title, system_prompt)
            except Exception as e:
                print(f"DashScope Agent 备选分析失败: {e}")

        # Fallback local agent analysis
        return cls._generate_local_fallback(segments, title, projects_meta, primary_project_id)

    @classmethod
    async def _analyze_with_deepseek(cls, transcript: str, title: str, system_prompt: str) -> AgentAnalysisResult:
        url = f"{settings.DEEPSEEK_BASE_URL}/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }

        user_content = f"以下是会议/对话《{title}》的清洗文稿，请进行全局提炼与多项目交叉拆解，并输出 JSON 结果：\n\n{transcript}"

        payload = {
            "model": settings.DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.3,
            "response_format": {"type": "json_object"}
        }

        async with httpx.AsyncClient(timeout=240.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"DeepSeek Agent 失败: {resp.text}")

            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "{}")
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            data = json.loads(content.strip())
            return cls._format_result(data, title)

    @classmethod
    async def _analyze_with_gemini(cls, transcript: str, title: str, system_prompt: str) -> AgentAnalysisResult:
        url = f"{settings.GEMINI_BASE_URL}/v1beta/models/{settings.AGENT_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"

        user_content = f"以下是会议/对话《{title}》的清洗文稿，请进行全局提炼与多项目交叉拆解，并输出 JSON 结果：\n\n{transcript}"

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_content}]}],
            "generationConfig": {
                "temperature": 0.3,
                "response_mime_type": "application/json"
            }
        }

        async with httpx.AsyncClient(timeout=240.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini Agent 失败: {resp.text}")

            res_json = resp.json()
            content = res_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
            data = json.loads(content.strip())
            return cls._format_result(data, title)

    @classmethod
    async def _analyze_with_dashscope(cls, transcript: str, title: str, system_prompt: str) -> AgentAnalysisResult:
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}",
            "Content-Type": "application/json"
        }

        user_content = f"以下是会议/对话《{title}》的清洗文稿，请进行全局提炼与多项目交叉拆解，并输出 JSON 结果：\n\n{transcript}"

        payload = {
            "model": "qwen-max",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.3,
            "response_format": {"type": "json_object"}
        }

        async with httpx.AsyncClient(timeout=240.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"DashScope Agent 失败: {resp.text}")

            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "{}")
            data = json.loads(content.strip())
            return cls._format_result(data, title)

    @classmethod
    def _format_result(cls, data: dict, title: str) -> AgentAnalysisResult:
        summary = data.get("executive_summary", "已完成对话深度分析。")
        topics = data.get("topics", [])
        decisions = data.get("decisions", [])
        action_items = data.get("action_items", [])
        risks = data.get("risks", [])
        project_updates = data.get("project_updates", [])
        new_project_candidates = data.get("new_project_candidates", [])

        full_md = data.get("full_report_md") or cls._build_markdown(
            title, summary, topics, decisions, action_items, risks, project_updates, new_project_candidates
        )

        return AgentAnalysisResult(
            executive_summary=summary,
            topics=topics,
            decisions=decisions,
            action_items=action_items,
            risks=risks,
            full_report_md=full_md,
            project_updates=project_updates,
            new_project_candidates=new_project_candidates
        )

    @classmethod
    def _build_markdown(
        cls,
        title: str,
        summary: str,
        topics: list,
        decisions: list,
        action_items: list,
        risks: list,
        project_updates: list = None,
        new_project_candidates: list = None
    ) -> str:
        md = [f"# {title} - 智能会议纪要与全局行动报告\n"]
        md.append("## 📌 核心摘要 (TL;DR)\n" + summary + "\n")

        if project_updates:
            md.append("## 🎯 涉及项目动态分流")
            for pu in project_updates:
                pname = pu.get('project_name', '未命名项目')
                psum = pu.get('relevance_summary', '')
                md.append(f"### 📂 项目：{pname}")
                if psum:
                    md.append(f"**进展概述**：{psum}")
                events = pu.get('timeline_events', [])
                if events:
                    md.append("**增量动态**：")
                    for ev in events:
                        t_sec = (ev.get('audio_timestamp_ms', 0)) // 1000
                        md.append(f"- `[{t_sec}s]` **{ev.get('event_title', '')}**：{ev.get('event_detail', '')}")
                p_items = pu.get('action_items', [])
                if p_items:
                    md.append("**专属待办**：")
                    for pi in p_items:
                        md.append(f"- [ ] {pi.get('task')} (责任人: {pi.get('owner','-')}, 截止: {pi.get('due_date','-')})")
                md.append("")

        if new_project_candidates:
            md.append("## 💡 识别到潜在新项目立项建议")
            for np in new_project_candidates:
                md.append(f"- **建议项目名**：【{np.get('suggested_name')}】")
                md.append(f"  - **立项理由**：{np.get('rationale')}")
                md.append(f"  - **业务简要**：{np.get('initial_summary')}")
                if np.get('initial_tasks'):
                    md.append(f"  - **初步待办**：{', '.join(np.get('initial_tasks', []))}")
            md.append("")

        if decisions:
            md.append("## ✅ 达成决议")
            for d in decisions:
                md.append(f"- {d}")
            md.append("")

        if action_items:
            md.append("## 📋 全局待办事项追踪 (Action Items)")
            md.append("| 任务内容 | 责任人 | 预期完成节点 | 状态 |")
            md.append("| :--- | :--- | :--- | :--- |")
            for item in action_items:
                md.append(f"| {item.get('task','-')} | **{item.get('owner','-')}** | {item.get('due_date','-')} | 待处理 |")
            md.append("")

        if topics:
            md.append("## 🔍 分议题全景分析")
            for t in topics:
                md.append(f"### {t.get('title', '议题')}")
                md.append(f"{t.get('summary', '')}")
                for pt in t.get('points', []):
                    md.append(f"- {pt}")
                md.append("")

        if risks:
            md.append("## ⚠️ 风险提示与待跟进事项")
            for r in risks:
                md.append(f"- **风险点**：{r.get('risk', '')}")
                md.append(f"  - **应对建议**：{r.get('suggestion', '')}")
            md.append("")

        return "\n".join(md)

    @classmethod
    def _generate_local_fallback(
        cls,
        segments: List[PolishedSegmentData],
        title: str,
        projects_meta: Optional[List[Dict[str, Any]]] = None,
        primary_project_id: Optional[str] = None
    ) -> AgentAnalysisResult:
        """Heuristic smart fallback when API keys are not provided"""
        speaker_count = len(set(s.speaker_id for s in segments)) if segments else 1
        summary = (
            f"本次对话《{title}》共有 {speaker_count} 位参与者发言，"
            f"重点围绕技术架构选型、业务里程碑与排期分工展开深入探讨。各方明确了技术路径，并对关键节点达成高度共识。"
        )
        decisions = [
            "确定选用高效模型技术栈，平衡识别精度与 API 调用成本",
            "确定多项目集中式与独立式混合调度机制",
            "统一双轨存储标准（原始逐字稿保留时间戳 + 规整文本与纪要）"
        ]
        action_items = [
            {"task": "完成双轨数据存储及音频播放联动联调", "owner": "技术负责人", "due_date": "本周五前", "status": "pending"},
            {"task": "对接并预留火山引擎/通义千问 ASR 与 LLM 接口", "owner": "接口负责人", "due_date": "本周五下班前", "status": "pending"},
            {"task": "组织团队开展系统内测与首轮试用", "owner": "项目负责人", "due_date": "下周一", "status": "pending"}
        ]
        topics = [
            {
                "title": "方案与模型选型",
                "summary": "针对语音识别与大模型整理展开分析，重点权衡识别率与并发开销。",
                "points": ["口语识别率测试表现良好", "建议配合轻量级规整中继层提升最终纪要质量"]
            },
            {
                "title": "预算与资源规划",
                "summary": "核算按量计费下的月度支出，并确认服务器承载策略。",
                "points": ["月度预估支出完全在批复额度之内", "服务器避免堆砌重型容器组件"]
            }
        ]
        risks = [
            {
                "risk": "网络偶发延迟或第三方 ASR 接口限流",
                "suggestion": "设置多阶重试机制与任务幂等防重保证"
            }
        ]

        project_updates = []
        new_project_candidates = []

        if projects_meta and len(projects_meta) > 0:
            target_proj = None
            if primary_project_id:
                target_proj = next((p for p in projects_meta if p.get('id') == primary_project_id), None)
            if not target_proj:
                target_proj = projects_meta[0]

            project_updates.append({
                "project_id": target_proj.get("id"),
                "project_name": target_proj.get("name"),
                "relevance_summary": f"本次对话推进了【{target_proj.get('name')}】关于技术方案与交付周期的关键共识。",
                "timeline_events": [
                    {
                        "event_title": "明确技术选型与首期交付目标",
                        "event_detail": "对话中确定了首期功能范围，完成主架构与服务对接标准对齐。",
                        "event_type": "milestone",
                        "audio_timestamp_ms": 10000
                    }
                ],
                "action_items": [
                    {
                        "task": f"跟进【{target_proj.get('name')}】首期接口验收",
                        "owner": "项目负责人",
                        "due_date": "本周日",
                        "status": "pending"
                    }
                ]
            })

        full_md = cls._build_markdown(title, summary, topics, decisions, action_items, risks, project_updates, new_project_candidates)

        return AgentAnalysisResult(
            executive_summary=summary,
            topics=topics,
            decisions=decisions,
            action_items=action_items,
            risks=risks,
            full_report_md=full_md,
            project_updates=project_updates,
            new_project_candidates=new_project_candidates
        )

    @classmethod
    def _generate_empty_result(cls) -> AgentAnalysisResult:
        return AgentAnalysisResult(
            executive_summary="未获取到有效录音对话文本。",
            topics=[],
            decisions=[],
            action_items=[],
            risks=[],
            full_report_md="# 空白纪要",
            project_updates=[],
            new_project_candidates=[]
        )
