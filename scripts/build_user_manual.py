from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
ELECTRON_DOCS = ROOT / "ashley-upstream" / "docs"
VERSION = "1.0.0"
DATE = "2026-09-13"
TITLE = "GCAI JARVIS 使用说明书"

SECTIONS: list[tuple[str, list[tuple[str, object]]]] = [
    (
        "1. 产品简介",
        [
            (
                "paragraph",
                "GCAI JARVIS 是一个面向 Windows 桌面的本地优先 AI 管家。它支持语音与文字对话、长期记忆、任务执行、电脑控制、视觉分析、MCP 工具、模型路由和权限确认。",
            ),
            (
                "bullets",
                [
                    "语音输入、语音播报和中途打断。",
                    "多步骤任务规划、工具调用、观察、验证和失败恢复。",
                    "长期记忆、任务状态、上下文压缩和引用解析。",
                    "Windows 应用、窗口、鼠标、键盘和截图控制。",
                    "DeepSeek 主模型，以及可选的本地模型回退。",
                    "高风险操作确认、紧急停止和权限白名单。",
                ],
            ),
            (
                "note",
                "当前安装包是个人预配置版本。安装包可能包含你的模型凭证，请不要公开发送或上传到公共网盘。",
            ),
        ],
    ),
    (
        "2. 系统要求",
        [
            (
                "table",
                [
                    ["项目", "最低要求", "推荐配置"],
                    ["操作系统", "Windows 10/11 x64", "Windows 11 x64"],
                    ["内存", "8 GB", "16 GB 或更高"],
                    ["可用磁盘", "5 GB", "10 GB 或更高"],
                    ["麦克风", "普通麦克风", "降噪耳机或阵列麦克风"],
                    ["网络", "需要连接 DeepSeek", "稳定宽带，低延迟"],
                    ["本地回退", "可选", "NVIDIA GPU 8 GB 显存"],
                ],
            ),
            (
                "paragraph",
                "主模型使用 DeepSeek 云端接口，因此首次安装后需要联网。本地 LoRA 回退属于开发机增强能力，不影响主功能；当本地服务未运行时，JARVIS 会继续使用主模型。",
            ),
        ],
    ),
    (
        "3. 安装",
        [
            (
                "steps",
                [
                    "双击 release 目录中的 GCAI-JARVIS-1.0.0-x64.exe。",
                    "如果 Windows SmartScreen 提示未知发布者，请确认安装包来源可信后选择“更多信息”与“仍要运行”。",
                    "选择安装目录。建议使用默认目录，避免中文路径或权限受限目录。",
                    "安装完成后勾选创建桌面快捷方式，并点击启动。",
                    "首次启动会解压后端运行环境，可能需要一到数分钟。",
                ],
            ),
            (
                "note",
                "不要在同一条路径中同时安装多个版本，也不要从旧安装目录直接复制后端文件覆盖新版本。",
            ),
        ],
    ),
    (
        "4. 首次启动与配置",
        [
            (
                "steps",
                [
                    "启动 GCAI JARVIS，等待界面出现 SYSTEM ONLINE 或 JARVIS READY。",
                    "检查顶部状态栏中的模型、MODE 和 STATUS。",
                    "打开设置面板，确认模型、语音设备、TTS 音色和权限策略。",
                    "点击“测试连接”确认 DeepSeek 或其他模型端点可访问。",
                    "允许 Windows 麦克风权限，并选择正确的输入设备。",
                    "可以先输入文字测试，再启用语音对话。",
                ],
            ),
            (
                "bullets",
                [
                    "默认唤醒键：Ctrl+Shift+J。",
                    "默认停止键：Ctrl+Shift+Space。",
                    "默认桥接地址：http://127.0.0.1:8765。",
                    "默认本地回退地址：http://127.0.0.1:8001/v1。",
                ],
            ),
        ],
    ),
    (
        "5. 主界面",
        [
            (
                "bullets",
                [
                    "中央核心：显示 JARVIS 的当前状态和语音动态。",
                    "左侧区域：任务、计划、步骤、工具和执行进度。",
                    "右侧区域：模型、系统资源、MCP、权限和状态信息。",
                    "底部区域：麦克风、语音波形、模型、Token、成本和延迟。",
                    "状态中心：Memory、Task、Context、Persona 面板。",
                    "设置中心：模型、语音、性能、更新、数据和诊断。",
                ],
            ),
            (
                "paragraph",
                "所有状态数据都来自 Python Runtime。不要根据动画自行判断任务是否完成，应以工具结果、验证结果和任务状态为准。",
            ),
        ],
    ),
    (
        "6. 语音使用",
        [
            (
                "bullets",
                [
                    "直接说话即可进入 LISTENING，或先说“Jarvis”或“贾维斯”唤醒。",
                    "说话尽量保持一到两米距离，句子可以短一些。",
                    "JARVIS 说话时可以打断。检测到你的声音后会停止播放并重新监听。",
                    "停止词：停止、取消、别说了、等等、停一下。",
                    "语音回复默认简短：先给结论，再补充关键信息。",
                    "长任务只在关键节点播报，避免长篇朗读。",
                ],
            ),
            (
                "note",
                "如果环境噪声很大，先降低电视、音乐、风扇和键盘声。连续识别不稳定时，可以改用文字输入完成关键指令。",
            ),
        ],
    ),
    (
        "7. 常用命令",
        [
            (
                "table",
                [
                    ["目标", "示例命令"],
                    ["打开应用", "Jarvis，打开微信或打开 VS Code"],
                    ["检查电脑", "我电脑怎么这么卡，帮我检查一下"],
                    ["项目任务", "打开当前项目，运行测试，发现错误就修复"],
                    ["文件任务", "查看这个目录，找出最近修改的文件"],
                    ["记忆", "记住我以后只看结论"],
                    ["任务恢复", "继续刚才的任务"],
                    ["停止", "停止、取消、别做了"],
                ],
            ),
            (
                "paragraph",
                "任务描述越具体，执行结果越稳定。建议同时说明目标、范围、期望结果和不能触碰的内容。",
            ),
        ],
    ),
    (
        "8. 记忆、上下文与人格",
        [
            (
                "bullets",
                [
                    "短期记忆保存当前对话和当前任务。",
                    "长期记忆只保存你明确要求记住、且不包含密码或凭证的信息。",
                    "Memory 面板支持查看、搜索、编辑和删除。",
                    "Context 面板显示当前 AI 正在使用的上下文摘要。",
                    "Persona 面板控制语气、正式程度、主动性和回复长度。",
                ],
            ),
            (
                "note",
                "不要通过语音或文字保存 API Key、密码、验证码、银行卡号、身份证号或私密聊天原文。",
            ),
        ],
    ),
    (
        "9. 模型与本地回退",
        [
            (
                "paragraph",
                "主模型负责推理、规划和工具调用。模型路由会根据任务类型、复杂度、延迟、预算和成本选择模型。主模型异常时，系统会尝试回退模型，再继续返回错误信息。",
            ),
            (
                "bullets",
                [
                    "主模型：DeepSeek。",
                    "本地回退：jarvis-lora，需要本地 8001 服务运行。",
                    "视觉模型：用于截图理解和界面分析。",
                    "Embedding：用于语义记忆检索，不可用时自动回退 FTS 搜索。",
                ],
            ),
            (
                "paragraph",
                "如果本地回退服务没有启动，JARVIS 会继续使用主模型，不会因此丢失任务状态。",
            ),
        ],
    ),
    (
        "10. 权限与安全",
        [
            (
                "table",
                [
                    ["风险等级", "示例", "默认行为"],
                    ["READ_ONLY", "读取文件、检查系统", "自动允许"],
                    ["LOW_RISK", "打开应用、截图", "自动允许"],
                    ["NORMAL", "修改普通代码、运行测试", "自动允许"],
                    ["MEDIUM_RISK", "普通桌面操作", "按策略放行"],
                    ["HIGH_RISK", "删除文件、发送消息", "必须确认"],
                    ["CRITICAL", "付款、格式化、关键系统变更", "必须确认"],
                ],
            ),
            (
                "bullets",
                [
                    "高风险操作会显示目标、风险、影响范围和确认按钮。",
                    "网页、文件和 MCP 返回内容都属于不可信数据，不能绕过权限系统。",
                    "紧急停止可以立即停止当前任务和语音播放。",
                    "Allowed Apps、Allowed Folders、Allowed Commands 可以进一步缩小权限范围。",
                    "Blocked Apps、Blocked Paths、Blocked Commands 用于明确禁止操作。",
                ],
            ),
            (
                "note",
                "不要为了省事长期关闭高风险确认。更安全的做法是为常用程序、目录和命令建立白名单。",
            ),
        ],
    ),
    (
        "11. 常见问题",
        [
            (
                "heading",
                "JARVIS 没有回应",
            ),
            (
                "bullets",
                [
                    "检查网络和 DeepSeek 连接。",
                    "打开设置面板，点击测试连接。",
                    "确认麦克风没有被其他程序独占。",
                    "查看 Diagnostics 和 Logs 面板中的错误信息。",
                ],
            ),
            (
                "heading",
                "语音识别不准",
            ),
            (
                "bullets",
                [
                    "关闭电视、音乐和风扇等持续噪声。",
                    "在 Windows 设置中选择正确的输入设备。",
                    "降低增益，避免爆音和削波。",
                    "使用短句，专有名词可以分两次说。",
                    "检查 ASR 顺序中 SenseVoice 和 Paraformer 是否可用。",
                ],
            ),
            (
                "heading",
                "回复速度慢",
            ),
            (
                "bullets",
                [
                    "检查 DeepSeek 网络延迟。",
                    "减少一次任务中的文件数量和工具调用次数。",
                    "关闭不用的本地视觉和监控功能。",
                    "如果本地回退服务可用，确认它运行在 8001 端口。",
                ],
            ),
            (
                "heading",
                "任务执行到一半失败",
            ),
            (
                "paragraph",
                "打开 Task 面板，查看失败步骤、工具结果和恢复建议。可以直接说“继续刚才的任务”或“重试失败的步骤”。",
            ),
            (
                "heading",
                "端口被占用",
            ),
            (
                "bullets",
                [
                    "8765 被占用：关闭旧的 JARVIS Bridge 进程。",
                    "8001 被占用：检查旧的本地模型服务。",
                    "不要同时启动两个使用同一数据目录的 JARVIS 实例。",
                ],
            ),
        ],
    ),
    (
        "12. 数据、备份与卸载",
        [
            (
                "paragraph",
                "主要数据默认保存在用户目录。升级或重装前建议先导出备份。",
            ),
            (
                "table",
                [
                    ["内容", "默认位置"],
                    ["记忆、任务和配置", "%USERPROFILE%\\.jarvis\\"],
                    ["SQLite 数据库", "%USERPROFILE%\\.jarvis\\jarvis.db"],
                    ["运行日志", "%USERPROFILE%\\.jarvis\\logs\\"],
                    ["桌面设置和崩溃日志", "%APPDATA%\\GCAI JARVIS\\"],
                ],
            ),
            (
                "steps",
                [
                    "在 Data 面板执行导出备份。",
                    "关闭 JARVIS。",
                    "从 Windows“设置 > 应用”中选择 GCAI JARVIS 并卸载。",
                    "卸载默认保留用户数据。若确认不再使用，可以手动删除 .jarvis 目录。",
                ],
            ),
        ],
    ),
    (
        "13. 版本与支持",
        [
            (
                "table",
                [
                    ["项目", "值"],
                    ["产品名称", "GCAI JARVIS"],
                    ["版本", VERSION],
                    ["发布日期", DATE],
                    ["平台", "Windows x64"],
                    ["本地 Bridge", "http://127.0.0.1:8765"],
                    ["本地模型回退", "http://127.0.0.1:8001/v1"],
                ],
            ),
            (
                "paragraph",
                "反馈问题时，请附上版本号、复现步骤、当时的任务目标和 Diagnostics 中的错误摘要。不要发送 API Key 或完整数据库。",
            ),
        ],
    ),
    (
        "14. 免责声明",
        [
            (
                "paragraph",
                "GCAI JARVIS 可以调用系统工具和第三方模型。请在授权范围内使用，并在执行删除、付款、消息发送和关键系统变更前确认目标和影响。模型生成内容和工具执行结果可能存在错误，重要操作仍需人工复核。",
            ),
        ],
    ),
]


