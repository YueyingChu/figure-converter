import SwiftUI
import AppKit
import UniformTypeIdentifiers

private enum Palette {
    private static func shade(light: CGFloat, dark: CGFloat) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            return NSColor(white: isDark ? dark : light, alpha: 1)
        })
    }

    static let paper = shade(light: 0.96, dark: 0.10)
    static let surface = shade(light: 1.0, dark: 0.145)
    static let ink = shade(light: 0.10, dark: 0.94)
    static let accent = ink
    static let onAccent = shade(light: 1.0, dark: 0.08)
    static let muted = shade(light: 0.40, dark: 0.66)
    static let border = shade(light: 0.86, dark: 0.26)
    static let wash = ink.opacity(0.07)
}

private enum JobState: Equatable {
    case waiting, processing, done, failed
    var label: String {
        switch self {
        case .waiting: return "待转换"
        case .processing: return "转换中"
        case .done: return "已完成"
        case .failed: return "未完成"
        }
    }
}

private struct OutputFile: Decodable {
    let path: String
    let format: String
    let artboard: Int?
    let width_mm: Double?
    let height_mm: Double?
    let width_px: Int?
    let height_px: Int?
    let dpi: Int?

    var sizeDescription: String? {
        var parts: [String] = []
        if let width = width_mm, let height = height_mm,
           width.isFinite, height.isFinite, width > 0, height > 0 {
            func mm(_ value: Double) -> String {
                let text = String(format: "%.2f", value)
                return text.replacingOccurrences(of: #"\.?0+$"#, with: "", options: .regularExpression)
            }
            parts.append("\(mm(width)) × \(mm(height)) mm")
        }
        if let width = width_px, let height = height_px {
            parts.append("\(width) × \(height) px")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}

private struct ConversionResult: Decodable {
    let success: Bool
    let outputs: [OutputFile]?
    let warnings: [String]?
    let error: String?
    let output_dir: String?
}

private struct EngineInfo: Decodable {
    let available: Bool
    let path: String?
}

private struct ProbeResult: Decodable {
    let illustrator: EngineInfo
    let libreoffice: EngineInfo
}

private struct ConversionJob: Identifiable {
    let id = UUID()
    let url: URL
    var state = JobState.waiting
    var outputs: [OutputFile] = []
    var warnings: [String] = []
    var error: String?
    var outputDirectory: String?
}

private struct BackendResponse {
    let data: Data
    let stderr: String
    let status: Int32
}

private enum Backend {
    static var scriptURL: URL? {
        Bundle.main.resourceURL?.appendingPathComponent("engine/converter.py")
    }

    /// Called only on a background queue. Both streams are drained concurrently.
    static func run(_ arguments: [String]) throws -> BackendResponse {
        guard let script = scriptURL, FileManager.default.fileExists(atPath: script.path) else {
            throw NSError(domain: "FigureConverter", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "找不到转换引擎，请重新构建应用。"])
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [script.path] + arguments
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        // Keep Python imports from adding cache files to the signed app bundle.
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        let output = Pipe()
        let errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        try process.run()
        let group = DispatchGroup()
        let lock = NSLock()
        var outputData = Data()
        var errorData = Data()
        group.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            let bytes = output.fileHandleForReading.readDataToEndOfFile()
            lock.lock(); outputData = bytes; lock.unlock()
            group.leave()
        }
        group.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            let bytes = errors.fileHandleForReading.readDataToEndOfFile()
            lock.lock(); errorData = bytes; lock.unlock()
            group.leave()
        }
        process.waitUntilExit()
        group.wait()
        return BackendResponse(data: outputData,
                               stderr: String(data: errorData, encoding: .utf8) ?? "",
                               status: process.terminationStatus)
    }

    static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        let decoder = JSONDecoder()
        if let value = try? decoder.decode(type, from: data) { return value }
        // Accommodate a diagnostic printed before the engine's final JSON line.
        if let string = String(data: data, encoding: .utf8),
           let last = string.split(separator: "\n").last,
           let finalData = String(last).data(using: .utf8) {
            return try decoder.decode(type, from: finalData)
        }
        return try decoder.decode(type, from: data)
    }
}

