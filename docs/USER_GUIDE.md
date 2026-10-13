# 丽萨 AI 使用说明书

适用范围：2026-10-14 公开源码预览。命令以仓库根目录的 Windows PowerShell 为例；桌面自动化主要面向 Windows。先完成文字版最小启动，再逐步启用其他能力。

## 1. 环境和安装

Python 支持 `>=3.11,<3.14`，建议 3.12。文字版只需 Python 和模型端点。桌面还需 Node.js 和 pnpm；本次开发机验证使用 Node.js 24 与 pnpm 11。语音需要麦克风、扬声器和模型下载空间。CPU 模式可运行，但延迟随硬件变化，不设统一内存/延迟承诺。

```powershell
git clone https://github.com/xuhaoranbb-boop/lisa-ai-agent.git
cd lisa-ai-agent
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

已有 `.env` 时先备份再编辑。不要用示例覆盖自己的配置。此步骤不下载 LLM 权重。使用本地 Ollama 时另外安装 Ollama，执行 `ollama pull qwen3:8b` 并确保其服务监听默认端口。

## 2. 配置模型

`.env` 最小配置：

```dotenv
JARVIS_ASSISTANT_NAME=丽萨
JARVIS_MODEL=qwen3:8b
JARVIS_MODEL_BASE_URL=http://127.0.0.1:11434/v1
JARVIS_MODEL_API_KEY=ollama
JARVIS_REASONING_EFFORT=
JARVIS_DATA_DIR=~/.jarvis
JARVIS_WORKSPACE=.
JARVIS_TIMEZONE=Asia/Shanghai
```

云端服务替换模型名、base URL 和 API key。选择支持工具调用的模型，服务地址以供应商提供的 OpenAI-compatible 接口为准；不支持 reasoning_effort 的服务保留空值。文本聊天能正常回答，不代表服务支持全部工具和视觉接口。

| 配置 | 用途 |
|---|---|
| `JARVIS_VOICE_MODEL` / `JARVIS_REASONING_MODEL` / `JARVIS_CODE_MODEL` | 不同任务的模型选择 |
| `JARVIS_VISION_MODEL` / `JARVIS_VISION_BASE_URL` / `JARVIS_VISION_API_KEY` | 可选视觉服务 |
| `JARVIS_EMBEDDING_MODEL` | 可选语义检索；未设置时使用文本检索 |
| `JARVIS_MODEL_FALLBACKS` 等 | 配置备用模型，备用服务也需可访问 |
| `JARVIS_BRIDGE_TOKEN` | 桥接接口鉴权；桌面/硬件客户端需一致 |
| `JARVIS_AMAP_API_KEY` | 高德附近地点查询的可选配置 |
| `JARVIS_PERSONA_MODE` | butler / focused / brief / companion / alert |
| `JARVIS_MCP_SERVERS` | JSON 数组形式的 MCP 服务配置 |

完整可配置项见 `.env.example` 和 `src/jarvis/config.py`。密钥仅放在自己的本地配置中。

## 3. 检查和文字使用

```powershell
.\.venv\Scripts\jarvis.exe --help
.\.venv\Scripts\jarvis.exe doctor
.\.venv\Scripts\jarvis.exe ask "你好，丽萨"
.\.venv\Scripts\jarvis.exe chat
```

`doctor` 检查模型连接、存储及可选语音依赖。纯文字安装时语音依赖缺失是预期情况；模型连接失败先修正服务状态、URL、模型名或密钥。

示例：询问天气、总结文本、读取工作区内文件。首次使用桌面动作时先选一个简单且可观察的动作，如打开记事本，再测试复杂流程。

## 4. 记忆、知识和任务

```powershell
.\.venv\Scripts\jarvis.exe memory list
.\.venv\Scripts\jarvis.exe memory search "偏好"
.\.venv\Scripts\jarvis.exe train README.md
.\.venv\Scripts\jarvis.exe task list
.\.venv\Scripts\jarvis.exe task show TASK_ID
.\.venv\Scripts\jarvis.exe task cancel TASK_ID
.\.venv\Scripts\jarvis.exe task continue TASK_ID
.\.venv\Scripts\jarvis.exe persona show
.\.venv\Scripts\jarvis.exe persona import personas/educated_butler.json
.\.venv\Scripts\jarvis.exe permission show
.\.venv\Scripts\jarvis.exe mcp list
```

`TASK_ID` 替换为任务列表中的 ID。`train` 把文本切块写入知识库，属于检索知识导入；`training/` 中的 LoRA 脚本才涉及权重训练，两者不同。

默认数据在用户目录 `.jarvis` 下，由 `JARVIS_DATA_DIR` 决定。迁移前关闭相关进程，备份整个数据目录和本地配置；备份中可能包含个人记录，请自行保管。删除记忆前可通过 `memory --help` 查看具体命令和确认行为。

## 5. 语音

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[voice]"
.\.venv\Scripts\jarvis.exe voice
```