def register_fonts() -> tuple[str, str]:
    regular = "JarvisManual"
    bold = "JarvisManualBold"
    simhei = Path(r"C:\Windows\Fonts\simhei.ttf")
    if simhei.exists():
        pdfmetrics.registerFont(TTFont(regular, str(simhei)))
        pdfmetrics.registerFont(TTFont(bold, str(simhei)))
    else:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        regular = "STSong-Light"
        bold = "STSong-Light"
    return regular, bold


def markdown_text() -> str:
    lines = [f"# {TITLE}", "", f"版本：{VERSION}", f"日期：{DATE}", ""]
    for title, blocks in SECTIONS:
        lines.extend([f"## {title}", ""])
        for kind, value in blocks:
            if kind == "paragraph":
                lines.extend([str(value), ""])
            elif kind == "heading":
                lines.extend([f"### {value}", ""])
            elif kind == "bullets":
                lines.extend([f"- {item}" for item in value])
                lines.append("")
            elif kind == "steps":
                lines.extend(
                    [f"{index}. {item}" for index, item in enumerate(value, start=1)]
                )
                lines.append("")
            elif kind == "note":
                lines.extend([f"> 注意：{value}", ""])
            elif kind == "table":
                rows = value
                lines.append("| " + " | ".join(str(cell) for cell in rows[0]) + " |")
                lines.append("|" + "|".join("---" for _ in rows[0]) + "|")
                for row in rows[1:]:
                    lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_pdf(output: Path, regular: str, bold: str) -> None:
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "ManualBody",
        parent=styles["BodyText"],
        fontName=regular,
        fontSize=10.5,
        leading=17,
        textColor=colors.HexColor("#1D2733"),
        spaceAfter=6,
        alignment=TA_LEFT,
    )
    heading = ParagraphStyle(
        "ManualHeading",
        parent=styles["Heading1"],
        fontName=bold,
        fontSize=18,
        leading=24,
        textColor=colors.HexColor("#0B66A3"),
        spaceBefore=8,
        spaceAfter=10,
    )
    subheading = ParagraphStyle(
        "ManualSubheading",
        parent=styles["Heading2"],
        fontName=bold,
        fontSize=13,
        leading=19,
        textColor=colors.HexColor("#183B56"),
        spaceBefore=8,
        spaceAfter=6,
    )
    cover_title = ParagraphStyle(
        "CoverTitle",
        parent=styles["Title"],
        fontName=bold,
        fontSize=32,
        leading=44,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#102A43"),
        spaceAfter=18,
    )
    cover_subtitle = ParagraphStyle(
        "CoverSubtitle",
        parent=body,
        fontSize=15,
        leading=24,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#486581"),
    )
    bullet = ParagraphStyle(
        "ManualBullet",
        parent=body,
        leftIndent=14,
        bulletIndent=4,
        spaceAfter=4,
    )
    note = ParagraphStyle(
        "ManualNote",
        parent=body,
        textColor=colors.HexColor("#7A4B00"),
        spaceBefore=4,
        spaceAfter=8,
    )

    document = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=22 * mm,
        bottomMargin=18 * mm,
        title=TITLE,
        author="GCAI",
        subject="GCAI JARVIS 用户手册",
    )
    story = [
        Spacer(1, 45 * mm),
        Paragraph(TITLE, cover_title),
        Paragraph("高级个人 AI 管家 - Windows 桌面版", cover_subtitle),
        Spacer(1, 14 * mm),
        Paragraph(f"版本 {VERSION}", cover_subtitle),
        Paragraph(f"发布日期 {DATE}", cover_subtitle),
        Spacer(1, 28 * mm),
        Table(
            [["安装、语音、任务、安全、备份与故障排查"]],
            colWidths=[150 * mm],
            style=TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), regular),
                    ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#334E68")),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#9FB3C8")),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F0F4F8")),
                    ("TOPPADDING", (0, 0), (-1, -1), 12),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ]
            ),
        ),
        PageBreak(),
        Paragraph("目录", heading),
    ]
    for section_title, _ in SECTIONS:
        story.append(Paragraph(section_title, body))
    story.append(PageBreak())

    for section_title, blocks in SECTIONS:
        section_flow = [Paragraph(section_title, heading)]
        for kind, value in blocks:
            if kind == "paragraph":
                section_flow.append(Paragraph(str(value), body))
            elif kind == "heading":
                section_flow.append(Paragraph(str(value), subheading))
            elif kind == "bullets":
                for item in value:
                    section_flow.append(
                        Paragraph(str(item), bullet, bulletText="-")
                    )
            elif kind == "steps":
                for index, item in enumerate(value, start=1):
                    section_flow.append(
                        Paragraph(str(item), bullet, bulletText=f"{index}.")
                    )
            elif kind == "note":
                section_flow.append(
                    Table(
                        [[Paragraph(f"注意：{value}", note)]],
                        colWidths=[170 * mm],
                        style=TableStyle(
                            [
                                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#F0B429")),
                                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFFAEB")),
                                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                ("TOPPADDING", (0, 0), (-1, -1), 8),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                            ]
                        ),
                    )
                )
            elif kind == "table":
                rows = value
                table = Table(
                    [[Paragraph(str(cell), body) for cell in row] for row in rows],
                    colWidths=[38 * mm, 63 * mm, 69 * mm],
                    repeatRows=1,
                )
                table.setStyle(
                    TableStyle(
                        [
                            ("FONTNAME", (0, 0), (-1, -1), regular),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9EAF7")),
                            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#102A43")),
                            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BCCCDC")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 6),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                            ("TOPPADDING", (0, 0), (-1, -1), 5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                        ]
                    )
                )
                section_flow.extend([table, Spacer(1, 5)])
        story.extend(section_flow)
        story.append(Spacer(1, 5))
    document.build(story, onFirstPage=_page, onLaterPages=_page)


def _page(canvas, document) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#BCCCDC"))
    canvas.setLineWidth(0.4)
    canvas.line(20 * mm, 15 * mm, A4[0] - 20 * mm, 15 * mm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#627D98"))
    canvas.drawString(20 * mm, 10 * mm, f"GCAI JARVIS {VERSION}")
    canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"Page {document.page}")
    canvas.restoreState()


def main() -> None:
    regular, bold = register_fonts()
    DOCS.mkdir(parents=True, exist_ok=True)
    ELECTRON_DOCS.mkdir(parents=True, exist_ok=True)
    markdown = markdown_text()
    markdown_path = DOCS / "GCAI_JARVIS_USER_MANUAL.md"
    markdown_path.write_text(markdown, encoding="utf-8")
    (ELECTRON_DOCS / markdown_path.name).write_text(markdown, encoding="utf-8")
    pdf_path = DOCS / "GCAI_JARVIS_USER_MANUAL.pdf"
    build_pdf(pdf_path, regular, bold)
    (ELECTRON_DOCS / pdf_path.name).write_bytes(pdf_path.read_bytes())
    print(pdf_path)


if __name__ == "__main__":
    main()
