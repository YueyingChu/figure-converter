# 使用说明

Figure Converter 将已保存的 Illustrator 文件转换为 SVG、EMF、PNG、PDF 和 EPS。转换在本机完成，预处理只作用于临时副本。

## 首次启动

1. 确认使用 Apple Silicon Mac，并已安装、激活 Illustrator。应用当前按标准安装位置 `/Applications/Adobe Illustrator */Adobe Illustrator.app` 查找 Illustrator。
2. 确认系统 Python 可用。在「终端」运行：

   ```sh
   /usr/bin/python3 --version
   ```

   需要 Python 3.9 或以上。应用固定调用这个路径；仅在其他位置安装 Python 不会替代它。如果该路径不可用，可按 Apple 的 [Command Line Tools 安装说明](https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/)运行 `xcode-select --install`，安装完成后再次检查版本。无需安装 pip 包。
3. 从本仓库的 Releases 下载 `Figure-Converter.zip`，解压并打开 `Figure Converter.app`。也可将应用移入「应用程序」文件夹。
4. macOS 首次询问是否允许控制 Illustrator 时，允许这项「自动化」权限。若此前拒绝，可在「系统设置 → 隐私与安全性 → 自动化」中调整。

当前应用使用临时签名，尚未使用 Developer ID 签名或完成 Apple 公证。若 macOS 提示无法验证开发者，仅在确认下载来源可信后，按 [Apple 官方说明](https://support.apple.com/en-us/102445)使用系统提供的「仍要打开」选项；也可以从本仓库源码构建。

## 一次完整转换

1. 在 Illustrator 中确认字体和链接完整，保存图稿。
2. 将一个或多个文件拖入 Figure Converter，或点击选择文件。
3. 选择需要的格式；默认选中 SVG、EMF、PNG，PDF 和 EPS 需按需勾选。可一次导出全部五种格式。
4. 设置画板、裁剪、尺寸与 PNG 分辨率，点击「开始转换」。
5. 等待本批任务完成。结果卡片会显示生成的文件、尺寸和处理说明。

每个画板会按每种已选格式各自输出一份文件；PDF 也是每画板一份单页文件。默认处理全部画板，也可选择「首个」。右侧设置面板可以滚动。

需要连续处理时，可开启「拖入后自动转换」。开始转换后，本批设置固定；转换结束后再修改设置。要用新设置重做已完成的文件，点击「再次导出」。

默认保存到源文件旁的 `Figure Exports`，每次新建独立子文件夹。可在界面中另选保存位置。转换失败时会保留已经完成的输出，并显示错误原因。

## 选择输出格式

| 格式 | 适用场景 | 需要留意 |
| --- | --- | --- |
| SVG | 继续排版、编辑或用于网页 | 保留路径和普通渐变；照片等内容以嵌入图片保存 |
| EMF | 插入 Word、PowerPoint | 复杂透明或渐变可能局部栅格化，请在目标软件中核对 |
| PNG | 需要确定像素尺寸的投稿、演示或分享 | 可选 DPI 和背景；放大清晰度受源图片限制 |
| PDF | 论文排版、打印、分享 | 通过 Illustrator 原生保存，保留矢量和支持的透明效果；原有图片仍是图片 |
| EPS | 投稿系统或旧排版流程明确要求 EPS | 透明内容会扁平化，部分区域可能变成图片 |

PDF 与 EPS 都沿用当前的文字转轮廓和描边展开选项，并应用相同的裁剪与宽度设置。PDF 采用支持透明的保存方式；EPS 在其他排版软件中使用的是扁平化后的内容。即使重新用 Illustrator 打开 EPS 能看到部分原生编辑数据，也不代表其他软件支持这些透明效果。详见 [Adobe 透明图稿说明](https://helpx.adobe.com/illustrator/using/printing-saving-transparent-artwork.html)。

## 裁剪、宽度与清晰度

**裁剪画板白边**会把画板缩到可见图形边界，默认开启。裁剪范围限制在原画板内，并考虑常见剪切蒙版。它依据图形对象的边界计算，因此图片内部的白色像素、白色背景矩形仍会保留。需要原有留白时，关闭此选项。

**自定义宽度**默认关闭。开启后输入毫米数，例如 `180`，工具先裁剪，再等比例缩放；高度自动计算。支持大于 0、且不超过 1000 mm 的宽度。全部五种输出格式都使用此设置；多个画板都会采用这个宽度，各自保留长宽比。

PNG 的像素宽度由打印宽度和 DPI 共同决定：

```text
像素宽度 ≈ 宽度（mm）÷ 25.4 × DPI
180 mm、600 DPI ≈ 4252 px
```

PNG 可选 300、600、1200 DPI，默认 600 DPI；支持透明或白色背景。提高 DPI 会让矢量路径渲染得更细，但无法补回原始照片或 PNG 中缺失的细节。为限制内存使用，PNG 单边最多 30000 像素、总计最多 1 亿像素。

SVG、PDF 和 EPS 会按所选画板尺寸输出。PNG 的 DPI 选项用于设置像素输出；PDF 和 EPS 的透明处理由各自的原生保存方式决定。EMF 会在可安全修正时按目标尺寸保存；若文件使用未知的坐标映射，工具会保留完整的原生结果并提示尺寸差异，此时在 Word 或 PowerPoint 中设置所需宽度并锁定长宽比。

## 文字、虚线与置入图片

「文字转轮廓」和「展开描边与虚线」默认开启，使用 Illustrator 处理副本中的可见内容。文字转轮廓后成为路径，后续无法直接作为文字编辑；需要保留文字时可关闭该选项。

工具会嵌入能解析的外部链接图片。若链接明确指向不存在的文件，转换会停止，请先在 Illustrator 中修复链接。

少数置入对象只有文档内部保存的外观，没有可访问的源文件。工具会先尝试嵌入；仍无法嵌入时，只将该对象恢复为透明图片，以免导出时漏掉它，其余文字和路径继续按矢量处理。这种恢复不能保留该对象内部的矢量结构，也不能创造原图中没有的细节；恢复采样最高 2400 DPI，大幅放大后需要检查清晰度。

每份结果目录包含 `conversion-report.json`，记录实际选项、原文件校验值、处理数量、尺寸和输出结构。界面的「有 N 条说明」可展开查看。报告包含本地文件路径，分享报告前请检查其中的信息。

## 保真与范围

| 情况 | 建议 |
| --- | --- |
| 缺失字体 | 先在 Illustrator 中检查。Illustrator 可能在转轮廓前替换字体，工具目前无法可靠检测所有替换 |
| EMF 中有复杂渐变、透明、蒙版或混合模式 | 部分区域可能栅格化；在最终使用的 Word、PowerPoint 或其他软件中核对 |
| EPS 中含透明、蒙版或混合模式 | 保存时需扁平化，可能产生局部位图；在实际排版软件中检查边缘和外观 |
| PDF 中含原有照片或已栅格化的效果 | 原生 PDF 保存不会把这些内容重新变成矢量 |
| 大画板或低分辨率透明图片 | EMF 可能采用较保守的导出精度，留意结果中的说明和小字边缘 |
| 组级阴影、发光等效果 | 建议关闭自动裁剪，先手动调整画板，避免效果边缘被切掉 |
| 复杂画笔、活效果、图表或符号 | 检查导出的线条粗细、虚线间隔和整体外观 |
| 多页 PDF | 先拆页；当前通过 Illustrator 导入，未实现多页 PDF 批量转换 |
| Illustrator 中有未保存修改 | 先保存。工具读取磁盘文件，不读取另一个已打开文档的未保存状态 |

EMF 支持情况还取决于查看软件。当前没有覆盖所有系统与 Office 版本，也未建立与 CloudConvert 的逐图质量基准；结构检查不能代替视觉比较。

当前拖拽入口是应用窗口，系统级快捷键圆环尚未实现。转换使用 Illustrator 的全局脚本接口，任务运行时请等待完成，再继续在 Illustrator 中编辑。

## 常见问题

| 问题 | 处理方法 |
| --- | --- |
| 提示找不到 Illustrator | 确认已安装、激活，并位于标准「应用程序」目录 |
| 无法运行 Python | 检查 `/usr/bin/python3 --version`；需要该路径下的 Python 3.9 或以上 |
| 无法控制 Illustrator | 检查 macOS「隐私与安全性 → 自动化」权限，并查看 Illustrator 是否有待处理对话框 |
| 图中还有白色区域 | 裁剪只调整画板边界；请在 Illustrator 中移除不需要的白色背景对象，或处理图片自身背景 |
| 大尺寸 PNG 导出失败 | 降低 DPI 或输出宽度，使像素尺寸在限制内 |
| 已有部分文件，另一格式失败 | 已完成文件会保留；查看错误说明，修复后再次导出 |

## 命令行

在源码目录中运行。示例使用当前目录的 `figure.ai`：

```sh
# 检查本机转换引擎
/usr/bin/python3 engine/converter.py probe

# 按原画板尺寸导出三种格式
/usr/bin/python3 engine/converter.py convert "figure.ai" \
  --format svg --format emf --format png --dpi 600

# 裁剪画板白边，按 180 mm 宽等比例输出全部五种格式
/usr/bin/python3 engine/converter.py convert "figure.ai" \
  --format svg --format emf --format png --format pdf --format eps \
  --trim-artboard --width-mm 180 --dpi 600

# 只输出 PDF 和 EPS
/usr/bin/python3 engine/converter.py convert "figure.ai" \
  --format pdf --format eps --trim-artboard --width-mm 180
```

命令行需显式传入 `--trim-artboard` 才裁剪，GUI 默认开启裁剪。其他选项：

| 参数 | 作用 |
| --- | --- |
| `--output-dir "exports"` | 指定输出目录 |
| `--keep-text` | 保留文字，不转轮廓 |
| `--keep-strokes` | 保留描边，不展开 |
| `--white-background` | PNG 使用白色背景 |
| `--first-artboard` | 只导出首个画板 |

默认 EMF 使用 Illustrator。命令行还提供实验选项 `--emf-engine libreoffice`，需要单独安装 LibreOffice；该路线可能改变透明图片或细线，只适合对照检查，正式交付建议使用默认方式。

源码构建和测试命令见 [README](../README.md#从源码构建)。
