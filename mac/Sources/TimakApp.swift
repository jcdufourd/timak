import AppKit
import SwiftUI

@main
struct TimakApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        Window("Timak", id: "main") {
            ContentView()
                .environmentObject(delegate.server)
                .frame(minWidth: 760, minHeight: 560)
        }
        .defaultSize(width: 1180, height: 860)
        .commands {
            CommandGroup(replacing: .appInfo) {
                Button("About Timak") { AboutPanel.show() }
            }
            CommandGroup(after: .toolbar) {
                Button("Reload Page") { delegate.server.reloadPage() }
                    .keyboardShortcut("r")
            }
            CommandGroup(replacing: .help) {
                Button("Timak Help") { delegate.server.showHelp() }
                    .keyboardShortcut("?")
            }
            CommandMenu("Server") {
                Button("Restart Server") { Task { await delegate.server.restart() } }
                Button("Show Server Log") { delegate.server.showLog() }
            }
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let server = ServerController()

    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { await server.launch() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard server.isRunning else { return .terminateNow }
        Task {
            let quit = await server.prepareToQuit()
            sender.reply(toApplicationShouldTerminate: quit)
        }
        return .terminateLater
    }
}
