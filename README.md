# 🎙️ VoiceAgentHub - 私有化智能语音对话与项目大脑 Agent

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-0.110+-009688.svg" alt="FastAPI"></a>
  <a href="https://vuejs.org/"><img src="https://img.shields.io/badge/Vue-3.x-4FC08D.svg" alt="Vue 3"></a>
  <a href="https://tailwindcss.com/"><img src="https://img.shields.io/badge/TailwindCSS-3.x-38B2AC.svg" alt="TailwindCSS"></a>
  <a href="https://www.docker.com/"><img src="https://img.shields.io/badge/Docker-Ready-2496ED.svg" alt="Docker"></a>
</p>

> **VoiceAgentHub** 是一款轻量级、注重隐私的**私有化智能语音会话与多项目管理大脑 Agent**。
> 它可以将碎片化的日常语音对讲、工作会议录音、电话会谈自动转化为结构化纪要，通过大模型进行「数字保真」口语清洗、自动分离说话人角色，并将讨论事项智能归流入不同的项目档案，提取带责任人与截止日期的**全局行动待办总池**。

---

## 🌟 核心特性

### 1. 🎙️ 全渠道语音采集与无缝兼容
- **多格式文件上传**：无缝支持 MP3、M4A、WAV、AAC 以及微信导出的 `.silk` / `.amr` 语音格式，内置 FFmpeg 自动预处理与统一降噪转码。
- **即开即用微信式对讲**：支持移动端/桌面端「按住说话」与「点击对讲」两种录入交互，随时随地随心记录灵感或会谈要点。
- **大音频流式写盘**：上传通道支持 1MB 分块流式写盘，支持 500MB+ 两个小时以上的超长录音，彻底杜绝小内存服务器 OOM 崩溃。

### 2. ⚡ 多 ASR 语音识别引擎即插即用
- **火山引擎 (Volcengine / 豆包大模型语音识别)**：支持超高精度的多角色说话人分离（Diarization）与时间戳对齐。
- **阿里云 DashScope (千问 / 通义听悟)**：支持 Paraformer-v2 高保真语音识别。
- **Google Gemini 多模态**：支持直接基于原生音频理解与跨语种识别。
- **本地仿真 Mock 模式**：内置离线演示数据生成器，**无需配置任何 API Key** 即可直接体验端到端完整业务闭环。

### 3. 🛡️ 智能口语规整清洗与「数字实体保真」安全防线
- 自动滤除“呃、啊、那个、就是说、然后然后”等无效口头禅与结巴口误。
- **严守事实底线**：独创大模型清洗后的数值校验回退机制。若大模型将原音频中的关键数字（如“5000元预算”漏洗或篡改成“5元”），系统将自动回退到规则清洗，杜绝商业事实失真。

### 4. 🧠 多项目自适应语义大脑 (Multi-Project Brain)
- **跨项目自适应分流**：一段长会谈中涉及多个业务线时，Agent 自动切片并向各个关联项目独立注入对应的事实进展与动态。
- **项目时间线与大事记**：按时间轴沉淀项目里程碑，支持动态手动补录与调整。
- **一键立项与拆分**：从对话纪要中自动识别潜在新项目线索，支持一键独立建档或项目合并。

### 5. ✅ 全局行动待办总池 (Action Items Pool)
- **结构化提炼**：自动识别行动任务、责任人（Owner）与截止日期（Due Date）。
- **双向联动回写**：支持在全局总池或具体项目档案中随时勾选完成状态，采用基于 SHA1 内容指纹的持久化 ID，即时精准回写数据库。

### 6. 📱 适老友好 / 新手极简的全端自适应设计
- **大字号与高对比度交互**：专为移动端单手操作优化，关键信息一目了然，新手与老人轻松上手。
- **四大主功能 Tab**：底部快捷切换「实时语音」、「会话录音」、「项目大脑」与「待办总池」。
- **双轨溯源播放**：点击纪要中的任意句子或时间戳，内置音频播放器即刻精准跳播至对应秒数。

### 7. 🚀 极致轻量与高安全性
- **1C1G 优化**：单机全流程仅占用约 50MB 内存，支持一键配置 2GB Swap 内存守护。
- **凭证脱敏**：前端配置页面所有 API Key 均实施前四后四掩码保护，杜绝敏感密钥泄露。

---

## 🛠️ 技术架构

```text
┌─────────────────────────────────────────────────────────────┐
│                    Web Frontend (Single File)                │
│    Vue 3 (Composition API) + TailwindCSS + Lucide Icons    │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP / JSON API
┌──────────────────────────────▼──────────────────────────────┐
│                    FastAPI Core Application                 │
│  ┌───────────────────────┐        ┌──────────────────────┐  │
│  │   Audio Service       │        │   Pipeline Manager   │  │
│  │  (FFmpeg Transcode)   │        │ (Orchestrate Flow)   │  │
│  └───────────┬───────────┘        └──────────┬───────────┘  │
│              │                               │              │
│  ┌───────────▼───────────┐        ┌──────────▼───────────┐  │
│  │    ASR Provider Hub   │        │     Agent Service    │  │
│  │ Volcengine / DashScope│        │ (DeepSeek / Gemini / │  │
│  │ Gemini / Mock Diarize │        │  Cleaning Fidelity)  │  │
│  └───────────────────────┘        └──────────────────────┘  │
│                              │                              │
│              ┌───────────────▼───────────────┐              │
│              │   SQLite Database (SQLAlchemy)│              │
│              │ Projects, Recordings, Actions │              │
│              └───────────────────────────────┘              │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 快速开始

### 方式一：Docker 一键运行 (推荐)

确保系统已安装 [Docker](https://docs.docker.com/get-docker/) 与 [Docker Compose](https://docs.docker.com/compose/)。

```bash
# 1. 克隆代码仓库
git clone https://github.com/yangg881/voice-agent-hub.git
cd voice-agent-hub

