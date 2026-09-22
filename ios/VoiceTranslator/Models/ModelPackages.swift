import Foundation

/// 模型包（`.vtmodel`）的导入、校验与激活。
///
/// 逐条对齐 Android `ModelPackages.java`：
/// schemaVersion / kind / engine / id / languages / files / licenses 的校验规则，
/// 路径安全判定，"未声明文件即拒绝"，大小与 SHA-256 双重校验，
/// 原子激活（staging → rename），以及**同一 kind 只保留一个已安装包**。
///
/// 存储位置按移植设计书 §5.3：`Application Support/Models/<id>-<uuid>`。
enum ModelPackages {

    static let maxPackageSize: Int64 = 3 * 1024 * 1024 * 1024
    static let maxManifestSize = 131_072
    private static let reservedFreeSpace: Int64 = 64 * 1024 * 1024

    enum Kind: String {
        case asr
        case translation

        var pathKey: String { rawValue + "Path" }
        var nameKey: String { rawValue + "Name" }
        /// 该类型允许的引擎（对应 Android 的 supported 判定）。
        var allowedEngines: Set<String> {
            switch self {
            case .asr: return ["sherpa-sensevoice"]
            case .translation: return ["llama-qwen2", "llama-gemma3"]
            }
        }
        /// manifest 必须声明的文件。
        var requiredFiles: [String] {
            switch self {
            case .asr: return ["model.int8.onnx", "tokens.txt", "silero_vad.onnx"]
            case .translation: return ["model.gguf"]
            }
        }
    }

    struct FileSpec {
        let path: String
        let size: Int64
        let sha256: String
    }

    struct Manifest {
        let schemaVersion: Int
        let kind: String
        let engine: String
        let id: String
        let name: String
        let languages: [String]
        let files: [FileSpec]
    }

    // MARK: - 存储位置

