import SwiftUI
import AppKit
import UniformTypeIdentifiers

private enum Palette {
    static let paper = Color(white: 0.975)
    static let ink = Color(white: 0.10)
    static let accent = Color(white: 0.08)
    static let muted = Color(white: 0.43)
    static let border = Color(white: 0.84)
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

    func probe() {
        probing = true
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let response = try Backend.run(["probe"])
                let result = try Backend.decode(ProbeResult.self, from: response.data)
                DispatchQueue.main.async {
                    self.illustratorAvailable = result.illustrator.available
                    if !result.illustrator.available {
                        self.notice = "未检测到 Adobe Illustrator。请安装并完成首次启动后，点“重新检测”。"
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

private struct FileDropTarget: NSViewRepresentable {
    var onDrop: ([URL]) -> Void
    @Binding var highlighted: Bool

    func makeNSView(context: Context) -> DropView {
        let view = DropView()
        view.onDrop = onDrop
        view.onHighlight = { highlighted = $0 }
        view.registerForDraggedTypes([.fileURL])
        return view
    }

    func updateNSView(_ view: DropView, context: Context) {
        view.onDrop = onDrop
        view.onHighlight = { highlighted = $0 }
    }

    final class DropView: NSView {
        var onDrop: (([URL]) -> Void)?
        var onHighlight: ((Bool) -> Void)?

        override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
            let accepts = sender.draggingPasteboard.canReadObject(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true])
            onHighlight?(accepts)
            return accepts ? .copy : []
        }

        override func draggingExited(_ sender: NSDraggingInfo?) { onHighlight?(false) }
        override func draggingEnded(_ sender: NSDraggingInfo) { onHighlight?(false) }
        override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
            onHighlight?(false)
            let urls = sender.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
            guard !urls.isEmpty else { return false }
            onDrop?(urls)
            return true
        }
    }
}

private struct FormatCard: View {
    let title: String
    let subtitle: String
    let icon: String
    let selected: Bool
    let available: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: icon).font(.system(size: 20, weight: .medium)).frame(width: 27)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.system(size: 14, weight: .semibold))
                    Text(subtitle).font(.system(size: 11)).foregroundColor(Palette.muted)
                }
                Spacer()
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 18)).foregroundColor(selected ? Palette.accent : Palette.border)
            }
            .foregroundColor(Palette.ink)
            .padding(.horizontal, 14).padding(.vertical, 12)
            .background(selected ? Palette.accent.opacity(0.065) : Color.white)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(selected ? Palette.accent.opacity(0.75) : Palette.border, lineWidth: selected ? 1.4 : 1))
        }
        .buttonStyle(.plain).disabled(!available).opacity(available ? 1 : 0.45)
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

    var statusColor: Color {
        switch job.state {
        case .done: return Palette.accent
        case .failed: return Palette.ink
        default: return Palette.muted
        }
    }

    var statusSymbol: String {
        switch job.state {
        case .done: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.circle.fill"
        case .waiting: return "clock"
        case .processing: return ""
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 12) {
                Text(job.url.pathExtension.uppercased())
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundColor(Palette.accent).frame(width: 40, height: 44)
                    .background(Palette.accent.opacity(0.075)).clipShape(RoundedRectangle(cornerRadius: 8))
                VStack(alignment: .leading, spacing: 4) {
                    Text(job.url.lastPathComponent).font(.system(size: 13, weight: .medium)).lineLimit(1).help(job.url.path)
                    Text(!job.outputs.isEmpty ? "已导出 \(job.outputs.count) 个文件" : job.url.deletingLastPathComponent().lastPathComponent)
                        .font(.system(size: 11)).foregroundColor(Palette.muted).lineLimit(1)
                }
                Spacer(minLength: 4)
                if job.state == .processing { ProgressView().controlSize(.small).scaleEffect(0.8) }
                HStack(spacing: 4) {
                    if !statusSymbol.isEmpty { Image(systemName: statusSymbol) }
                    Text(job.state == .failed && !job.outputs.isEmpty ? "部分完成" : job.state.label)
                }
                .font(.system(size: 11, weight: job.state == .failed ? .semibold : .medium))
                .foregroundColor(statusColor)
                if !job.outputs.isEmpty {
                    Button(action: onReveal) { Image(systemName: "folder").font(.system(size: 13)) }
                        .buttonStyle(.plain).help("在访达中显示导出文件")
                }
                if !busy {
                    Button(action: onRemove) { Image(systemName: "xmark").font(.system(size: 10)) }
                        .buttonStyle(.plain).foregroundColor(Palette.muted).help("从列表移除")
                }
            }
            if let error = job.error, !error.isEmpty {
                HStack(alignment: .top, spacing: 7) {
                    Image(systemName: "exclamationmark.triangle.fill").padding(.top, 1)
                    Text(error).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
                .font(.system(size: 11, weight: .medium)).foregroundColor(Palette.ink)
                .padding(9).frame(maxWidth: .infinity, alignment: .leading)
                .background(Palette.accent.opacity(0.055)).clipShape(RoundedRectangle(cornerRadius: 7))
            }
            if let firstSize = sizeSummaries.first {
                Text(firstSize + (sizeSummaries.count > 1 ? " · 另有 \(sizeSummaries.count - 1) 个画板" : ""))
                    .font(.system(size: 11)).foregroundColor(Palette.accent).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !job.warnings.isEmpty || job.outputDirectory != nil {
                Button(action: onToggleDetails) {
                    HStack(spacing: 5) {
                        Image(systemName: detailsExpanded ? "chevron.down" : "chevron.right").font(.system(size: 9, weight: .semibold))
                        Text(job.warnings.isEmpty ? "转换详情" : "有 \(job.warnings.count) 条说明")
                    }.font(.system(size: 11)).foregroundColor(Palette.muted)
                }.buttonStyle(.plain)
                if detailsExpanded {
                    VStack(alignment: .leading, spacing: 8) {
                        if sizeSummaries.count > 1 {
                            ForEach(sizeSummaries, id: \.self) { size in
                                Text(size).font(.system(size: 11)).foregroundColor(Palette.muted).textSelection(.enabled)
                            }
                        }
                        ForEach(Array(job.warnings.enumerated()), id: \.offset) { _, warning in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: "info.circle").padding(.top, 1)
                                Text(warning).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                            }.font(.system(size: 11)).foregroundColor(Palette.muted)
                        }
                        if job.outputDirectory != nil {
                            Button("查看转换报告", action: onOpenReport).buttonStyle(.plain)
                                .font(.system(size: 11)).foregroundColor(Palette.accent)
                        }
                    }.padding(.leading, 14).padding(.top, 3)
                }
            }
        }
        .padding(14).background(Color.white).clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(job.state == .failed ? Palette.accent.opacity(0.55) : Palette.border.opacity(0.6), lineWidth: 1))
    }
}

