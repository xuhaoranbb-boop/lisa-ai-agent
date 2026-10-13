# 丽萨 AI · Lisa AI Agent

一个从个人日常需求出发、仍在持续完善的 AI 智能体：希望把文字聊天、语音交流、长期记忆和电脑操作放进同一个助手里，让它逐步成为真正用得上的个人助理。

**当前发布为实验性源码预览。** 桌面版主要面向 Windows；Orange Pi 终端是探索中的硬件分支。仓库版本字段为 `1.0.0`，这不代表全部功能已通过生产验收。欢迎体验、讨论、提交问题和 Pull Request，也希望各位大佬提供宝贵意见。

> 对外叫「丽萨」，历史内部名称是 Jarvis / Ashley。因此 Python 包名 `jarvis-agent`、命令 `jarvis`、环境变量 `JARVIS_*` 和目录 `ashley-upstream` 暂时保留，避免破坏已有接口。

## 先看这些文档

| 文档 | 内容 |
|---|---|
| [详细使用说明](docs/USER_GUIDE.md) | 环境准备、安装、模型配置、桌面/语音使用、数据管理和排障 |
| [功能与架构](docs/FEATURES.md) | 模块分工、执行链路、已实现能力及依赖条件 |
| [不完善之处与路线图](docs/LIMITATIONS.md) | 已知问题、实验功能、待验证项目及改进优先级 |
| [Orange Pi 终端说明](docs/TERMINAL.md) | 配置、设备协议、端口、硬件限制 |
| [本次验证记录](docs/VALIDATION.md) | 本次实际测试与构建结果，区别于历史报告 |
| [参与和反馈](CONTRIBUTING.md) | 如何提意见、复现问题和贡献代码 |
| [来源与许可](THIRD_PARTY_NOTICES.md) | 桌面模块、3D 素材及依赖署名 |

## 能做什么

| 能力 | 当前情况 |
|---|---|
| 文字聊天、一次性提问 | 已有 CLI 与桌面桥接入口，需配置可用模型 |
| 长期记忆与知识导入 | SQLite 存储、FTS 检索、可选语义检索；导入文本不是训练模型权重 |
| 语音对话 | 本地 ASR、流式回复、Edge/Piper TTS；需音频设备与模型，真实声学效果待验证 |
| 电脑操作 | Windows 窗口、截图、键鼠、剪贴板等工具，效果取决于应用与权限 |
| 联网信息与附近地点 | 搜索、天气、网页读取、高德附近查询，依赖网络或对应服务配置 |
| 浏览器交互 | 可选 Playwright 后端，需安装 browser extra 与 Chromium |
| 看图与屏幕理解 | 需额外配置视觉模型；普通文本模型不自动具备视觉能力 |
| 任务管理与扩展 | 持久任务、取消/继续、审批、预算、MCP 与插件接口 |
| Orange Pi 语音终端 | PTT 录音、WebSocket、LED、音频播放实现；实机验收、OLED、完整离线对话待完善 |

这些是代码中的能力入口，不是对所有设备、应用和模型的效果保证。尤其是音乐控制、长任务和语音打断，请结合[限制说明](docs/LIMITATIONS.md)评估。

## 最小启动：先跑文字版

要求 Python **3.11–3.13**。以下为 Windows PowerShell：

```powershell
git clone https://github.com/xuhaoranbb-boop/lisa-ai-agent.git
cd lisa-ai-agent
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

在 `.env` 配置自己的 OpenAI-compatible 模型地址、模型名和密钥。默认示例使用本机 Ollama 的 `qwen3:8b`，需要先安装 Ollama 并执行 `ollama pull qwen3:8b`，确保服务已启动。

```powershell
.\.venv\Scripts\jarvis.exe doctor
.\.venv\Scripts\jarvis.exe ask "你好，丽萨，请介绍一下你自己"
.\.venv\Scripts\jarvis.exe chat
```

文字版跑通后，再按[说明书](docs/USER_GUIDE.md)配置语音和 Electron 桌面。此仓库提供源码；本次没有发布经过签名和干净机器验收的安装包。模型权重、个人数据与 API 密钥也不随源码分发。

## 本地优先的含义

记忆和任务数据默认保存在本机 `~/.jarvis`。使用云端模型时，发送给模型的对话、工具结果及选用的视觉内容会经过服务商；Edge TTS、联网搜索和天气也依赖网络。本地优先不等于所有数据始终离线。完整离线体验需要自行部署兼容的本地模型和语音资源。

## 项目结构

```text
src/jarvis/          Python 智能体、工具、语音桥接、任务和记忆
ashley-upstream/    Electron + TypeScript 桌面界面
terminal/           Orange Pi 实验语音客户端
tests/              Python 回归测试
scripts/            检查与开发辅助脚本
training/           可选 LoRA 实验脚本，不含权重和训练产物
personas/           示例人格配置
docs/               公开版说明、限制和验证记录
```

## 想听听大家的建议

这是一个在实际使用中逐步搭建的项目，距离成熟产品还有不少差距。特别希望得到大家对中文语音打断、桌面操作可靠性、记忆质量、模型路由、硬件适配和首次安装体验的建议。批评、使用体验、改进思路和代码贡献都很欢迎。

- 发现问题：[提交 Issue](https://github.com/xuhaoranbb-boop/lisa-ai-agent/issues/new/choose)
- 交流想法：[参与 Discussions](https://github.com/xuhaoranbb-boop/lisa-ai-agent/discussions)
- 提交代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可

代码采用 [MIT License](LICENSE)，第三方原有声明保留。3D 头盔素材另遵循 CC BY 4.0，详细来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