    /// 已激活的模型包目录（设计书 §5.3：`Application Support/Models/<id>-<uuid>`）。
    static var modelsRoot: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("Models", isDirectory: true)
    }

    /// 导入过程中的临时文件目录（设计书 §5.3：`Library/Caches/ModelImports`）。
    ///
    /// 刻意放 Caches 而不是 Application Support：模型包最大 3GB，
    /// 万一导入到一半被杀进程（设计书 §9 的发布测试项），
    /// 残留的临时文件应该由系统负责回收，而不是永久占着用户空间。
    static var importsCache: URL {
        let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("ModelImports", isDirectory: true)
    }

    /// 清掉上次异常退出留下的副本与 staging 目录。
    ///
    /// 只匹配我们自己产生的前缀 `import-` / `stage-`，
    /// **不会**碰到已激活的 `<id>-<uuid>` 包——那些只由 `removeInstalled` 清理。
    private static func purgeStaleScratch() {
        let fileManager = FileManager.default
        for directory in [importsCache, modelsRoot] {
            guard let names = try? fileManager.contentsOfDirectory(atPath: directory.path) else { continue }
            for name in names where name.hasPrefix("import-") || name.hasPrefix("stage-") {
                try? fileManager.removeItem(at: directory.appendingPathComponent(name))
            }
        }
    }

    // MARK: - 已选中的模型包

    /// 对应 `ModelPackages.selected(ctx, kind)`：路径有效且含 manifest.json 才认。
    static func selected(_ kind: Kind) -> URL? {
        let store = UserDefaults.standard
        guard let path = store.string(forKey: kind.pathKey), !path.isEmpty else { return nil }
        let url = URL(fileURLWithPath: path)
        let manifest = url.appendingPathComponent("manifest.json")
        return FileManager.default.fileExists(atPath: manifest.path) ? url : nil
    }

    static func displayName(_ kind: Kind) -> String {
        UserDefaults.standard.string(forKey: kind.nameKey) ?? "未导入模型包"
    }

    static func engine(of directory: URL) throws -> TranslationEngineKind {
        let manifest = try readManifest(at: directory)
        guard let kind = TranslationEngineKind(rawValue: manifest.engine) else {
            throw ModelPackageError.invalidManifest("不支持的翻译模型引擎")
        }
        return kind
    }

    static func readManifest(at directory: URL) throws -> Manifest {
        let url = directory.appendingPathComponent("manifest.json")
        guard let data = try? Data(contentsOf: url) else {
            throw ModelPackageError.invalidManifest("缺少 manifest.json")
        }
        return try parseManifest(data)
    }

    // MARK: - 校验

    static func parseManifest(_ data: Data) throws -> Manifest {
        guard data.count <= maxManifestSize else {
            throw ModelPackageError.invalidManifest("清单过大")
        }
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ModelPackageError.invalidManifest("清单不是合法 JSON")
        }
        guard let schemaVersion = root["schemaVersion"] as? Int, schemaVersion == 1,
              let kind = root["kind"] as? String,
              let engine = root["engine"] as? String,
              let id = root["id"] as? String,
              let languages = root["languages"] as? [String],
              let filesRaw = root["files"] as? [[String: Any]] else {
            throw ModelPackageError.invalidManifest("清单字段缺失")
        }
        guard id.range(of: "^[a-z0-9][a-z0-9._-]{0,79}$", options: .regularExpression) != nil else {
            throw ModelPackageError.invalidManifest("模型包 ID 无效")
        }
        guard languages.count == 4, Set(languages) == Set(["zh", "ja", "ko", "en"]) else {
            throw ModelPackageError.invalidManifest("模型包必须支持中日韩英四语")
        }

        var files: [FileSpec] = []
        var seen = Set<String>()
        var total: Int64 = 0
        var hasLicense = false
        for raw in filesRaw {
            guard let path = raw["path"] as? String,
                  let size = (raw["size"] as? NSNumber)?.int64Value,
                  let sha = raw["sha256"] as? String else {
                throw ModelPackageError.invalidManifest("模型清单无效")
            }
            guard safePath(path), path != "manifest.json", !seen.contains(path),
                  size >= 0, size <= maxPackageSize,
                  sha.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else {
                throw ModelPackageError.invalidManifest("模型清单无效")
            }
            seen.insert(path)
            total += size
            if path.hasPrefix("licenses/") { hasLicense = true }
            files.append(FileSpec(path: path, size: size, sha256: sha))
        }
        guard hasLicense else { throw ModelPackageError.invalidManifest("缺少许可文件") }
        guard total <= maxPackageSize else { throw ModelPackageError.invalidManifest("模型过大") }

        return Manifest(schemaVersion: schemaVersion,
                        kind: kind,
                        engine: engine,
                        id: id,
                        name: (root["name"] as? String) ?? id,
                        languages: languages,
                        files: files)
    }

    /// 对应 `ModelPackages.safe`。
    static func safePath(_ name: String) -> Bool {
        VTSafeArchivePath(name)
    }

    // MARK: - 导入

    /// 从用户选择的文件导入模型包。返回已激活的目录。
    static func install(from source: URL,
                        kind: Kind,
                        progress: @escaping (String) -> Void) throws -> URL {
        let fileManager = FileManager.default
        let base = modelsRoot
        if !fileManager.fileExists(atPath: base.path) {
            try fileManager.createDirectory(at: base, withIntermediateDirectories: true)
        }

        // 1) 复制到临时文件（安全作用域 URL 必须拷出来再处理）。
        progress("正在读取模型包…")
        let scratchDir = importsCache
        if !fileManager.fileExists(atPath: scratchDir.path) {
            try fileManager.createDirectory(at: scratchDir, withIntermediateDirectories: true)
        }
        purgeStaleScratch()
        let scratch = scratchDir.appendingPathComponent("import-\(UUID().uuidString).zip")
        defer { try? fileManager.removeItem(at: scratch) }

        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        guard let input = try? FileHandle(forReadingFrom: source) else {
            throw ModelPackageError.ioFailure("无法打开文件")
        }
        defer { try? input.close() }

        try? fileManager.removeItem(at: scratch)
        guard fileManager.createFile(atPath: scratch.path, contents: nil) else {
            throw ModelPackageError.ioFailure("无法创建临时文件")
        }
        let output = try FileHandle(forWritingTo: scratch)
        var copied: Int64 = 0
        while true {
            let chunk = try input.read(upToCount: 1 << 20) ?? Data()
            if chunk.isEmpty { break }
            copied += Int64(chunk.count)
            if copied > maxPackageSize {
                try? output.close()
                throw ModelPackageError.packageTooLarge
            }
            try output.write(contentsOf: chunk)
        }
        try output.close()

        // 2) 解析清单。
        //    注意：ObjC 的 `readEntryNamed:error:` 导入 Swift 后是
        //    `readEntryNamed(_:) throws -> Data`（nullable + NSError** 会被
        //    折成 throws 非可选），所以这里用 do/catch 而不是 guard let。
        let archive = try VTZipArchive(atPath: scratch.path)
        let manifestData: Data
        do {
            manifestData = try archive.readEntryNamed("manifest.json")
        } catch {
            throw ModelPackageError.invalidManifest(
                "缺少有效 manifest.json，请选择 .vtmodel 模型包（\(error.localizedDescription)）")
        }
        let manifest = try parseManifest(manifestData)

        // 3) 类型与引擎匹配。
        guard manifest.kind == kind.rawValue else {
            throw ModelPackageError.invalidManifest("模型包类型不符")
        }
        guard kind.allowedEngines.contains(manifest.engine) else {
            throw ModelPackageError.invalidManifest("暂不支持此模型引擎")
        }
        for required in kind.requiredFiles where !manifest.files.contains(where: { $0.path == required }) {
            throw ModelPackageError.invalidManifest("缺少 \(required)")
        }

        // 4) 压缩包内容必须是"已声明文件的子集 + manifest.json"。
        let declared = Set<String>(manifest.files.map { $0.path })
        var seen = Set<String>()
        for entry in archive.entryNames {
            guard safePath(entry), !seen.contains(entry) else {
                throw ModelPackageError.unsafeEntry(entry)
            }
            seen.insert(entry)
            guard entry == "manifest.json" || declared.contains(entry) else {
                throw ModelPackageError.unsafeEntry(entry)
            }
        }
        guard seen.count == declared.count + 1 else {
            throw ModelPackageError.invalidManifest("模型包文件不完整")
        }

        // 5) 空间检查。
        let total = manifest.files.reduce(Int64(0)) { $0 + $1.size }
        if let free = availableSpace(at: base), free < total + reservedFreeSpace {
            throw ModelPackageError.notEnoughSpace(total)
        }

        // 6) 解包到 staging 并逐个校验。
        let staging = base.appendingPathComponent("stage-\(UUID().uuidString)")
        try fileManager.createDirectory(at: staging, withIntermediateDirectories: true)
        do {
            for spec in manifest.files {
                progress("正在校验 \(spec.path)…")
                let destination = staging.appendingPathComponent(spec.path)
                let parent = destination.deletingLastPathComponent()
                if !fileManager.fileExists(atPath: parent.path) {
                    try fileManager.createDirectory(at: parent, withIntermediateDirectories: true)
                }
                try archive.extractEntryNamed(spec.path,
                                              toFile: destination.path,
                                              expectedSize: UInt64(spec.size),
                                              expectedSHA256: spec.sha256)
            }
            try manifestData.write(to: staging.appendingPathComponent("manifest.json"))

            // 7) 原子激活。
            let installed = base.appendingPathComponent("\(manifest.id)-\(UUID().uuidString)")
            try fileManager.moveItem(at: staging, to: installed)

            // 8) 记录新包，再清理旧包。
            let previous = selected(kind)
            let store = UserDefaults.standard
            store.set(installed.path, forKey: kind.pathKey)
            store.set(manifest.name, forKey: kind.nameKey)

            if let previous,
               previous.path.hasPrefix(base.path + "/"),
               previous.path != installed.path {
                try? fileManager.removeItem(at: previous)
            }
            progress("模型包已导入")
            return installed
        } catch {
            try? fileManager.removeItem(at: staging)
            throw error
        }
    }

    /// 删除当前已选中的模型包（设置页可用的维护动作）。
    static func removeInstalled(_ kind: Kind) throws {
        guard let current = selected(kind) else { return }
        try FileManager.default.removeItem(at: current)
        let store = UserDefaults.standard
        store.removeObject(forKey: kind.pathKey)
        store.removeObject(forKey: kind.nameKey)
    }

    private static func availableSpace(at url: URL) -> Int64? {
        guard let values = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
              let capacity = values.volumeAvailableCapacityForImportantUsage else {
            return nil
        }
        return Int64(capacity)
    }
}

enum ModelPackageError: LocalizedError {
    case invalidManifest(String)
    case unsafeEntry(String)
    case packageTooLarge
    case notEnoughSpace(Int64)
    case ioFailure(String)

    var errorDescription: String? {
        switch self {
        case .invalidManifest(let reason): return reason
        case .unsafeEntry(let name): return "压缩包存在未声明或危险路径：\(name)"
        case .packageTooLarge: return "模型包超过 3GB"
        case .notEnoughSpace(let total):
            return "存储空间不足，解包需要 \(total / 1024 / 1024) MB"
        case .ioFailure(let reason): return reason
        }
    }
}
