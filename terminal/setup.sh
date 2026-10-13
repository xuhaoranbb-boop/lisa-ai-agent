#!/bin/bash
# ============================================================
# 丽萨终端 - 一键安装脚本
# 适用于 Orange Pi 5 Pro (Debian/Ubuntu, aarch64)
#
# 用法：
#   chmod +x setup.sh
#   sudo ./setup.sh
# ============================================================

set -e

echo "============================================"
echo "  丽萨终端 安装脚本 v1.0"
echo "  Orange Pi 5 Pro (RK3588S)"
echo "============================================"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 检查 root
if [ "$EUID" -ne 0 ]; then
    error "请用 sudo 运行此脚本"
    exit 1
fi

INSTALL_DIR="/opt/lisa-terminal"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_USER="lisa"

# ==================== 1. 系统依赖 ====================
info "安装系统依赖..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    portaudio19-dev libsndfile1 \
    spi-tools \
    i2c-tools \
    git \
    2>/dev/null

# ==================== 2. 启用 SPI 和 I2C ====================
info "启用 SPI..."
# Orange Pi 5 Pro 使用 rk3588s 设备树
if ! grep -q "spidev" /boot/dtbo/overlay.txt 2>/dev/null; then
    # 尝试启用 SPI overlay
    if [ -f /boot/orangepiEnv.txt ]; then
        grep -q "spi" /boot/orangepiEnv.txt || echo "overlays=spi0" >> /boot/orangepiEnv.txt
        info "SPI overlay 已添加（需要重启生效）"
    fi
fi

# 检查 SPI 设备
if [ -e /dev/spidev0.0 ]; then
    info "SPI 设备就绪: /dev/spidev0.0"
else
    warn "SPI 设备未找到 — LED 灯环将使用日志模式"
    warn "重启后如果仍未出现，请检查设备树配置"
fi

# ==================== 3. 创建用户 ====================
info "创建服务用户..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -m -s /bin/bash "$SERVICE_USER"
    info "用户 $SERVICE_USER 已创建"
fi

# 把用户加入 gpio, spi, i2c 组
for grp in gpio spi i2c audio; do
    if getent group "$grp" &>/dev/null; then
        usermod -aG "$grp" "$SERVICE_USER" 2>/dev/null || true
    fi
done

# ==================== 4. 安装 Python 环境 ====================
info "创建 Python 虚拟环境..."
if [ -d "$VENV_DIR" ]; then
    warn "虚拟环境已存在，跳过创建"
else
    python3 -m venv "$VENV_DIR"
fi

info "安装 Python 依赖..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r requirements.txt -q

# ==================== 5. 部署文件 ====================
info "部署到 $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp -r lisa_client/ "$INSTALL_DIR/"
cp main.py "$INSTALL_DIR/"
cp config.yaml "$INSTALL_DIR/"

# 权限
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ==================== 6. 安装 systemd 服务 ====================
info "安装 systemd 服务..."
cp lisa-terminal.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable lisa-terminal.service
info "服务已安装并设为开机自启"

# ==================== 7. 配置提示 ====================
echo ""
echo "============================================"
echo -e "${GREEN}  安装完成！${NC}"
echo "============================================"
echo ""
echo "接下来你需要："
echo ""
echo "  1. 编辑配置文件，填入 PC 的 IP 和 token："
echo "     sudo nano $INSTALL_DIR/config.yaml"
echo ""
echo "     需要改的："
echo "       bridge.host:  你电脑的局域网 IP"
echo "       bridge.port:  bridge 端口（默认 8080）"
echo "       bridge.token: 和电脑端 JARVIS_TOKEN 一致"
echo ""
echo "  2. 运行硬件自检："
echo "     cd $INSTALL_DIR"
echo "     sudo $VENV_DIR/bin/python main.py --selftest"
echo ""
echo "  3. 手动启动测试："
echo "     sudo systemctl start lisa-terminal"
echo "     sudo journalctl -u lisa-terminal -f"
echo ""
echo "  4. 查看电脑端 IP 的方法（在 PC 上运行）："
echo "     ipconfig | findstr IPv4"
echo ""
echo "============================================"
