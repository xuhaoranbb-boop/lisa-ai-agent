# 本次公开源码验证记录

日期：2026-10-14。环境：Windows，Python 3.12 开发环境、Node.js `v24.21.0`、pnpm `11.7.0`。检查针对本次公开副本；Python 借用已有项目的虚拟环境执行，尚未完成干净机器上的后端安装验收。下面的 `python` 表示该开发虚拟环境解释器，工作目录为公开副本根目录。

| 命令 / 检查 | 实际结果 | 退出状态 |
|---|---|---|
| `python -m pytest -q` | `128 passed, 2 warnings in 36.35s` | 0 |
| `python -m ruff check src tests` | `Found 9 errors.` | 1 |
| `python -m compileall -q src terminal` | 无语法错误输出 | 0 |
| `PYTHONPATH=src python -m jarvis --help`（PowerShell 等价设置） | CLI 帮助正常展示 | 0 |
| 桌面 `pnpm install --frozen-lockfile` | 安装 406 个包，未改变锁文件 | 0 |
| 桌面 `pnpm run build` | 主进程 TypeScript、renderer 类型检查和 bundle 完成 | 0 |
| 桌面 `pnpm test` | spoken-hide/music/conflicts 三组通过；`runtime panel model tests passed` | 0 |

两个 Python 警告来自 pipecat 依赖：audioop 弃用、AudioContextTTSService 弃用。Python 3.13 支持范围仅为项目声明，本次实际运行版本是 3.12。

## 尚存的静态检查问题

9 项诊断分布在 bridge/device_channel 等代码，类型包括 I001、UP035、F841、E501、UP041，即导入排序、Callable 来源、未使用变量、长行和 TimeoutError 写法。此次以源码发布和文档整理为目标，没有顺带大范围修改执行逻辑。不能将本次状态描述成全部检查通过。

## 原工作目录的异常

发布前在原项目运行全量 pytest，出现 `Windows fatal exception: code 0xc000070a`，命令退出 1，输出包含执行引擎初始化的堆栈。之后在公开副本使用相同虚拟环境执行全量测试完成并得到 128 passed。尚未查明原生异常根因；单次成功不代表该异常已经修复。

## 发布检查

- 公共文档相对链接逐项检查。
- Git 候选文件排除运行 `.env`、日志、聊天/数据库、私人训练与声纹、依赖目录及无关项目。
- 文本执行凭据模式检查；测试文件中的 `sk-deadbeef...` 为明示的脱敏测试夹具，按精确值排除。
- 打包脚本使用最小隔离夹具实际执行：即使项目存在测试 `.env`，产物只携带 `.env.example`，不携带运行 `.env` 或夹具密钥。

上述发布检查结果在发布前由维护者实际运行确认。模式检查不等同于全面机密识别；公开副本采用明确文件清单从源项目提取，并未复制源目录的私人状态。

## 本次未验证的内容

云模型的真实计费请求、全链路聊天、麦克风声学表现、真实桌面操作、Orange Pi GPIO/SPI/音频、安装包签名与安装、自动更新、长期稳定运行及 macOS/Linux 桌面完整能力。这里的测试结果只证明对应测试和构建完成，不代替实机端到端验收。
