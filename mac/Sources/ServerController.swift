import AppKit
import Foundation
import WebKit

struct Failure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// Starts `timak serve` (as root through `sudo`, so that APFS backups can be mounted), keeps an eye on it, and stops it when the app quits.
///
/// The app chooses the port and the token. The token goes to the server in a file
/// only this user can read, never on a command line other users could see. When the
/// app quits while a command is running, the server can finish it on its own: it
/// exits once nothing is running and no Timak app is around (`--owner-pid`), and
/// a Timak started before then reconnects to it instead of starting a new one.
@MainActor
final class ServerController: ObservableObject {
    enum Phase: Equatable {
        case starting(String)
        case running(URL)
        case failed(String)
    }

    @Published var phase: Phase = .starting("Starting…")
    @Published var asAdministrator: Bool = UserDefaults.standard.object(forKey: "asAdministrator") as? Bool ?? true {
        didSet { UserDefaults.standard.set(asAdministrator, forKey: "asAdministrator") }
    }
    weak var webView: WKWebView?

    let logURL: URL
    private let support: URL
    private let tokenURL: URL
    private let sessionURL: URL
    private var port = 0
    private var token = ""
    private var health: Task<Void, Never>?
    private var lostServer = false  // shown as failed because it stopped answering; health keeps checking
    private var serverProcess: Process?

    private struct Session: Codable { let port: Int }

    init() {
        let fm = FileManager.default
        support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Timak")
        tokenURL = support.appendingPathComponent("token")
        sessionURL = support.appendingPathComponent("session.json")
        logURL = fm.urls(for: .libraryDirectory, in: .userDomainMask)[0].appendingPathComponent("Logs/Timak/server.log")
    }

    var isRunning: Bool {
        if case .running = phase { return true }
        return false
    }

    private var pageURL: URL { URL(string: "http://127.0.0.1:\(port)/#token=\(token)")! }

    // MARK: - starting

    /// Reconnects to a server that is already running (left by an earlier Timak, or
    /// one that stopped answering for a while), and starts one only if there is none:
    /// a second server would not know about the command the first one is running.
    func launch() async {
        if await reconnect() { return }
        await start()
    }

    /// A server left running by an earlier Timak (finishing a command) is reused.
    ///
    /// Starting a new server writes a new token, after which the old one can never be
    /// reached again. So a new one is started only when the old one is surely gone
    /// (nothing listens on its port) or is not ours (it answers, but refuses our token).
    /// A server that is only slow, busy with a command that keeps the disks busy, is
    /// kept: Timak waits for it as it does for a running server that turns slow.
    private func reconnect() async -> Bool {
        guard let data = try? Data(contentsOf: sessionURL),
              let session = try? JSONDecoder().decode(Session.self, from: data),
              let saved = try? String(contentsOf: tokenURL, encoding: .utf8)
        else { return false }
        port = session.port
        token = saved.trimmingCharacters(in: .whitespacesAndNewlines)
        switch await probe(timeout: 30) {
        case .gone, .notOurs:
            return false
        case .answering:
            _ = try? await call("/api/adopt", body: ["pid": Int(getpid())])
            running()
        case .slow:
            running()
            showNotAnswering()
            // adopt as soon as it answers, so that it does not exit when the earlier Timak is gone
            Task { [weak self] in
                while let self, !Task.isCancelled {
                    if (try? await self.call("/api/adopt", body: ["pid": Int(getpid())], timeout: 30)) != nil { return }
                    guard self.isListening(self.port) else { return }
                    try? await Task.sleep(nanoseconds: 10_000_000_000)
                }
            }
        }
        return true
    }

    private enum Probe { case answering, slow, notOurs, gone }

    /// What is at `port`: our server, a slow one, something else, or nothing.
    private func probe(timeout: TimeInterval) async -> Probe {
        do {
            try await call("/api/state", timeout: timeout)
            return .answering
        } catch is Failure {
            return .notOurs  // it answered, but not with 200: wrong token or not timak
        } catch {
            return isListening(port) ? .slow : .gone
        }
    }

    /// Whether something accepts connections on 127.0.0.1:`port` (immediate on localhost).
    nonisolated private func isListening(_ port: Int) -> Bool {
        guard port > 0 else { return false }
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = UInt16(port).bigEndian
        return withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
    }

    private func showNotAnswering() {
        lostServer = true
        phase = .failed("timak is not answering. A command keeping the disks busy can make it slow: "
                        + "Timak keeps trying and comes back by itself as soon as timak answers. "
                        + "The server log may say more.")
    }

    func start() async {
        health?.cancel()
        lostServer = false
        phase = .starting("Starting timak…")
        do {
            let python = try findPython()
            try prepareFiles()
            port = try choosePort()
            try JSONEncoder().encode(Session(port: port)).write(to: sessionURL)
            try await launchServer(python: python)
            try await waitUntilUp()
            running()
        } catch {
            try? FileManager.default.removeItem(at: sessionURL)
            phase = .failed(error.localizedDescription)
        }
    }

