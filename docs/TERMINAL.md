# Orange Pi 实验终端说明

终端把按键录音发给 PC 的丽萨 bridge，接收状态与 WAV 音频。它不是独立运行完整智能体的成品设备。代码主要面向 Orange Pi 5 Pro / RK3588S；其他板卡需重新确认 GPIO、SPI、音频驱动与系统配置。

## 连接与配置

PC 端安装语音依赖，设置 `JARVIS_BRIDGE_TOKEN` 后运行：

```powershell
.\.venv\Scripts\jarvis.exe bridge --host 0.0.0.0 --port 8765
```

绑定 `0.0.0.0` 会让局域网客户端连接到服务。配置防火墙后，终端填写 PC 的局域网地址；不要填 `127.0.0.1`，它在板端指向板子自己。此实验链路使用普通 WebSocket，未内建 TLS。

```yaml
bridge:
  host: "192.168.1.100" # 替换为自己的 PC 地址
  port: 8765
  token: "填写和-PC-一致的-token"
  device_id: "lisa-opi5-01"
```

真实配置留在本地，不提交 token。设备端点为 `/ws/device`。客户端支持 hello、audio、ping 等消息，服务端回传状态和二进制 WAV。

## 手动启动

以下在板端 Linux 的 `terminal` 目录执行：

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python main.py --config config.yaml --selftest
python main.py --config config.yaml
```

音频依赖可能需要系统的 PortAudio 与 ALSA 配置；使用 `arecord -l` / `aplay -l` 检查设备。CPU 上 Faster Whisper 的性能需要实测，安装成功也不代表 GPIO/SPI 功能可用。

`setup.sh` 会安装系统依赖、创建服务用户、部署到 `/opt/lisa-terminal` 并修改系统配置。先阅读脚本和 `lisa-terminal.service`，确认板卡、系统与路径后再运行 `sudo bash setup.sh`；它不是 Windows 安装脚本。

## 硬件与尚未完成的部分

- PTT 按钮、GPIO 去抖、录音、LED、心跳、重连和音频播放已有代码。
- 示例 GPIO 数字和 SPI 路径不是通用接线保证；原理图、电平和实际板卡引脚需核对后使用。
- 配置中的 OLED 字段属于预留项；display 分支当前只记录日志。
- 板端断网流程有转写与简单文本应答，尚无完整离线 LLM/TTS。
- ESP32 出现在设备协议的历史命名中；本仓库不含完整 ESP32 固件。
- 本次发布没有连接真实 Orange Pi、按钮、灯环或麦克风验收。

欢迎提交带板卡型号、镜像版本、音频设备和最小复现日志的实测反馈。
