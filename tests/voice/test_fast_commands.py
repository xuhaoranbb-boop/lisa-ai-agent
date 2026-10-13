from jarvis.fast_commands import FastCommandRouter, normalize_fast_command


def test_normalize_fast_command_strips_punctuation_and_prefixes() -> None:
    assert normalize_fast_command("  帮我打开浏览器！ ") == "打开浏览器"
    assert normalize_fast_command("刘览器。") == "浏览器"
    assert normalize_fast_command("截屏") == "截图"


def test_fast_router_opens_common_apps() -> None:
    router = FastCommandRouter()

    browser = router.route("帮我打开浏览器")
    wechat = router.route("打开微信")
    vscode = router.route("启动 VS Code")

    assert browser is not None
    assert browser.tool == "computer_open_app"
    assert browser.arguments == {"application": "浏览器"}
    assert wechat is not None
    assert wechat.arguments == {"application": "微信"}
    assert vscode is not None
    assert vscode.arguments == {"application": "VS Code"}


def test_fast_router_handles_desktop_shortcuts() -> None:
    router = FastCommandRouter()

    screenshot = router.route("帮我截图")
    close_window = router.route("关闭当前窗口")
    show_desktop = router.route("显示桌面")

    assert screenshot is not None
    assert screenshot.tool == "computer_screenshot"
    assert close_window is not None
    assert close_window.arguments == {"keys": ["alt", "f4"]}
    assert show_desktop is not None
    assert show_desktop.arguments == {"keys": ["win", "d"]}


def test_fast_router_opens_extended_apps() -> None:
    router = FastCommandRouter()

    netease = router.route("打开网易云音乐")
    dingtalk = router.route("钉钉")
    wps = router.route("打开WPS")
    feishu = router.route("启动飞书")

    assert netease is not None
    assert netease.arguments == {"application": "网易云音乐"}
    assert dingtalk is not None
    assert dingtalk.arguments == {"application": "钉钉"}
    assert wps is not None
    assert wps.arguments == {"application": "WPS Office"}
    assert feishu is not None
    assert feishu.arguments == {"application": "飞书"}


def test_fast_router_cancels_without_llm() -> None:
    match = FastCommandRouter().route("取消刚才的任务")

    assert match is not None
    assert match.category == "cancel"
    assert match.tool is None


def test_fast_router_answers_time_without_llm() -> None:
    router = FastCommandRouter()

    for command in ("现在几点", "今天几号", "今天星期几"):
        match = router.route(command)
        assert match is not None
        assert match.category == "utility"
        assert match.tool is None
        assert "现在是" in match.reply


def test_fast_router_leaves_complex_requests_to_agent() -> None:
    router = FastCommandRouter()

    assert router.route("帮我搜索一下今天的天气") is None
    assert router.route("打开浏览器然后搜索 DeepSeek") is None
    assert router.route("今天心情不太好") is None