private final class ConverterModel: ObservableObject {
    @Published var jobs: [ConversionJob] = []
    @Published var formats: Set<String> = ["svg", "emf", "png"]
    @Published var dpi = 600
    @Published var outlineText = true
    @Published var outlineStrokes = true
    @Published var whiteBackground = false
    @Published var firstArtboard = false
    @Published var trimArtboard = true
    @Published var customWidthEnabled = false
    @Published var widthMMText = "180"
    @Published var autoConvert = false
    @Published var outputFolder: URL?
    @Published var busy = false
    @Published var probing = true
    @Published var illustratorAvailable = false
    @Published var notice: String?
    @Published var dragHighlighted = false
    @Published var expandedDetails: Set<UUID> = []
    private var pendingRun: [UUID] = []
    private var runFormats: [String] = []
    private var runOptions: [String] = []
    private var runOutputFolder: URL?

    private static let missingIllustratorNotice = "未检测到 Adobe Illustrator。请安装并完成首次启动后，点“重新检测”。"

    var waitingCount: Int { jobs.filter { $0.state == .waiting || $0.state == .failed }.count }
    var doneCount: Int { jobs.filter { $0.state == .done }.count }
    var requestedWidthMM: Double? {
        guard let value = Double(widthMMText.trimmingCharacters(in: .whitespacesAndNewlines)),
              value.isFinite, value > 0, value <= 1000 else { return nil }
        return value
    }
    var widthValidationMessage: String? {
        customWidthEnabled && requestedWidthMM == nil ? "请输入大于 0、且不超过 1000 的宽度（mm）。" : nil
    }
    var canConvert: Bool {
        !busy && !probing && illustratorAvailable && !formats.isEmpty && waitingCount > 0 && widthValidationMessage == nil
    }

    var remainingInRun: Int {
        pendingRun.count + jobs.filter { $0.state == .processing }.count
    }
    /// Why the convert button is unavailable, shown next to it.
    var convertHint: String? {
        guard !busy, !probing else { return nil }
        if !illustratorAvailable { return "需要 Illustrator" }
        if jobs.isEmpty { return "请先添加文件" }
        if formats.isEmpty { return "请至少选择一种格式" }
        if widthValidationMessage != nil { return "宽度无效" }
        if waitingCount == 0 { return "全部已完成" }
        return nil
    }

