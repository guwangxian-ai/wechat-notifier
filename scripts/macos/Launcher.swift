import Cocoa
import Foundation

// A native app remains the responsible process for its Python/osascript children.
// No permissions are granted or requested automatically by this launcher.
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var server: Process?
    private var root: URL?
    private var log: FileHandle?
    private var stopping = false
    private var terminationSignal: DispatchSourceSignal?

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            guard let configuration = Bundle.main.url(forResource: "launcher", withExtension: "json"),
                  let values = try JSONSerialization.jsonObject(with: Data(contentsOf: configuration)) as? [String: String],
                  let project = values["project_root"] else {
                throw NSError(domain: "WeChatNotifier", code: 1)
            }
            let directory = URL(fileURLWithPath: project, isDirectory: true)
            root = directory
            let output = directory.appendingPathComponent(".state/service.log")
            if !FileManager.default.fileExists(atPath: output.path) {
                FileManager.default.createFile(atPath: output.path, contents: nil,
                    attributes: [.posixPermissions: 0o600])
            }
            log = try FileHandle(forWritingTo: output)
            try log?.seekToEnd()
            let process = Process()
            process.executableURL = directory.appendingPathComponent(".venv/bin/python")
            process.arguments = ["-m", "notifier", "--data-dir", directory.appendingPathComponent(".state").path]
            process.currentDirectoryURL = directory
            var environment = ProcessInfo.processInfo.environment
            environment["TZ"] = "Asia/Shanghai"
            environment["PYTHONUNBUFFERED"] = "1"
            environment["WECHAT_NOTIFIER_APP"] = Bundle.main.bundlePath
            process.environment = environment
            process.standardOutput = log
            process.standardError = log
            process.terminationHandler = { [weak self] _ in
                DispatchQueue.main.async {
                    if self?.stopping == true {
                        NSApp.reply(toApplicationShouldTerminate: true)
                    } else {
                        NSApp.terminate(nil)
                    }
                }
            }
            server = process
            try process.run()
            signal(SIGTERM, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
            source.setEventHandler { NSApp.terminate(nil) }
            source.resume()
            terminationSignal = source
            if !CommandLine.arguments.contains("--background") { openDashboard() }
        } catch {
            fputs("WeChatNotifier launcher could not start its service.\n", stderr)
            NSApp.terminate(nil)
        }
    }

    private func openDashboard() {
        guard let directory = root else { return }
        let opener = Process()
        opener.executableURL = directory.appendingPathComponent(".venv/bin/python")
        opener.arguments = [directory.appendingPathComponent("scripts/open_dashboard.py").path]
        opener.currentDirectoryURL = directory
        opener.standardOutput = log
        opener.standardError = log
        do { try opener.run() }
        catch { fputs("WeChatNotifier could not open the dashboard.\n", stderr) }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openDashboard()
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let process = server, process.isRunning else { return .terminateNow }
        if !stopping {
            stopping = true
            process.terminate()
        }
        return .terminateLater
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
