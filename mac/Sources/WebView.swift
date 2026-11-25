import AppKit
import SwiftUI
import WebKit

/// The timak web page, with the native bits a browser would provide: alert and
/// confirm dialogs, and backups dropped onto the window becoming sources.
struct WebView: NSViewRepresentable {
    let url: URL
    let server: ServerController

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.uiDelegate = context.coordinator
        web.navigationDelegate = context.coordinator
        web.setValue(false, forKey: "drawsBackground")  // no white flash in dark mode
        server.webView = web
        load(url, in: web, context: context)
        return web
    }

    func updateNSView(_ web: WKWebView, context: Context) {
        if context.coordinator.loaded != url {  // the server was restarted: new port and token
            load(url, in: web, context: context)
        }
    }

    private func load(_ url: URL, in web: WKWebView, context: Context) {
        context.coordinator.loaded = url
        web.load(URLRequest(url: url))
    }

    final class Coordinator: NSObject, WKUIDelegate, WKNavigationDelegate {
        var loaded: URL?

        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            let alert = NSAlert()
            alert.messageText = message
            alert.runModal()
            completionHandler()
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            let alert = NSAlert()
            alert.messageText = message
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Cancel")
            completionHandler(alert.runModal() == .alertFirstButtonReturn)
        }

        /// A link opening a new window (target="_blank"): the help gets its own window, the rest the browser.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            guard let url = action.request.url else { return nil }
            if url.host == "127.0.0.1" && url.path == "/help.html" {
                MainActor.assumeIsolated { HelpWindow.shared.show(url) }
            } else {
                NSWorkspace.shared.open(url)
            }
            return nil
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else { return decisionHandler(.cancel) }
            if url.isFileURL {  // a disk image or folder dropped onto the window
                decisionHandler(.cancel)
                if let path = try? String(data: JSONEncoder().encode(url.path), encoding: .utf8) {
                    webView.evaluateJavaScript("addSource(\(path)); showTab('snapshots');")
                }
                return
            }
            if url.host == "127.0.0.1" {
                return decisionHandler(.allow)
            }
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }
}