    func probe() {
        probing = true
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let response = try Backend.run(["probe"])
                let result = try Backend.decode(ProbeResult.self, from: response.data)
                DispatchQueue.main.async {
                    self.illustratorAvailable = result.illustrator.available
                    if !result.illustrator.available {
                        self.notice = Self.missingIllustratorNotice
                    } else if self.notice == Self.missingIllustratorNotice {
                        self.notice = nil
                    }
                    self.probing = false
                }
            } catch {
                DispatchQueue.main.async {
                    self.notice = "检查转换环境失败：\(error.localizedDescription)"
                    self.probing = false
                }
            }
        }
    }

    func add(_ urls: [URL]) {
        let accepted: Set<String> = ["ai", "eps", "svg", "pdf"]
        var rejected = 0
        for url in urls {
            var directory: ObjCBool = false
            guard url.isFileURL,
                  FileManager.default.fileExists(atPath: url.path, isDirectory: &directory),
                  !directory.boolValue,
                  accepted.contains(url.pathExtension.lowercased()) else { rejected += 1; continue }
            if !jobs.contains(where: { $0.url.standardizedFileURL == url.standardizedFileURL }) {
                let job = ConversionJob(url: url)
                jobs.append(job)
                if busy && autoConvert { pendingRun.append(job.id) }
            }
        }
        if rejected > 0 { notice = "已略过 \(rejected) 个不支持的项目。请添加 AI、EPS、SVG 或 PDF 文件。" }
        if autoConvert && canConvert { convert() }
    }

    func addDropped(_ providers: [NSItemProvider]) -> Bool {
        let fileProviders = providers.filter { $0.canLoadObject(ofClass: URL.self) }
        guard !fileProviders.isEmpty else { return false }
        let group = DispatchGroup()
        let lock = NSLock()
        var urls: [Int: URL] = [:]
        for (index, provider) in fileProviders.enumerated() {
            group.enter()
            _ = provider.loadObject(ofClass: URL.self) { url, _ in
                if let url {
                    lock.lock(); urls[index] = url; lock.unlock()
                }
                group.leave()
            }
        }
        // One add call per drop: auto-convert then sees the whole batch at once.
        group.notify(queue: .main) {
            self.add(urls.keys.sorted().compactMap { urls[$0] })
        }
        return true
    }

    func pickFiles() {
        let panel = NSOpenPanel()
        panel.title = "选择科研图文件"
        panel.prompt = "添加"
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = ["ai", "eps", "svg", "pdf"].compactMap { UTType(filenameExtension: $0) }
        if panel.runModal() == .OK { add(panel.urls) }
    }

    func pickOutputFolder() {
        let panel = NSOpenPanel()
        panel.title = "选择导出位置"
        panel.prompt = "使用此文件夹"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        if panel.runModal() == .OK { outputFolder = panel.url }
    }

    func remove(_ id: UUID) {
        guard !busy else { return }
        jobs.removeAll { $0.id == id }
        expandedDetails.remove(id)
    }

    func toggleDetails(_ id: UUID) {
        if expandedDetails.contains(id) { expandedDetails.remove(id) }
        else { expandedDetails.insert(id) }
    }

    func openReport(_ job: ConversionJob) {
        guard let folder = job.outputDirectory else { return }
        let report = URL(fileURLWithPath: folder).appendingPathComponent("conversion-report.json")
        if FileManager.default.fileExists(atPath: report.path) { NSWorkspace.shared.open(report) }
    }

    func requeueCompleted() {
        guard !busy else { return }
        for index in jobs.indices where jobs[index].state == .done {
            jobs[index].state = .waiting
            jobs[index].outputs = []
            jobs[index].outputDirectory = nil
            jobs[index].warnings = []
            jobs[index].error = nil
        }
    }

    func convert() {
        guard canConvert else { return }
        notice = nil
        pendingRun = jobs.filter { $0.state == .waiting || $0.state == .failed }.map(\.id)
        runFormats = ["svg", "emf", "png", "pdf", "eps"].filter { formats.contains($0) }
        runOptions = ["--dpi", String(dpi)]
        if !outlineText { runOptions.append("--keep-text") }
        if !outlineStrokes { runOptions.append("--keep-strokes") }
        if whiteBackground { runOptions.append("--white-background") }
        if firstArtboard { runOptions.append("--first-artboard") }
        if trimArtboard { runOptions.append("--trim-artboard") }
        if customWidthEnabled, let width = requestedWidthMM { runOptions += ["--width-mm", String(width)] }
        runOutputFolder = outputFolder
        busy = true
        runNext()
    }

    private func runNext() {
        guard !pendingRun.isEmpty else {
            busy = false
            return
        }
        let id = pendingRun.removeFirst()
        guard let index = jobs.firstIndex(where: { $0.id == id }) else { runNext(); return }
        jobs[index].state = .processing
        jobs[index].outputs = []
        jobs[index].outputDirectory = nil
        jobs[index].warnings = []
        jobs[index].error = nil
        let file = jobs[index].url
        let output = runOutputFolder ?? file.deletingLastPathComponent().appendingPathComponent("Figure Exports", isDirectory: true)
        var args = ["convert", file.path, "--output-dir", output.path]
        for format in runFormats { args += ["--format", format] }
        args += runOptions
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let response = try Backend.run(args)
                let result: ConversionResult
                do {
                    result = try Backend.decode(ConversionResult.self, from: response.data)
                } catch {
                    let diagnostic = response.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                    throw NSError(domain: "FigureConverter", code: Int(response.status), userInfo: [
                        NSLocalizedDescriptionKey: diagnostic.isEmpty ? "转换引擎未返回结果（退出码 \(response.status)）。" : diagnostic
                    ])
                }
                DispatchQueue.main.async {
                    if let current = self.jobs.firstIndex(where: { $0.id == id }) {
                        self.jobs[current].state = result.success ? .done : .failed
                        self.jobs[current].outputs = result.outputs ?? []
                        self.jobs[current].warnings = result.warnings ?? []
                        self.jobs[current].error = result.error
                        self.jobs[current].outputDirectory = result.output_dir
                    }
                    self.runNext()
                }
            } catch {
                DispatchQueue.main.async {
                    if let current = self.jobs.firstIndex(where: { $0.id == id }) {
                        self.jobs[current].state = .failed
                        self.jobs[current].error = error.localizedDescription
                    }
                    self.runNext()
                }
            }
        }
    }

    func reveal(_ job: ConversionJob) {
        if let output = job.outputs.first {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: output.path)])
        } else if let folder = job.outputDirectory {
            NSWorkspace.shared.open(URL(fileURLWithPath: folder))
        }
    }
}

private struct CropMarks: Shape {
    var length: CGFloat = 20

