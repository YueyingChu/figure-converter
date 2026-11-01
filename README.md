# Figure Converter

把 Illustrator 科研插图拖进窗口，批量导出 **SVG、EMF、PDF、EPS 和高分辨率 PNG**。支持文字转轮廓、展开虚线、裁剪画板留白，以及按毫米设置等比例输出宽度。

当前版本：**0.3.1**。黑白界面，本地转换。

## 使用要求

| 项目 | 要求 |
| --- | --- |
| 电脑 | Apple Silicon Mac；构建目标为 macOS 13 及以上，目前仅在开发者本机环境实测 |
| Illustrator | 已安装并激活 Adobe Illustrator；当前实测 Illustrator 2026 / 30.2.1 |
| Python | `/usr/bin/python3`，版本 3.9 或以上；仅使用标准库，无需安装 pip 包 |
| 权限 | 首次使用时，允许 Figure Converter 通过 macOS「自动化」控制 Illustrator |

应用需要调用本机 Illustrator。默认转换无需 LibreOffice，也不会上传图稿。当前不支持 Windows/Linux；Intel Mac 需自行构建，尚未验证。

## 开始使用

1. 在本仓库的 **Releases** 中下载 `Figure-Converter.zip`，解压后打开应用；也可按下方说明从源码构建。
2. 拖入一个或多个已保存的 `.ai` 文件，选择需要的格式。默认选中 SVG、EMF、PNG，PDF 和 EPS 可按需勾选。
3. 按需开启「自定义宽度」，例如填入 **180 mm**；高度自动按比例计算。PNG 可选 **300 / 600 / 1200 DPI**。
4. 点击「开始转换」。完成后，点击结果中的文件夹按钮查看输出。

五种格式都支持裁剪、按指定宽度等比例缩放，并按画板分别输出。默认开启裁剪画板白边、文字转轮廓和展开描边，导出所有画板。每次转换会在原文件旁的 `Figure Exports` 中新建文件夹，也可自行选择保存位置。原稿和已有输出均保留。

完整操作、首次启动和常见问题见 **[使用说明](docs/USAGE.md)**。

## 适合怎样使用

| 输出 | 用途 |
| --- | --- |
| SVG | 保留路径和普通渐变，继续排版或编辑；内嵌图片一同保存 |
| EMF | 插入 Word、PowerPoint 等软件；复杂透明或渐变可能以局部图片保存 |
| PNG | 按目标尺寸和 DPI 渲染，支持透明或白色背景 |
| PDF | 供论文排版、打印或分享，使用 Illustrator 原生保存，保留矢量与支持的透明效果 |
| EPS | 用于明确要求 EPS 的投稿或排版流程；透明效果需要扁平化，部分区域可能成为图片 |

例如，**180 mm、600 DPI** 的 PNG 宽度约为 **4252 像素**。裁剪会先去掉画板外沿的空白，再按指定宽度缩放；每个画板分别保持自身长宽比。

## 使用前了解

- 主要处理 Illustrator AI 文件；也接受 EPS、SVG、PDF。多页 PDF 需先拆页。
- 转轮廓前仍依赖本机字体。请先在 Illustrator 检查缺失字体与链接，并保存文件；转换过程中等待任务完成后再继续编辑。
- **EMF 的外观需要在最终使用的软件中核对。** 部分效果会栅格化，少数文件的实际插入尺寸可能需要手动调整。当前没有覆盖所有 Word、PowerPoint 或 Windows 版本。
- EPS 的透明效果会按外观扁平化，不能保证全部内容都是矢量；需要保留透明效果时可选择 PDF。
- 图中已有的 PNG、照片会保留。少数无法直接嵌入的内部置入对象会按外观恢复为图片，其内部矢量结构不再保留。
- 组级阴影、发光等复杂效果可能超出自动裁剪识别的边界；这类图建议保留原画板或手动调整画板。

更多保真边界和处理方法见[使用说明](docs/USAGE.md#保真与范围)。

## 从源码构建

需要 Apple 的 Swift 编译器和 macOS SDK，可通过 Xcode 或 [Command Line Tools](https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/) 提供。在仓库根目录运行：

```sh
bash scripts/build-app.sh
```

构建结果为 `dist/Figure Converter.app` 和 `dist/Figure-Converter.zip`。应用使用临时签名，尚未使用 Developer ID 签名或完成 Apple 公证；首次打开的说明见[使用说明](docs/USAGE.md#首次启动)。

运行自动化检查：

```sh
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py' -v
```

当前有 66 项自动化测试，覆盖输出结构、尺寸、图片采样、失败处理和原稿保护；这些测试不会启动 Illustrator。实际图稿仍需进行视觉核对。[命令行用法](docs/USAGE.md#命令行) · [发布说明](docs/PUBLISHING.md)

## 参考与许可

拖拽工作流的灵感来自 [Filee](https://github.com/KnifeLemon/Filee)。本项目独立实现，核心导出调用本机 Illustrator。

- [Adobe Illustrator 导出说明](https://helpx.adobe.com/illustrator/using/exporting-artwork.html)
- [Adobe 关于透明图稿与扁平化的说明](https://helpx.adobe.com/illustrator/using/printing-saving-transparent-artwork.html)
- [Adobe Illustrator 脚本接口](https://developer.adobe.com/illustrator/scripting/)

本项目采用 [MIT License](LICENSE)。Adobe Illustrator 等外部软件遵循各自的许可条款，未随本项目分发。
