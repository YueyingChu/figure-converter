# 发布到 GitHub

源码放进 GitHub 仓库，编译好的应用 ZIP 放进 GitHub Releases。普通使用者下载应用 ZIP，开发者下载源码；两者用途不同。

## 1. 生成发布材料

在项目根目录运行：

```sh
bash scripts/build-app.sh
/usr/bin/python3 scripts/package-release.py
```

当前版本生成 `dist/release-v0.3.0/`：

| 文件 | 用途 |
| --- | --- |
| `Figure-Converter.zip` | 给 Apple Silicon Mac 用户下载的应用 |
| `Figure-Converter-v0.3.0-source.zip` | 仅含公开源码、文档、许可证和合成测试脚本 |
| `SHA256SUMS.txt` | 校验上面两个 ZIP 是否传输完整 |
| `RELEASE_NOTES.md` | 可直接粘贴到 GitHub Release 的说明 |

源码包按明确的文件清单生成，不包含 `.git`、本地配置、真实图稿、`tests/artifacts/`、导出结果或编译缓存。不要把整个工作目录压缩后直接上传，也不要使用 `git add -f` 将这些忽略项加入仓库。

## 2. 创建并上传源码仓库

1. 在 GitHub 新建仓库，例如 `figure-converter`，选择 **Public**。创建空仓库，不额外生成 README、许可证或 `.gitignore`，因为源码包已经包含它们。
2. 将源码 ZIP 解压到一个新的文件夹。以下命令在该文件夹中运行，先把远程地址中的 `YOUR_USERNAME` 替换为自己的 GitHub 用户名：

```sh
git init -b main
git add .gitignore README.md LICENSE app engine scripts tests docs
git commit -m "Release Figure Converter 0.3.0"
git remote add origin https://github.com/YOUR_USERNAME/figure-converter.git
git push -u origin main
```

推送需要使用你自己的 GitHub 登录凭据。也可以使用 GitHub Desktop 发布这个新文件夹。不要把源码 ZIP 作为仓库中唯一的文件上传；展开的源码和根目录 README 才能被 GitHub 正常展示。

## 3. 发布可下载的应用

在仓库页面进入 **Releases → Draft a new release**：

1. 新建标签 `v0.3.0`，目标分支选择刚上传源码的 `main`。
2. 标题填写 `Figure Converter v0.3.0`，说明复制发布目录中的 `RELEASE_NOTES.md`。
3. 在附件区上传 `Figure-Converter.zip`、`Figure-Converter-v0.3.0-source.zip` 和 `SHA256SUMS.txt`。
4. 当前版本属于早期本地工具，建议勾选 **This is a pre-release**，先保存草稿确认附件齐全，再点击 **Publish release**。

GitHub 会额外生成名为 “Source code” 的压缩包。它们是源码，不是可双击运行的 Mac 应用。给普通用户的下载指引应明确选择 **`Figure-Converter.zip`**。操作入口可参考 [GitHub 官方发布说明](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)。

## 4. 发给其他人的使用说明

可以直接复制下面这段，并附上你的仓库或 Release 链接：

> Figure Converter 是 macOS 科研图转换工具，支持 AI/EPS/SVG/PDF 导出 SVG、EMF、高分辨率 PNG、PDF 和 EPS。请在 Releases 下载 `Figure-Converter.zip`，解压后将应用放入“应用程序”。当前安装包面向 Apple Silicon Mac，需要安装并激活 Adobe Illustrator，并确保 `/usr/bin/python3` 为 3.9 或更高版本。首次使用允许应用控制 Illustrator。拖入文件、选择输出格式，再点“开始转换”；默认选择 SVG、EMF、PNG，PDF/EPS 可按需勾选。期刊要求 180 mm 时，勾选“裁剪画板白边”和“自定义宽度”，输入 180；五种格式均按此宽度等比例缩放，每个画板单独输出。EPS 中的透明效果会扁平化，正式使用前请核对。

更详细的依赖安装、权限和常见问题见 [使用说明](USAGE.md)。

## 签名、平台和后续版本

当前应用是 Apple Silicon 本地构建，使用临时签名，**没有 Developer ID 签名或 Apple 公证**。GitHub 上传不会使它自动获得公证；首次下载到其他 Mac 上可能出现验证提示。普通用户应按 Apple 的 [安全打开应用说明](https://support.apple.com/en-us/102445) 核实来源后自行决定是否打开。

若希望面向更广泛用户发布并减少这类提示，需要由维护者使用自己的 Apple Developer 凭据完成 Developer ID 签名和公证。请遵循 [Apple 的软件公证说明](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)。不要把签名私钥、证书密码或 Apple 凭据提交到仓库。

后续发布时，更新 `app/Info.plist` 的版本与构建号、README 和发布说明，然后重新构建并打包。`package-release.py` 会拒绝与当前源码或版本不匹配的应用 ZIP，防止把旧安装包当作新版本发布。