    func restart() async {
        guard isRunning else {
            // stopped, or not answering: reuse the server if it is still there, since a new
            // one would get a new token and leave the old one, and its command, unreachable
            await launch()
            return
        }
        if let command = await runningCommand() {
            let alert = NSAlert()
            alert.messageText = "“\(command)” is still running."
            alert.informativeText = "Restarting the server stops it. An extract can be resumed later."
            alert.addButton(withTitle: "Stop and Restart")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        await stopServer(stopJob: true)
        guard !isListening(port) else {  // it did not stop: keep using it rather than lose it
            running()
            return
        }
        await start()
    }

    /// Shows the page and keeps checking the server.
    ///
    /// A server busy with a large extract (disks saturated by rsync) can be slow to
    /// answer, so a few slow answers only show a warning: the checks go on, and the
    /// page comes back by itself as soon as the server answers again. Only a server
    /// process that has exited is reported as stopped.
    private func running() {
        phase = .running(pageURL)
        lostServer = false
        health?.cancel()
        health = Task { [weak self] in
            var failures = 0
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                guard let self, !Task.isCancelled, self.isRunning || self.lostServer else { return }
                if let process = self.serverProcess, !process.isRunning {
                    self.lostServer = false
                    self.phase = .failed("timak stopped unexpectedly. The server log may say why.")
                    return
                }
                if (try? await self.call("/api/state", timeout: 30)) != nil {
                    failures = 0
                    if self.lostServer {
                        self.lostServer = false
                        self.phase = .running(self.pageURL)
                    }
                } else {
                    failures += 1
                    if failures >= 3, self.isRunning {
                        self.showNotAnswering()
                    }
                }
            }
        }
    }