private struct MainView: View {
    @StateObject private var model = ConverterModel()

    var body: some View {
        VStack(spacing: 0) {
            header
            HStack(alignment: .top, spacing: 24) {
                filesPanel
                settingsPanel.frame(width: 300)
            }.padding(.horizontal, 30).padding(.bottom, 20)
            footer
        }
        .frame(minWidth: 960, minHeight: 720)
        .background(Palette.paper).foregroundColor(Palette.ink)
        .tint(Palette.accent).preferredColorScheme(.light)
        .onAppear { model.probe() }
        .onOpenURL { model.add([$0]) }
    }

    private var header: some View {
        HStack(spacing: 14) {
            Image(systemName: "point.topleft.down.to.point.bottomright.curvepath")
                .font(.system(size: 26, weight: .medium)).foregroundColor(.white)
                .frame(width: 51, height: 51).background(Palette.accent).clipShape(RoundedRectangle(cornerRadius: 15))
            VStack(alignment: .leading, spacing: 5) {
                Text("Figure Converter").font(.system(size: 25, weight: .semibold, design: .rounded))
                Text("科研插图，一次导出。")
                    .font(.system(size: 12)).foregroundColor(Palette.muted)
            }
            Spacer()
            Label("本地处理", systemImage: "desktopcomputer")
                .font(.system(size: 11, weight: .medium)).foregroundColor(Palette.accent)
                .padding(.horizontal, 12).padding(.vertical, 7)
                .background(Palette.accent.opacity(0.07)).clipShape(Capsule())
        }.padding(.horizontal, 30).padding(.top, 25).padding(.bottom, 26)
    }

