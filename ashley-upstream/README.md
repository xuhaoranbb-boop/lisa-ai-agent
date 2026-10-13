# 丽萨桌面模块（历史目录名 Ashley）

这是丽萨 AI 的 Electron / TypeScript 界面。当前公开项目主要面向 Windows，历史接口和命名保留。安装、配置、能力边界及验证结果统一以根目录 [README](../README.md) 和 [使用说明](../docs/USER_GUIDE.md) 为准。

```powershell
pnpm install --frozen-lockfile
pnpm run build
pnpm test
pnpm run dev
```

运行前配置 `.env` 中的本地语音 provider、bridge 地址和对应 token，并启动 Python bridge。预置唤醒词为 **Hey Jarvis**；个人声学模板属于实验路径，不能等同于完成的中文唤醒或声纹模型。

Windows 打包命令是 `pnpm run package:win`，会复制运行库与模型缓存，体积可能很大。本次只验证源码构建和测试，没有发布安装包。公开打包脚本只携带 `.env.example`，请用户自行配置凭据。

## 原有署名

历史桌面 README 记载由抖音 [@陆叁昧](https://www.douyin.com/user/601786286)（ID `601786286`）展示开发过程。本次保留署名，不将第三方桌面工作宣称为独立原创。

## Credits

- Model: [Sci-Fi Helmet - High Poly - Ngchipv](https://sketchfab.com/3d-models/sci-fi-helmet-high-poly-ngchipv-2f7218f88b94455cb69411e4069dc3b9)
- Author: [HiepVu](https://sketchfab.com/ngchipv)
- License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- 原有加工说明：由 240,984 三角形减面至约 40,000，采用程序化 PBR 材质与青色眼部发光。该说明沿用本地原 README，本次未重新测量。

代码原有 [MIT License](LICENSE) 保留；头盔素材另遵循 CC BY 4.0。