    private func findPython() throws -> String {
        var candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
        if let custom = UserDefaults.standard.string(forKey: "pythonPath") { candidates.insert(custom, at: 0) }
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: path)
            p.arguments = ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"]
            if (try? p.run()) != nil {
                p.waitUntilExit()
                if p.terminationStatus == 0 { return path }
            }
        }
        throw Failure("timak needs Python 3.11 or later. Install it with `brew install python`, or set another one with:\n"
                      + "defaults write \(Bundle.main.bundleIdentifier ?? "Timak") pythonPath /path/to/python3")
    }

    private func prepareFiles() throws {
        let fm = FileManager.default
        try fm.createDirectory(at: support, withIntermediateDirectories: true)
        try fm.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        var bytes = [UInt8](repeating: 0, count: 24)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { throw Failure("no random numbers") }
        token = Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        try? fm.removeItem(at: tokenURL)
        guard fm.createFile(atPath: tokenURL.path, contents: Data(token.utf8), attributes: [.posixPermissions: 0o600]) else {
            throw Failure("Cannot write \(tokenURL.path)")
        }
        // created by us, so a server running as root appends to a file we can read
        let size = (try? fm.attributesOfItem(atPath: logURL.path)[.size] as? Int) ?? 0
        if size > 5_000_000 || !fm.fileExists(atPath: logURL.path) {
            fm.createFile(atPath: logURL.path, contents: nil)
        }
    }

    /// Starts the server as a child of the app, through `sudo` when running as administrator.
    ///
    /// macOS checks Full Disk Access against the app responsible for a process, which a
    /// child keeps: so the server gets Timak's access, as `sudo` in Terminal gets Terminal's.
    /// (The standard administrator prompt of `osascript` would not do: its commands are
    /// started by a system helper and get none of Timak's permissions.)
    private func launchServer(python: String) async throws {
        let resources = Bundle.main.resourceURL!.path
        let server = ["/usr/bin/env", "PYTHONPATH=\(resources)", "PYTHONDONTWRITEBYTECODE=1",  // never write into the signed app
                      python, "-m", "timak", "serve", "--port", "\(port)", "--no-browser",
                      "--token-file", tokenURL.path, "--owner-pid", "\(getpid())"]
        guard asAdministrator else {
            serverProcess = try spawn(server, password: nil)
            return
        }
        var retry = false
        while true {
            guard let password = askPassword(retry: retry) else {
                throw Failure("The password prompt was cancelled. timak needs administrator rights to mount APFS backups and to keep file owners; it can also run without them.")
            }
            // -k: always read the password, so that it never ends up in the server's input;
            // sudo sets SUDO_USER and friends, which tell timak whose home folder to show
            let process = try spawn(["/usr/bin/sudo", "-S", "-k", "-p", ""] + server, password: password)
            // a wrong password ends sudo within a few seconds; a right one starts the server
            for _ in 0..<40 where process.isRunning {
                if (try? await call("/api/state")) != nil { break }
                try await Task.sleep(nanoseconds: 250_000_000)
            }
            if process.isRunning {
                serverProcess = process
                return
            }
            guard logTail(lines: 3).contains("incorrect password") || logTail(lines: 3).contains("try again") else {
                throw Failure("timak did not start.\n\n" + logTail())
            }
            retry = true
        }
    }

    private func spawn(_ argv: [String], password: String?) throws -> Process {
        let log = try FileHandle(forWritingTo: logURL)
        log.seekToEndOfFile()
        defer { try? log.close() }
        let input = Pipe()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: argv[0])
        p.arguments = Array(argv.dropFirst())
        p.currentDirectoryURL = URL(fileURLWithPath: NSHomeDirectory())
        p.standardInput = input
        p.standardOutput = log
        p.standardError = log
        try p.run()
        if let password { input.fileHandleForWriting.write(Data((password + "\n").utf8)) }
        try? input.fileHandleForWriting.close()
        return p
    }

    private func askPassword(retry: Bool) -> String? {
        let alert = NSAlert()
        alert.messageText = retry ? "Wrong password. Try again." : "Timak needs your password."
        alert.informativeText = "timak runs as administrator to mount APFS backups and to keep file owners. "
            + "Enter the password of “\(NSUserName())”."
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }
        return field.stringValue
    }

    private func waitUntilUp() async throws {
        for _ in 0..<60 {
            if (try? await call("/api/state")) != nil { return }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        throw Failure("timak did not start.\n\n" + logTail())
    }

    /// The same port every time when possible: the page keeps its settings per address.
    private func choosePort() throws -> Int {
        let saved = UserDefaults.standard.integer(forKey: "port")
        if saved > 0, (try? freePort(saved)) != nil { return saved }
        let port = try freePort(0)
        UserDefaults.standard.set(port, forKey: "port")
        return port
    }

    private func freePort(_ wanted: Int) throws -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure("no socket") }
        defer { close(fd) }
        var on: Int32 = 1  // like the Python server: a port just released by the last run is reusable
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = UInt16(wanted).bigEndian
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let ok = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, len) == 0 && getsockname(fd, $0, &len) == 0 }
        }
        guard ok else { throw Failure("no free port") }
        return Int(UInt16(bigEndian: addr.sin_port))
    }

    // MARK: - talking to the server

    @discardableResult
    func call(_ path: String, body: [String: Any]? = nil, timeout: TimeInterval = 10) async throws -> [String: Any] {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!, timeoutInterval: timeout)
        request.setValue(token, forHTTPHeaderField: "X-Timak-Token")
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else { throw Failure("timak answered \(status)") }
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    private func runningCommand() async -> String? {
        guard case .some(.some(let command)) = await activeCommand() else { return nil }
        return command
    }

    /// The command the server is running: `.some(nil)` when it runs none, `nil` when it
    /// does not answer, so that a slow server is not mistaken for an idle one.
    private func activeCommand() async -> String?? {
        guard let state = try? await call("/api/state", timeout: 30) else { return nil }
        guard let job = state["job"] as? [String: Any],
              let status = job["status"] as? String, status == "running" || status == "stopping"
        else { return .some(nil) }
        return .some(job["command"] as? String ?? "A command")
    }

    private func stopServer(stopJob: Bool) async {
        health?.cancel()
        _ = try? await call("/api/quit", body: ["stop_job": stopJob])
        for _ in 0..<240 {  // a stopped extract unmounts what it mounted before the server exits
            if !isListening(port) { break }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        if !isListening(port) {  // keep the session of a server still there, so that Timak can find it again
            try? FileManager.default.removeItem(at: sessionURL)
        }
    }

    /// Whether the app may quit now; asks first when a command is running.
    func prepareToQuit() async -> Bool {
        guard isListening(port) else {
            health?.cancel()
            return true
        }
        guard case .some(let active) = await activeCommand() else {
            // not answering: it may be busy with a command, so keep its session and token.
            // A server with nothing to do exits by itself once Timak is gone.
            health?.cancel()
            return true
        }
        guard let command = active else {
            health?.cancel()
            _ = try? await call("/api/quit", body: [:])
            try? FileManager.default.removeItem(at: sessionURL)
            return true
        }
        let alert = NSAlert()
        alert.messageText = "“\(command)” is still running."
        alert.informativeText = "It can keep running in the background and stop by itself when it is done. "
            + "Open Timak again before then to follow it. If you stop it, an extract can be resumed later."
        alert.addButton(withTitle: "Keep Running")
        alert.addButton(withTitle: "Stop and Quit")
        alert.addButton(withTitle: "Cancel")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            health?.cancel()
            return true
        case .alertSecondButtonReturn:
            phase = .starting("Stopping “\(command)”…")
            await stopServer(stopJob: true)
            return true
        default:
            return false
        }
    }

    // MARK: - menu actions

    /// The help page of the running server, in its own window.
    func showHelp() {
        guard isRunning, let url = URL(string: "http://127.0.0.1:\(port)/help.html") else { return }
        HelpWindow.shared.show(url)
    }

    func reloadPage() {
        webView?.reload()
    }

    func showLog() {
        NSWorkspace.shared.open(logURL)
    }

    func logTail(lines: Int = 12) -> String {
        guard let text = try? String(contentsOf: logURL, encoding: .utf8) else { return "" }
        return text.split(separator: "\n").suffix(lines).joined(separator: "\n")
    }
}