    func path(in rect: CGRect) -> Path {
        var path = Path()
        for (corner, x, y) in [(CGPoint(x: rect.minX, y: rect.minY), length, length),
                               (CGPoint(x: rect.maxX, y: rect.minY), -length, length),
                               (CGPoint(x: rect.minX, y: rect.maxY), length, -length),
                               (CGPoint(x: rect.maxX, y: rect.maxY), -length, -length)] {
            path.move(to: CGPoint(x: corner.x + x, y: corner.y))
            path.addLine(to: corner)
            path.addLine(to: CGPoint(x: corner.x, y: corner.y + y))
        }
        return path
    }
}

private struct SmallLinkButton: View {
    let title: String
    var color = Palette.ink
    let action: () -> Void

    init(_ title: String, color: Color = Palette.ink, action: @escaping () -> Void) {
        self.title = title
        self.color = color
        self.action = action
    }

    var body: some View {
        Button(title, action: action).buttonStyle(.plain)
            .font(.system(size: 12, weight: .medium)).foregroundColor(color)
    }
}

private struct FormatRow: View {
    let title: String
    let subtitle: String
    let selected: Bool
    let available: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: selected ? "checkmark.square.fill" : "square")
                    .font(.system(size: 15)).foregroundColor(selected ? Palette.accent : Palette.muted)
                Text(title).font(.system(size: 12, weight: selected ? .bold : .medium, design: .monospaced))
                    .frame(width: 32, alignment: .leading)
                Text(subtitle).font(.system(size: 12)).foregroundColor(Palette.muted).lineLimit(1)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 9).frame(height: 30)
            .background(selected ? Palette.wash : Color.clear)
            .clipShape(RoundedRectangle(cornerRadius: 7))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(!available).opacity(available ? 1 : 0.45)
        .accessibilityLabel("\(title)，\(subtitle)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

private struct SettingsSection<Content: View>: View {
    let title: String
    var trailing: String?
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(title).font(.system(size: 12, weight: .semibold))
                Spacer()
                if let trailing {
                    Text(trailing).font(.system(size: 11)).foregroundColor(Palette.muted)
                }
            }
            content
        }
    }
}

private struct SettingRow<Control: View>: View {
    let title: String
    @ViewBuilder let control: Control

    var body: some View {
        HStack {
            Text(title).font(.system(size: 12))
            Spacer(minLength: 8)
            control
        }.frame(minHeight: 22)
    }
}

private struct Caption: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text).font(.system(size: 11)).foregroundColor(Palette.muted)
            .fixedSize(horizontal: false, vertical: true)
    }
}

private struct StatusBadge: View {
    let job: ConversionJob

    private var partial: Bool { job.state == .failed && !job.outputs.isEmpty }

    private var symbol: String? {
        switch job.state {
        case .waiting: return "circle.dashed"
        case .processing: return nil
        case .done: return "checkmark.circle.fill"
        case .failed: return partial ? "circle.lefthalf.filled" : "exclamationmark.circle.fill"
        }
    }

    var body: some View {
        HStack(spacing: 5) {
            if job.state == .processing { ProgressView().controlSize(.small).scaleEffect(0.7).frame(width: 14, height: 14) }
            if let symbol { Image(systemName: symbol) }
            Text(partial ? "部分完成" : job.state.label)
        }
        .font(.system(size: 12, weight: job.state == .waiting ? .regular : .medium))
        .foregroundColor(job.state == .failed ? Palette.onAccent : job.state == .waiting ? Palette.muted : Palette.ink)
        .padding(.horizontal, job.state == .failed ? 8 : 0).padding(.vertical, job.state == .failed ? 3 : 0)
        .background(job.state == .failed ? Palette.accent : Color.clear)
        .clipShape(Capsule())
    }
}

private struct JobRow: View {
    let job: ConversionJob
    let busy: Bool
    let onRemove: () -> Void
    let onReveal: () -> Void
    let detailsExpanded: Bool
    let onToggleDetails: () -> Void
    let onOpenReport: () -> Void

    var sizeSummaries: [String] {
        let grouped = Dictionary(grouping: job.outputs, by: { $0.artboard ?? 1 })
        return grouped.keys.sorted().compactMap { board in
            guard let files = grouped[board],
                  let preferred = files.first(where: { $0.format == "png" && $0.sizeDescription != nil })
                    ?? files.first(where: { $0.sizeDescription != nil }),
                  let size = preferred.sizeDescription else { return nil }
            let description = "\(preferred.format.uppercased())：\(size)"
            return grouped.count > 1 ? "画板 \(board) · \(description)" : description
        }
    }