第一次使用可能下载 Whisper/Piper 资源，部分 ASR 后端需要预先准备模型目录。默认 ASR 会按配置顺序选择可用提供者。先使用 `JARVIS_WHISPER_DEVICE=cpu`、`JARVIS_WHISPER_COMPUTE_TYPE=int8`；CUDA 模式需自行匹配运行库。

默认 TTS 是 Edge，依赖网络；选 Piper 时设置 `JARVIS_TTS_BACKEND=piper` 并准备对应资源。声音、语速见 `JARVIS_TTS_VOICE`、`JARVIS_TTS_RATE`。旧私人说明中的「晓伊」不是公开配置的固定默认值。

桌面预置唤醒模型仍为 **Hey Jarvis**，中文「丽萨」唤醒与个人声纹尚未完成；仓库不包含个人录音或声纹。音频打断需要真实环境测试，耳机通常更容易排查回声问题。

## 6. Electron 桌面

桌面本地语音服务使用 Python bridge。在一个终端运行：

```powershell
.\.venv\Scripts\jarvis.exe bridge --host 127.0.0.1 --port 8765
```

另一个终端进入桌面目录，配置本地 provider 和桥接地址：

```powershell
cd ashley-upstream
Copy-Item .env.example .env
# 编辑桌面 .env，将 JARVIS_VOICE_PROVIDER 设为 local
# JARVIS_BRIDGE_URL=http://127.0.0.1:8765
# 若后端设了 JARVIS_BRIDGE_TOKEN，桌面也填相同值
pnpm install --frozen-lockfile
pnpm run build
pnpm run dev
```

根目录 `.env` 与桌面目录 `.env` 是两份配置：后端填模型配置，桌面填语音 provider/bridge 配置。桌面也提供其他语音 provider 接口，对应服务需自己的配置与凭据。

`Ctrl+Shift+J` 可显示/隐藏窗口，`Ctrl+Shift+Space` 可唤起语音。关闭窗口可能只是隐藏到托盘；彻底退出使用托盘菜单。配置和平台差异以实际界面为准。

根目录 `start-jarvis.ps1` 可启动本地 bridge 和 Electron，但会尝试启动机器相关的 LoRA 服务。首次运行建议手动启动；使用该脚本前设置 `$env:JARVIS_LOCAL_MODEL_AUTOSTART='0'`，并先完成依赖安装和桌面构建。

## 7. 浏览器、视觉与权限

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[browser]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

浏览器配置见 `JARVIS_BROWSER_BACKEND`，交互后端还需浏览器资源。视觉工具需要图像兼容模型；配置后可用 `jarvis vision analyze --help` 查看接口。

审批根据风险等级和 `JARVIS_PERMISSION_*` 配置决定，默认策略会自动放行多个等级；不是每个动作都弹确认。消息收件人默认白名单为「文件传输助手」。用 `jarvis permission show` 查看自己的有效策略。操作可靠性受窗口状态、应用更新和模型判断影响。

## 8. 排障

| 现象 | 优先检查 |
|---|---|
| 模型无响应 | `doctor`、服务进程、URL、模型是否已下载、鉴权 |
| 工具调用失败 | 模型工具能力、有效权限、工作区和依赖 |
| 桌面连接失败 | bridge 是否监听 8765，两份配置的 URL/token 是否一致 |
| 有文字无声音 | TTS 服务、网络、资源目录、音量与输出设备 |
| 唤醒无反应 | 使用 Hey Jarvis、麦克风权限、输入设备和噪声 |
| 浏览器交互失败 | browser extra、Chromium、选定后端 |
| 文档解析缺模块 | 格式支持是基础提取；PDF 的 pypdf 尚未声明为项目依赖 |
| LoRA 启动失败 | 示例仍含机器相关路径，适配后再启用 |

日志位置随启动方式变化，启动脚本与辅助工具可能使用 `.jarvis-run`，运行数据目录也有生产日志。反馈时提交错误片段、版本、命令和复现步骤，并删除密钥、聊天内容和私人路径。详见 [反馈指南](../CONTRIBUTING.md)。
