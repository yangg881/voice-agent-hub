#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "🚀 开始部署 VoiceAgentHub 智能语音 Agent 系统..."
echo "=========================================================="

# 1. 内存保障：检查与增加 Swap 虚拟内存 (针对 1C1G 机器至关重要)
SWAP_TOTAL=$(free -m | awk '/Swap/ {print $2}')
if [ "$SWAP_TOTAL" -lt 1500 ]; then
    echo "⚙️ 检测到 Swap 内存小于 1.5GB，正在配置 2GB Swapfile 防止 OOM..."
    swapoff -a || true
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q "/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
    echo "✅ 2GB Swap 内存配置完成！"
fi

# 2. 系统依赖与 FFmpeg 安装
echo "📦 更新系统软件包并安装依赖 (FFmpeg, Nginx, Python3)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-pip python3-venv ffmpeg nginx curl git

# 3. 部署目录准备
APP_DIR="/opt/voice-agent-hub"
mkdir -p "$APP_DIR/data/audio/raw" "$APP_DIR/data/audio/processed" "$APP_DIR/data/db"

# 4. 创建 Python 独立虚拟环境
if [ ! -d "$APP_DIR/venv" ]; then
    echo "🐍 正在创建 Python 虚拟环境..."
    python3 -m venv "$APP_DIR/venv"
fi

echo "📦 正在安装 Python 依赖项..."
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# 5. 配置 Systemd 服务
echo "⚙️ 配置 Systemd 系统服务..."
cp "$APP_DIR/voice-agent.service" /etc/systemd/system/voice-agent.service
systemctl daemon-reload
systemctl enable voice-agent
systemctl restart voice-agent

# 6. 配置 Nginx 反向代理
echo "🌐 配置 Nginx 反向代理..."
rm -f /etc/nginx/sites-enabled/default
cp "$APP_DIR/nginx_voice_agent.conf" /etc/nginx/sites-available/voice-agent
ln -sf /etc/nginx/sites-available/voice-agent /etc/nginx/sites-enabled/voice-agent
nginx -t
systemctl restart nginx

# 7. 健康检查
echo "⏳ 等待服务启动中..."
sleep 3
HEALTH=$(curl -s http://127.0.0.1:8000/api/health || true)
echo "🔍 服务健康检查结果: $HEALTH"

echo "=========================================================="
echo "🎉 部署全部成功！"
echo "👉 请在手机或电脑浏览器中直接访问: http://128.1.38.216"
echo "=========================================================="
