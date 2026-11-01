# Figure Converter v0.3.0

面向科研插图的本地 macOS 拖拽转换工具。

## 本次更新

- 新增 PDF 和 EPS 输出，与 SVG、EMF、PNG 一起按画板分别导出。
- 五种格式共用裁剪画板留白、自定义毫米宽度和等比例缩放设置。
- 默认仍选择 SVG、EMF、PNG；PDF 和 EPS 按需勾选，可同时导出全部五种格式。
- PDF 使用 Illustrator 原生保存，保留矢量与支持的透明效果；EPS 按格式要求处理透明内容。
- 沿用黑白界面、批量转换、文字转轮廓、描边展开和 300/600/1200 DPI PNG 设置。

66 项自动化测试通过，包含 PDF/EPS 文件检查、画板与尺寸传递、部分失败保留结果及原稿保护；自动化测试不启动 Illustrator，实际图稿仍需视觉核对。

## 下载哪个文件

- **普通用户：`Figure Converter.zip`**，解压后打开 Mac 应用。
- 开发者：`Figure-Converter-v0.3.0-source.zip` 或 GitHub 自动生成的 Source code。
- `SHA256SUMS.txt` 用于校验下载文件。

## 使用条件

当前安装包面向 Apple Silicon Mac，最低构建目标为 macOS 13；需要已安装并激活的 Adobe Illustrator，以及 `/usr/bin/python3` 3.9+。本工具不包含 Illustrator 或其许可证，不提供 Windows/Linux 版本。LibreOffice 不是默认导出的依赖。

当前版本使用临时签名，未完成 Apple 公证。安装和权限说明见仓库的 `docs/USAGE.md`。

## 保真范围

文字与线条尽量保留矢量；原有 PNG/照片不会变成矢量。PDF 使用支持透明的原生保存方式。EPS 的透明内容会扁平化，EMF 的部分渐变或透明区域也可能转成图片；这些格式不能保证全矢量。少数无法直接嵌入的内部置入对象会按外观恢复为图片。正式投稿前请在目标排版软件中核对结果。

源码采用 MIT 许可证；Adobe Illustrator 等外部软件适用各自许可。