    private var filesPanel: some View {
        VStack(alignment: .leading, spacing: 15) {
            HStack {
                Text("01").font(.system(size: 11, weight: .semibold, design: .monospaced)).foregroundColor(Palette.accent)
                Text("添加文件").font(.system(size: 14, weight: .semibold))
                Spacer()
                if model.doneCount > 0 {
                    Button("再次导出") { model.requeueCompleted() }.font(.system(size: 11)).buttonStyle(.plain)
                        .foregroundColor(Palette.accent).disabled(model.busy)
                }
                if !model.jobs.isEmpty {
                    Button("清空列表") { model.jobs.removeAll() }.font(.system(size: 11)).buttonStyle(.plain)
                        .foregroundColor(Palette.muted).disabled(model.busy)
                }
            }
            ZStack {
                RoundedRectangle(cornerRadius: 17).fill(model.dragHighlighted ? Palette.accent.opacity(0.10) : Color.white.opacity(0.72))
                RoundedRectangle(cornerRadius: 17).stroke(Palette.accent.opacity(model.dragHighlighted ? 0.8 : 0.35), style: StrokeStyle(lineWidth: 1.3, dash: [6, 5]))
                VStack(spacing: 9) {
                    Image(systemName: "square.and.arrow.down").font(.system(size: 29, weight: .light)).foregroundColor(Palette.accent)
                    Text(model.dragHighlighted ? "松开，即可加入列表" : "把图拖到这里")
                        .font(.system(size: 19, weight: .medium))
                    Text("AI · EPS · SVG · PDF  /  支持批量添加")
                        .font(.system(size: 11)).foregroundColor(Palette.muted)
                }.allowsHitTesting(false)
                FileDropTarget(onDrop: { model.add($0) }, highlighted: $model.dragHighlighted)
            }.frame(height: 164)
            HStack {
                Button { model.pickFiles() } label: { Label("选择文件", systemImage: "plus") }
                    .buttonStyle(.bordered).controlSize(.small)
                Spacer()
                Toggle("拖入后自动转换", isOn: $model.autoConvert).font(.system(size: 11)).toggleStyle(.checkbox)
                    .disabled(model.busy)
            }
            if let notice = model.notice {
                HStack(alignment: .top, spacing: 7) {
                    Image(systemName: "info.circle")
                    Text(notice).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }.font(.system(size: 11)).foregroundColor(Palette.muted)
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.white.opacity(0.8)).clipShape(RoundedRectangle(cornerRadius: 9))
            }
            if model.jobs.isEmpty {
                VStack(spacing: 10) {
                    HStack(spacing: 14) {
                        sampleMark("AI", color: Palette.accent)
                        Image(systemName: "arrow.right").foregroundColor(Palette.muted.opacity(0.6))
                        sampleMark("SVG", color: Palette.accent)
                        sampleMark("EMF", color: Palette.accent)
                        sampleMark("PNG", color: Palette.accent)
                        sampleMark("PDF", color: Palette.accent)
                        sampleMark("EPS", color: Palette.accent)
                    }
                    Text("选好输出格式，一次导出多个版本")
                        .font(.system(size: 12)).foregroundColor(Palette.muted)
                    Text("原文件保留，结果另存。")
                        .font(.system(size: 11)).foregroundColor(Palette.muted.opacity(0.8))
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                HStack {
                    Text("文件列表").font(.system(size: 11, weight: .medium)).foregroundColor(Palette.muted)
                    Spacer()
                    Text("\(model.jobs.count) 个文件 · \(model.doneCount) 个已完成").font(.system(size: 11)).foregroundColor(Palette.muted)
                }
                ScrollView {
                    LazyVStack(spacing: 9) {
                        ForEach(model.jobs) { job in
                            JobRow(job: job, busy: model.busy,
                                   onRemove: { model.remove(job.id) }, onReveal: { model.reveal(job) },
                                   detailsExpanded: model.expandedDetails.contains(job.id),
                                   onToggleDetails: { model.toggleDetails(job.id) }, onOpenReport: { model.openReport(job) })
                        }
                    }.padding(.trailing, 2)
                }
            }
        }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func sampleMark(_ text: String, color: Color) -> some View {
        Text(text).font(.system(size: 11, weight: .semibold, design: .monospaced))
            .foregroundColor(color).frame(width: 49, height: 57)
            .background(Color.white.opacity(0.7)).clipShape(RoundedRectangle(cornerRadius: 9))
            .overlay(RoundedRectangle(cornerRadius: 9).stroke(Palette.border.opacity(0.6)))
    }

    private var settingsPanel: some View {
        ScrollView {
          VStack(alignment: .leading, spacing: 13) {
            HStack(spacing: 7) {
                Text("02").font(.system(size: 11, weight: .semibold, design: .monospaced)).foregroundColor(Palette.accent)
                Text("输出格式").font(.system(size: 14, weight: .semibold))
                Spacer()
                Text("五种格式 · 可多选").font(.system(size: 11)).foregroundColor(Palette.muted)
            }
            VStack(spacing: 8) {
                formatCard("svg", title: "SVG", subtitle: "保留矢量，适合继续排版", icon: "point.3.connected.trianglepath.dotted")
                formatCard("emf", title: "EMF", subtitle: "用于 Word 与 PowerPoint", icon: "doc.richtext", available: model.illustratorAvailable)
                formatCard("png", title: "PNG", subtitle: "高分辨率，支持透明背景", icon: "photo")
                formatCard("pdf", title: "PDF", subtitle: "用于投稿、出版与打印", icon: "doc.text")
                formatCard("eps", title: "EPS", subtitle: "适合传统期刊投稿", icon: "doc.plaintext")
            }.disabled(model.busy)
            if model.formats.contains("emf") {
                Text("EMF 中的渐变或透明效果可能变为局部图片。")
                    .font(.system(size: 10)).foregroundColor(Palette.muted).fixedSize(horizontal: false, vertical: true)
            }
            if model.formats.contains("eps") {
                Text("EPS 会合并透明效果，部分区域可能转为图片。")
                    .font(.system(size: 10)).foregroundColor(Palette.muted).fixedSize(horizontal: false, vertical: true)
            }
            Divider().padding(.vertical, 2)
            VStack(alignment: .leading, spacing: 10) {
                Text("裁剪与尺寸").font(.system(size: 12, weight: .semibold))
                Toggle("裁剪画板白边", isOn: $model.trimArtboard).toggleStyle(.checkbox).font(.system(size: 12))
                Text("画板缩到图形边界，不会删除图片内部的白色区域。")
                    .font(.system(size: 10)).foregroundColor(Palette.muted).fixedSize(horizontal: false, vertical: true)
                Toggle("自定义宽度", isOn: $model.customWidthEnabled).toggleStyle(.checkbox).font(.system(size: 12))
                if model.customWidthEnabled {
                    HStack(spacing: 8) {
                        Text("宽度").font(.system(size: 12))
                        TextField("180", text: $model.widthMMText).textFieldStyle(.roundedBorder)
                            .multilineTextAlignment(.trailing).frame(width: 88)
                            .accessibilityLabel("导出宽度，单位毫米")
                        Text("mm").font(.system(size: 11)).foregroundColor(Palette.muted)
                        Spacer(minLength: 0)
                        Image(systemName: "lock.fill").font(.system(size: 10)).foregroundColor(Palette.accent)
                        Text("等比例").font(.system(size: 10)).foregroundColor(Palette.accent)
                    }
                    if let message = model.widthValidationMessage {
                        HStack(alignment: .top, spacing: 5) {
                            Image(systemName: "exclamationmark.circle.fill").padding(.top, 1)
                            Text(message).fixedSize(horizontal: false, vertical: true)
                        }.font(.system(size: 10, weight: .semibold)).foregroundColor(Palette.ink)
                    } else if let width = model.requestedWidthMM, model.formats.contains("png") {
                        Text("PNG 宽约 \(Int((width / 25.4 * Double(model.dpi)).rounded())) px（\(model.dpi) DPI）")
                            .font(.system(size: 10)).foregroundColor(Palette.muted)
                    }
                    Text(model.trimArtboard ? "先裁掉画板空白，再按宽度等比例缩放；高度自动计算。" : "按当前画板比例缩放；高度自动计算。")
                        .font(.system(size: 10)).foregroundColor(Palette.muted).fixedSize(horizontal: false, vertical: true)
                }
            }.disabled(model.busy)
            Divider().padding(.vertical, 2)
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("PNG 分辨率").font(.system(size: 12, weight: .medium))
                    Spacer()
                    Picker("分辨率", selection: $model.dpi) {
                        Text("300 DPI").tag(300)
                        Text("600 DPI").tag(600)
                        Text("1200 DPI").tag(1200)
                    }.labelsHidden().frame(width: 116)
                }.disabled(!model.formats.contains("png"))
                HStack {
                    Text("PNG 背景").font(.system(size: 12, weight: .medium))
                    Spacer()
                    Picker("背景", selection: $model.whiteBackground) {
                        Text("透明").tag(false)
                        Text("白色").tag(true)
                    }.labelsHidden().pickerStyle(.segmented).frame(width: 116)
                }.disabled(!model.formats.contains("png"))
                HStack {
                    Text("导出画板").font(.system(size: 12, weight: .medium))
                    Spacer()
                    Picker("画板", selection: $model.firstArtboard) {
                        Text("全部").tag(false)
                        Text("首个").tag(true)
                    }.labelsHidden().pickerStyle(.segmented).frame(width: 116)
                }
                Toggle("文字转轮廓", isOn: $model.outlineText).toggleStyle(.checkbox).font(.system(size: 12))
                Toggle("展开描边与虚线", isOn: $model.outlineStrokes).toggleStyle(.checkbox).font(.system(size: 12))
            }.disabled(model.busy)
            Text("在副本中预处理，保留原文件。")
                .font(.system(size: 10)).foregroundColor(Palette.muted)
          }.padding(.trailing, 3).padding(.bottom, 8)
        }.frame(maxHeight: .infinity, alignment: .topLeading)
    }

    private func formatCard(_ id: String, title: String, subtitle: String, icon: String, available: Bool = true) -> some View {
        FormatCard(title: title, subtitle: subtitle, icon: icon, selected: model.formats.contains(id), available: available) {
            if model.formats.contains(id) { model.formats.remove(id) } else { model.formats.insert(id) }
        }
    }

    private var footer: some View {
        VStack(spacing: 13) {
            Divider()
            HStack(spacing: 12) {
                Image(systemName: "folder").font(.system(size: 16)).foregroundColor(Palette.accent)
                VStack(alignment: .leading, spacing: 3) {
                    Text("保存到").font(.system(size: 10)).foregroundColor(Palette.muted)
                    Text(model.outputFolder?.path ?? "原文件旁的 Figure Exports 文件夹")
                        .font(.system(size: 11)).lineLimit(1).truncationMode(.middle)
                }
                Button("更改") { model.pickOutputFolder() }.font(.system(size: 11)).buttonStyle(.plain)
                    .foregroundColor(Palette.accent).disabled(model.busy)
                if model.outputFolder != nil {
                    Button("还原") { model.outputFolder = nil }.font(.system(size: 10)).buttonStyle(.plain)
                        .foregroundColor(Palette.muted).disabled(model.busy)
                }
                Spacer()
                Button { model.convert() } label: {
                    HStack(spacing: 8) {
                        if model.busy { ProgressView().controlSize(.small).tint(.white) }
                        Text(model.busy ? "正在转换…" : "开始转换\(model.waitingCount > 0 ? " · \(model.waitingCount)" : "")")
                            .font(.system(size: 13, weight: .semibold))
                        if !model.busy { Image(systemName: "arrow.up.right").font(.system(size: 12, weight: .semibold)) }
                    }.frame(minWidth: 125).padding(.horizontal, 15).padding(.vertical, 13)
                        .foregroundColor(.white).background(model.canConvert || model.busy ? Palette.accent : Palette.muted.opacity(0.4))
                        .clipShape(RoundedRectangle(cornerRadius: 11))
                }.buttonStyle(.plain).disabled(!model.canConvert)
            }
            HStack(spacing: 5) {
                Circle().fill(model.illustratorAvailable ? Palette.accent : Palette.muted).frame(width: 5, height: 5)
                Text(model.probing ? "正在检查转换环境…" : model.illustratorAvailable ? "Illustrator 已就绪 · 五种格式本地导出" : "Illustrator 尚未就绪")
                    .font(.system(size: 10)).foregroundColor(Palette.muted)
                if !model.probing && !model.illustratorAvailable {
                    Button("重新检测") { model.probe() }.buttonStyle(.plain).font(.system(size: 10)).foregroundColor(Palette.accent)
                }
                Spacer()
                Text("自定义尺寸保持长宽比 · PNG 支持高分辨率")
                    .font(.system(size: 10)).foregroundColor(Palette.muted)
            }
        }.padding(.horizontal, 30).padding(.bottom, 18)
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