    private var exportedFormats: [String] {
        ["svg", "emf", "png", "pdf", "eps"].filter { format in job.outputs.contains { $0.format == format } }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 11) {
                Text(job.url.pathExtension.uppercased())
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .frame(width: 36, height: 36)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(Palette.border))
                VStack(alignment: .leading, spacing: 2) {
                    Text(job.url.lastPathComponent).font(.system(size: 13, weight: .medium))
                        .lineLimit(1).truncationMode(.middle).help(job.url.path)
                    Text(!job.outputs.isEmpty ? "已导出 \(job.outputs.count) 个文件" : job.url.deletingLastPathComponent().lastPathComponent)
                        .font(.system(size: 11)).foregroundColor(Palette.muted).lineLimit(1)
                }
                Spacer(minLength: 8)
                StatusBadge(job: job)
                if !job.outputs.isEmpty {
                    Button(action: onReveal) { Image(systemName: "folder").font(.system(size: 13)).frame(width: 22, height: 22).contentShape(Rectangle()) }
                        .buttonStyle(.plain).help("在访达中显示导出文件").accessibilityLabel("在访达中显示导出文件")
                }
                if !busy {
                    Button(action: onRemove) { Image(systemName: "xmark").font(.system(size: 10, weight: .medium)).frame(width: 22, height: 22).contentShape(Rectangle()) }
                        .buttonStyle(.plain).foregroundColor(Palette.muted).help("从列表移除").accessibilityLabel("从列表移除")
                }
            }
            VStack(alignment: .leading, spacing: 7) {
                if let error = job.error, !error.isEmpty {
                    HStack(alignment: .top, spacing: 7) {
                        Image(systemName: "exclamationmark.triangle.fill").padding(.top, 1)
                        Text(error).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                    }
                    .font(.system(size: 12)).padding(9)
                    .background(Palette.wash).clipShape(RoundedRectangle(cornerRadius: 6))
                }
                if !exportedFormats.isEmpty {
                    HStack(alignment: .firstTextBaseline, spacing: 4) {
                        ForEach(exportedFormats, id: \.self) { format in
                            Text(format.uppercased()).font(.system(size: 10, weight: .semibold, design: .monospaced))
                                .padding(.horizontal, 5).padding(.vertical, 2)
                                .background(Palette.wash).clipShape(RoundedRectangle(cornerRadius: 4))
                        }
                        if let firstSize = sizeSummaries.first {
                            Text(firstSize + (sizeSummaries.count > 1 ? " · 另有 \(sizeSummaries.count - 1) 个画板" : ""))
                                .font(.system(size: 11).monospacedDigit()).foregroundColor(Palette.muted)
                                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true).padding(.leading, 5)
                        }
                    }
                }
                if !job.warnings.isEmpty || job.outputDirectory != nil {
                    Button(action: onToggleDetails) {
                        HStack(spacing: 5) {
                            Image(systemName: detailsExpanded ? "chevron.down" : "chevron.right").font(.system(size: 9, weight: .semibold))
                            Text(job.warnings.isEmpty ? "转换详情" : "有 \(job.warnings.count) 条说明")
                        }.font(.system(size: 11)).foregroundColor(Palette.muted).contentShape(Rectangle())
                    }.buttonStyle(.plain)
                    if detailsExpanded {
                        VStack(alignment: .leading, spacing: 7) {
                            if sizeSummaries.count > 1 {
                                ForEach(sizeSummaries, id: \.self) { size in
                                    Text(size).font(.system(size: 11).monospacedDigit()).foregroundColor(Palette.muted).textSelection(.enabled)
                                }
                            }
                            ForEach(Array(job.warnings.enumerated()), id: \.offset) { _, warning in
                                HStack(alignment: .top, spacing: 6) {
                                    Image(systemName: "info.circle").padding(.top, 1)
                                    Text(warning).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                                }.font(.system(size: 11)).foregroundColor(Palette.muted)
                            }
                            if job.outputDirectory != nil {
                                SmallLinkButton("查看转换报告", action: onOpenReport)
                            }
                        }.padding(.leading, 14)
                    }
                }
            }.padding(.leading, 47)
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
    }
}

private struct MainView: View {
    @StateObject private var model: ConverterModel

    init(model: ConverterModel = ConverterModel()) {
        _model = StateObject(wrappedValue: model)
    }

