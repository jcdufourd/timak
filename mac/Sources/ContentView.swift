import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var server: ServerController

    var body: some View {
        switch server.phase {
        case .running(let url):
            WebView(url: url, server: server)
        case .starting(let message):
            VStack(spacing: 16) {
                ProgressView()
                Text(message).foregroundStyle(.secondary).multilineTextAlignment(.center)
            }
            .padding(40)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message):
            FailedView(message: message)
        }
    }
}

private struct FailedView: View {
    @EnvironmentObject private var server: ServerController
    let message: String

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("timak is not running", systemImage: "exclamationmark.triangle")
                .font(.title2.weight(.semibold))
            ScrollView {
                Text(message)
                    .font(.system(.body, design: message.contains("\n") ? .monospaced : .default))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 260)
            Toggle("Run as administrator (needed for APFS backups and to keep file owners)", isOn: $server.asAdministrator)
            HStack {
                Button("Start timak") { Task { await server.launch() } }
                    .keyboardShortcut(.defaultAction)
                Button("Show Server Log") { server.showLog() }
            }
        }
        .padding(32)
        .frame(maxWidth: 640)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