# 2. 复制配置文件并按需填入 API Key (也可以不填，进入系统使用 Mock 模式体验)
cp .env.example .env

# 3. 启动容器
docker-compose up -d

# 4. 在浏览器中打开
# http://localhost:8000
```

---

### 方式二：本地 Python 运行

#### 环境要求
- Python 3.10+
- FFmpeg (用于音频转码与时长探测)
  - Ubuntu/Debian: `sudo apt-get install ffmpeg`
  - macOS: `brew install ffmpeg`
  - Windows: `winget install Gyan.FFmpeg` 或加入系统 PATH

```bash
# 1. 克隆并进入目录
git clone https://github.com/yangg881/voice-agent-hub.git
cd voice-agent-hub

# 2. 创建并激活虚拟环境
python3 -m venv venv
# Linux / macOS:
source venv/bin/activate
# Windows:
.\venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env

# 5. 启动开发服务器
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

访问 `http://localhost:8000` 即可开始使用。

---

### 方式三：Linux 单机生产一键部署

专为 Ubuntu / Debian 1C1G 小内存云服务器准备的自动化生产部署脚本：

```bash
git clone https://github.com/yangg881/voice-agent-hub.git /opt/voice-agent-hub
cd /opt/voice-agent-hub
chmod +x setup_server.sh
sudo ./setup_server.sh
```

脚本将自动完成：
1. 检查并挂载 2GB Swapfile，提供小内存 OOM 保护。
2. 安装系统级 Python3、pip、FFmpeg 与 Nginx。
3. 创建独立 Python 虚拟环境并安装 requirements。
4. 注册并启用 `voice-agent.service` Systemd 守护进程。

---

## ⚙️ 环境变量配置 (.env)

项目支持在运行时通过 Web 界面右上角的「⚙️ 设置」抽屉直接填写保存，亦可直接编辑 `.env` 文件：

```ini
# 服务基础配置
APP_NAME=VoiceAgentHub
DEBUG=False
PORT=8000
HOST=0.0.0.0

# 火山引擎 (豆包大模型语音识别 / ASR)
# 控制台: https://console.volcengine.com/speech/service/8
VOLC_APP_ID=
VOLC_ACCESS_TOKEN=
VOLC_CLUSTER_ID=volc.bigasr.sauc.duration

# 阿里云 DashScope (千问 / 通义听悟 Paraformer)
# 百炼平台: https://bailian.console.aliyun.com/
DASHSCOPE_API_KEY=

# Google Gemini API
# AI Studio: https://aistudio.google.com/
GEMINI_API_KEY=

# DeepSeek API (会议纪要提炼与口语清洗推荐引擎)
# 开放平台: https://platform.deepseek.com/
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat

# 默认引擎选择 (ASR: doubao / dashscope / gemini / mock)
# 默认大模型 (LLM: deepseek / gemini / dashscope)
DEFAULT_ASR_PROVIDER=doubao
DEFAULT_LLM_PROVIDER=deepseek
```

---

## 📂 项目目录结构

```text
voice-agent-hub/
├── app/
│   ├── routes/              # FastAPI 业务路由
│   │   ├── actions.py       # 全局待办总池与状态切换 API
│   │   ├── projects.py      # 项目档案、时间线与大事记 API
│   │   ├── recordings.py    # 音频上传、分块写盘与转写触发 API
│   │   └── settings.py      # 系统配置与凭证脱敏保存 API
│   ├── services/            # 核心业务服务层
│   │   ├── asr/             # 多平台语音识别引擎实现
│   │   │   ├── volc_asr.py      # 火山豆包大模型 ASR (含说话人分离)
│   │   │   ├── dashscope_asr.py # 阿里云通义听悟 ASR
│   │   │   ├── gemini_asr.py    # Google Gemini 多模态 ASR
│   │   │   └── mock_asr.py      # 本地离线演示引擎
│   │   ├── agent_service.py     # 深度纪要分析与结构化提炼
│   │   ├── cleaning_service.py  # 口语规整与数字保真防护网
│   │   ├── audio_service.py     # 音频探测与 FFmpeg 转码
│   │   └── pipeline.py          # 任务编排与幂等清理
│   ├── static/              # 前端静态资源
│   │   └── index.html       # 响应式全功能单页应用 (Vue 3 + Tailwind)
│   ├── config.py            # 全局配置管理
│   ├── database.py          # SQLite 数据库模型与会话管理
│   └── main.py              # 应用入口与中间件
├── Dockerfile               # Docker 镜像构建文件
├── docker-compose.yml       # Docker Compose 编排文件
├── setup_server.sh          # Linux 单机自动化生产部署脚本
├── nginx_voice_agent.conf.example # Nginx 反向代理与 HTTPS 配置参考
├── voice-agent.service      # Systemd 系统服务模板
├── requirements.txt         # Python 依赖清单
├── .env.example             # 环境变量模板
├── .gitignore               # Git 忽略配置
├── LICENSE                  # MIT 开源许可证
└── README.md                # 项目文档
```

---

## 🤝 参与贡献

欢迎提交 Issue 与 Pull Request！
1. Fork 本仓库。
2. 创建您的特性分支 (`git checkout -b feature/amazing-feature`)。
3. 提交您的修改 (`git commit -m 'feat: add some amazing feature'`)。
4. 推送分支 (`git push origin feature/amazing-feature`)。
5. 开启 Pull Request。

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源发布。
