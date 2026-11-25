import AppKit
import WebKit

/// The help page (served by timak at /help.html) in a window of its own, beside the main one.
@MainActor
final class HelpWindow: NSObject, WKNavigationDelegate {
    static let shared = HelpWindow()

    private var window: NSWindow?
    private var web: WKWebView?

    func show(_ url: URL) {
        if window == nil {
            let web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
            web.navigationDelegate = self
            web.setValue(false, forKey: "drawsBackground")
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 820, height: 860),
                                  styleMask: [.titled, .closable, .miniaturizable, .resizable],
                                  backing: .buffered, defer: false)
            window.title = "Timak Help"
            window.contentView = web
            window.isReleasedWhenClosed = false
            window.setFrameAutosaveName("TimakHelp")
            if window.frame.origin == .zero { window.center() }
            self.window = window
            self.web = web
        }
        if web?.url?.path != url.path || web?.url?.port != url.port {
            web?.load(URLRequest(url: url))
        }
        window?.makeKeyAndOrderFront(nil)
    }

    /// Links to the help's own sections stay here; anything else opens in the browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.host == "127.0.0.1" && url.path == "/help.html" { return decisionHandler(.allow) }
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }
}