    var body: some View {
        VStack(spacing: 0) {
            titleBar
            Divider()
            HStack(spacing: 0) {
                filesPanel
                Divider()
                settingsPanel.frame(width: 330).background(Palette.surface)
            }
            Divider()
            actionBar
        }
        .frame(minWidth: 960, minHeight: 680)
        .background(Palette.paper)
        .foregroundColor(Palette.ink)
        .tint(Palette.accent)
        .ignoresSafeArea(.container, edges: .top)
        .onDrop(of: [.fileURL], isTargeted: $model.dragHighlighted) { providers in
            model.addDropped(providers)
        }
        .onAppear { model.probe() }
        .onOpenURL { model.add([$0]) }
    }

    // Height and top padding line the title up with the window's traffic lights.
    private var titleBar: some View {
        HStack(alignment: .top, spacing: 8) {
            Text("Figure Converter").font(.system(size: 13, weight: .semibold))
            Text("科研插图，一次导出。").font(.system(size: 12)).foregroundColor(Palette.muted)
            Spacer()
            HStack(spacing: 6) {
                Circle().fill(model.illustratorAvailable ? Palette.accent : Color.clear)
                    .overlay(Circle().stroke(Palette.muted, lineWidth: model.illustratorAvailable ? 0 : 1))
                    .frame(width: 6, height: 6)
                Text(model.probing ? "正在检查转换环境…" : model.illustratorAvailable ? "Illustrator 已就绪 · 五种格式本地导出" : "Illustrator 尚未就绪")
                    .font(.system(size: 11)).foregroundColor(Palette.muted)
                if !model.probing && !model.illustratorAvailable {
                    SmallLinkButton("重新检测") { model.probe() }
                }
            }.frame(height: 16)
        }
        .padding(.leading, 84).padding(.trailing, 16).padding(.top, 6).frame(height: 36, alignment: .top)
        .background(Palette.surface)
    }

