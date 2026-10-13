from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class FastCommandMatch:
    """A zero-LLM route for a short, unambiguous desktop command."""

    category: str
    tool: str | None
    arguments: dict[str, Any]
    reply: str


_PUNCTUATION = re.compile(r"[\s，。！？!?；;：:、,.]+")
_OPEN_PREFIXES = re.compile(r"^(请|帮我|麻烦|给我|帮我打开|给我打开)+")

_APP_ALIASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("打开浏览器", "启动浏览器", "浏览器"), "浏览器"),
    (("打开chrome", "启动chrome", "chrome"), "Chrome"),
    (("打开edge", "启动edge", "edge"), "Edge"),
    (("打开火狐", "启动火狐", "firefox"), "Firefox"),
    (("打开微信", "启动微信", "微信"), "微信"),
    (("打开qq", "启动qq", "qq"), "QQ"),
    (("打开vscode", "打开code", "启动vscode"), "VS Code"),
    (("打开记事本", "启动记事本", "记事本"), "记事本"),
    (("打开计算器", "启动计算器", "计算器"), "计算器"),
    (("打开文件管理器", "文件资源管理器", "资源管理器"), "文件资源管理器"),
    (("打开网易云音乐", "启动网易云音乐", "网易云音乐", "网易云"), "网易云音乐"),
    (("打开qq音乐", "启动qq音乐", "qq音乐"), "QQ音乐"),
    (("打开钉钉", "启动钉钉", "钉钉"), "钉钉"),
    (("打开飞书", "启动飞书", "飞书"), "飞书"),
    (("打开企业微信", "启动企业微信", "企业微信"), "企业微信"),
    (("打开wps", "启动wps", "wps"), "WPS Office"),
    (("打开抖音", "启动抖音", "抖音"), "抖音"),
    (("打开剪映", "启动剪映", "剪映"), "剪映"),
)

_SCREENSHOT_COMMANDS = {
    "截图",
    "截屏",
    "截个屏",
    "截个图",
    "帮我截图",
    "给我截图",
    "屏幕截图",
}

_CLOSE_WINDOW_COMMANDS = {
    "关闭窗口",
    "关闭当前窗口",
    "关掉窗口",
    "关掉当前窗口",
}

_SHOW_DESKTOP_COMMANDS = {
    "显示桌面",
    "回到桌面",
}

_TIME_COMMANDS = {
    "几点",
    "几点了",
    "现在几点",
    "现在几点了",
    "当前时间",
    "现在时间",
    "时间",
    "报时",
}

_DATE_COMMANDS = {
    "今天几号",
    "今天是几号",
    "今天日期",
    "当前日期",
    "今天什么日子",
}

_WEEKDAY_COMMANDS = {
    "今天星期几",
    "星期几",
    "今天礼拜几",
    "礼拜几",
}

_CANCEL_COMMANDS = {
    "停止",
    "停",
    "停下",
    "停一下",
    "取消",
    "取消任务",
    "取消刚才的任务",
    "别说了",
    "闭嘴",
    "住口",
    "安静",
    "stop",
    "cancel",
}


def normalize_fast_command(text: str) -> str:
    normalized = _PUNCTUATION.sub("", text.strip().lower())
    normalized = _OPEN_PREFIXES.sub("", normalized)
    replacements = {
        "刘览器": "浏览器",
        "流览器": "浏览器",
        "薇信": "微信",
        "威信": "微信",
        "截屏": "截图",
        "关掉窗口": "关闭窗口",
    }
    for heard, intended in replacements.items():
        normalized = normalized.replace(heard, intended)
    return normalized


def _spoken_datetime_reply(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    weekday = "一二三四五六日"[current.weekday()]
    return (
        f"现在是{current.year}年{current.month}月{current.day}日，"
        f"星期{weekday}，{current.hour:02d}点{current.minute:02d}分。"
    )


class FastCommandRouter:
    """Route short desktop commands without paying for a model call.

    The router deliberately handles only commands whose meaning is stable
    across phrasing. Anything ambiguous or multi-step falls through to the
    normal Agent path.
    """

    def route(self, text: str) -> FastCommandMatch | None:
        normalized = normalize_fast_command(text)
        if not normalized:
            return None

        if normalized in _CANCEL_COMMANDS:
            return FastCommandMatch(
                category="cancel",
                tool=None,
                arguments={},
                reply="已取消。",
            )

        if normalized in _SCREENSHOT_COMMANDS:
            return FastCommandMatch(
                category="desktop",
                tool="computer_screenshot",
                arguments={},
                reply="好了，截图完成，保存在 artifacts 文件夹了。",
            )

        if normalized in _CLOSE_WINDOW_COMMANDS:
            return FastCommandMatch(
                category="desktop",
                tool="computer_keyboard_hotkey",
                arguments={"keys": ["alt", "f4"]},
                reply="好的，已发送关闭窗口指令。",
            )

        if normalized in _SHOW_DESKTOP_COMMANDS:
            return FastCommandMatch(
                category="desktop",
                tool="computer_keyboard_hotkey",
                arguments={"keys": ["win", "d"]},
                reply="好的，已显示桌面。",
            )

        if normalized in _TIME_COMMANDS | _DATE_COMMANDS | _WEEKDAY_COMMANDS:
            return FastCommandMatch(
                category="utility",
                tool=None,
                arguments={},
                reply=_spoken_datetime_reply(),
            )

        for aliases, application in _APP_ALIASES:
            if normalized in aliases:
                return FastCommandMatch(
                    category="desktop",
                    tool="computer_open_app",
                    arguments={"application": application},
                    reply=f"好了，已打开{application}。",
                )

        return None