    private var filesPanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 14) {
                Text("文件").font(.system(size: 13, weight: .semibold))
                if !model.jobs.isEmpty {
                    Text("\(model.jobs.count) 个文件 · \(model.doneCount) 个已完成")
                        .font(.system(size: 11).monospacedDigit()).foregroundColor(Palette.muted)
                }
                Spacer()
                if model.doneCount > 0 {
                    SmallLinkButton("再次导出") { model.requeueCompleted() }.disabled(model.busy)
                }
                if !model.jobs.isEmpty {
                    SmallLinkButton("清空列表", color: Palette.muted) { model.jobs.removeAll() }.disabled(model.busy)
                    addFilesButton
                }
            }.frame(height: 24)
            if let notice = model.notice { noticeBanner(notice) }
            if model.jobs.isEmpty { dropStage } else { jobList }
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var addFilesButton: some View {
        Button { model.pickFiles() } label: { Label("选择文件", systemImage: "plus") }
            .controlSize(.small)
            .keyboardShortcut("o", modifiers: .command)
            .help("选择文件（⌘O）")
    }

    private func noticeBanner(_ notice: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "info.circle").padding(.top, 1)
            Text(notice).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 4)
            if !model.probing && !model.illustratorAvailable {
                SmallLinkButton("重新检测") { model.probe() }
            }
            Button { model.notice = nil } label: {
                Image(systemName: "xmark").font(.system(size: 9, weight: .medium)).frame(width: 16, height: 16).contentShape(Rectangle())
            }.buttonStyle(.plain).foregroundColor(Palette.muted).help("关闭提示").accessibilityLabel("关闭提示")
        }
        .font(.system(size: 12)).padding(.horizontal, 10).padding(.vertical, 8)
        .background(Palette.surface).clipShape(RoundedRectangle(cornerRadius: 7))
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.border))
    }

    private var dropStage: some View {
        ZStack {
            if model.dragHighlighted { Rectangle().fill(Palette.wash) }
            CropMarks(length: model.dragHighlighted ? 34 : 22)
                .stroke(model.dragHighlighted ? Palette.accent : Palette.muted, lineWidth: model.dragHighlighted ? 2 : 1)
            VStack(spacing: 10) {
                Image(systemName: "square.and.arrow.down").font(.system(size: 30, weight: .light))
                Text(model.dragHighlighted ? "松开，即可加入列表" : "把图拖到这里").font(.system(size: 20, weight: .medium))
                Text("AI · EPS · SVG · PDF  /  支持批量添加").font(.system(size: 12)).foregroundColor(Palette.muted)
                addFilesButton.controlSize(.regular).padding(.top, 8)
                Text("原文件保留，结果另存。").font(.system(size: 11)).foregroundColor(Palette.muted).padding(.top, 8)
            }
        }
        .animation(.easeOut(duration: 0.12), value: model.dragHighlighted)
    }

    private var jobList: some View {
        ScrollView {
            LazyVStack(spacing: 0) {
                ForEach(model.jobs) { job in
                    JobRow(job: job, busy: model.busy,
                           onRemove: { model.remove(job.id) }, onReveal: { model.reveal(job) },
                           detailsExpanded: model.expandedDetails.contains(job.id),
                           onToggleDetails: { model.toggleDetails(job.id) }, onOpenReport: { model.openReport(job) })
                    if job.id != model.jobs.last?.id { Divider().padding(.leading, 59) }
                }
                Text(model.dragHighlighted ? "松开，即可加入列表" : "继续拖入文件，可追加到列表")
                    .font(.system(size: 11, weight: model.dragHighlighted ? .semibold : .regular))
                    .foregroundColor(model.dragHighlighted ? Palette.ink : Palette.muted)
                    .frame(maxWidth: .infinity).padding(.vertical, 12)
            }
        }
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: 9))
        .overlay(RoundedRectangle(cornerRadius: 9).stroke(model.dragHighlighted ? Palette.accent : Palette.border, lineWidth: model.dragHighlighted ? 2 : 1))
    }

    private var settingsPanel: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 13) {
                formatSection
                Divider()
                sizeSection.disabled(model.busy)
                Divider()
                pngSection.disabled(model.busy)
                Divider()
                artworkSection.disabled(model.busy)
            }.padding(14)
        }
    }

    private var formatSection: some View {
        SettingsSection(title: "输出格式", trailing: "已选 \(model.formats.count) / 5 · 可多选") {
            VStack(spacing: 2) {
                formatRow("svg", title: "SVG", subtitle: "保留矢量，适合继续排版")
                formatRow("emf", title: "EMF", subtitle: "用于 Word 与 PowerPoint", available: model.illustratorAvailable)
                formatRow("png", title: "PNG", subtitle: "高分辨率，支持透明背景")
                formatRow("pdf", title: "PDF", subtitle: "用于投稿、出版与打印")
                formatRow("eps", title: "EPS", subtitle: "适合传统期刊投稿")
            }.disabled(model.busy)
            if model.formats.contains("emf") { Caption("EMF 中的渐变或透明效果可能变为局部图片。") }
            if model.formats.contains("eps") { Caption("EPS 会合并透明效果，部分区域可能转为图片。") }
        }
    }

    private func formatRow(_ id: String, title: String, subtitle: String, available: Bool = true) -> some View {
        FormatRow(title: title, subtitle: subtitle, selected: model.formats.contains(id), available: available) {
            if model.formats.contains(id) { model.formats.remove(id) } else { model.formats.insert(id) }
        }
    }

    private var sizeSection: some View {
        SettingsSection(title: "裁剪与尺寸") {
            SettingRow(title: "裁剪画板白边") { settingSwitch("裁剪画板白边", isOn: $model.trimArtboard) }
            Caption("画板缩到图形边界，不会删除图片内部的白色区域。")
            SettingRow(title: "自定义宽度") { settingSwitch("自定义宽度", isOn: $model.customWidthEnabled) }
            if model.customWidthEnabled {
                HStack(spacing: 8) {
                    TextField("180", text: $model.widthMMText).textFieldStyle(.roundedBorder)
                        .multilineTextAlignment(.trailing).frame(width: 76)
                        .accessibilityLabel("导出宽度，单位毫米")
                    Text("mm").font(.system(size: 12)).foregroundColor(Palette.muted)
                    Spacer(minLength: 0)
                    Label("等比例", systemImage: "lock.fill").font(.system(size: 11)).foregroundColor(Palette.muted)
                }
                if let message = model.widthValidationMessage {
                    HStack(alignment: .top, spacing: 5) {
                        Image(systemName: "exclamationmark.circle.fill").padding(.top, 1)
                        Text(message).fixedSize(horizontal: false, vertical: true)
                    }.font(.system(size: 11, weight: .semibold))
                } else if let width = model.requestedWidthMM, model.formats.contains("png") {
                    Caption("PNG 宽约 \(Int((width / 25.4 * Double(model.dpi)).rounded())) px（\(model.dpi) DPI）")
                }
                Caption(model.trimArtboard ? "先裁掉画板空白，再按宽度等比例缩放；高度自动计算。" : "按当前画板比例缩放；高度自动计算。")
            }
        }
    }

    private var pngSection: some View {
        let enabled = model.formats.contains("png")
        return SettingsSection(title: "PNG", trailing: enabled ? nil : "未选择 PNG") {
            SettingRow(title: "分辨率（DPI）") {
                Picker("分辨率", selection: $model.dpi) {
                    Text("300").tag(300)
                    Text("600").tag(600)
                    Text("1200").tag(1200)
                }.labelsHidden().pickerStyle(.segmented).fixedSize()
            }
            SettingRow(title: "背景") {
                Picker("背景", selection: $model.whiteBackground) {
                    Text("透明").tag(false)
                    Text("白色").tag(true)
                }.labelsHidden().pickerStyle(.segmented).fixedSize()
            }
        }
        .disabled(!enabled).opacity(enabled ? 1 : 0.5)
    }

    private var artworkSection: some View {
        SettingsSection(title: "图稿处理") {
            SettingRow(title: "导出画板") {
                Picker("画板", selection: $model.firstArtboard) {
                    Text("全部").tag(false)
                    Text("首个").tag(true)
                }.labelsHidden().pickerStyle(.segmented).fixedSize()
            }
            SettingRow(title: "文字转轮廓") { settingSwitch("文字转轮廓", isOn: $model.outlineText) }
            SettingRow(title: "展开描边与虚线") { settingSwitch("展开描边与虚线", isOn: $model.outlineStrokes) }
            Caption("在副本中预处理，保留原文件。")
        }
    }

    private func settingSwitch(_ title: String, isOn: Binding<Bool>) -> some View {
        Toggle(title, isOn: isOn).labelsHidden().toggleStyle(.switch).controlSize(.small)
    }

    private var actionBar: some View {
        HStack(spacing: 12) {
            Image(systemName: "folder").font(.system(size: 15))
            VStack(alignment: .leading, spacing: 2) {
                Text("保存到").font(.system(size: 11)).foregroundColor(Palette.muted)
                Text(model.outputFolder?.path ?? "原文件旁的 Figure Exports 文件夹")
                    .font(.system(size: 12)).lineLimit(1).truncationMode(.middle)
                    .help(model.outputFolder?.path ?? "原文件旁的 Figure Exports 文件夹")
            }
            SmallLinkButton("更改") { model.pickOutputFolder() }.disabled(model.busy)
            if model.outputFolder != nil {
                SmallLinkButton("还原", color: Palette.muted) { model.outputFolder = nil }.disabled(model.busy)
            }
            Spacer(minLength: 16)
            Toggle("拖入后自动转换", isOn: $model.autoConvert).font(.system(size: 12)).toggleStyle(.checkbox)
                .disabled(model.busy).fixedSize()
            if let hint = model.convertHint {
                Text(hint).font(.system(size: 11)).foregroundColor(Palette.muted).lineLimit(1).fixedSize()
            }
            convertButton
        }
        .padding(.horizontal, 16).frame(height: 62)
        .background(Palette.surface)
    }

    private var convertButton: some View {
        let active = model.canConvert || model.busy
        return Button { model.convert() } label: {
            HStack(spacing: 8) {
                if model.busy { ProgressView().controlSize(.small).scaleEffect(0.7).frame(width: 14, height: 14) }
                Text(model.busy ? "正在转换… 剩 \(model.remainingInRun) 个" : "开始转换\(model.waitingCount > 0 ? " · \(model.waitingCount)" : "")")
                    .font(.system(size: 13, weight: .semibold).monospacedDigit())
                if !model.busy { Image(systemName: "arrow.right").font(.system(size: 11, weight: .semibold)) }
            }
            .frame(minWidth: 126).padding(.horizontal, 14).frame(height: 36)
            .foregroundColor(active ? Palette.onAccent : Palette.muted)
            .background(active ? Palette.accent : Palette.wash)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(!model.canConvert)
        .keyboardShortcut(.return, modifiers: .command)
        .help("开始转换（⌘↩）")
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct FigureConverterApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    var body: some Scene {
        WindowGroup {
            MainView()
        }
        .defaultSize(width: 1040, height: 760)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) { }
        }
    }
}
