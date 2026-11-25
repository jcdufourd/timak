import AppKit

/// The standard About window, with what Timak does and its licence.
enum AboutPanel {
    static let licenceURL = URL(string: "https://creativecommons.org/licenses/by-sa/4.0/")!

    @MainActor
    static func show() {
        let body = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        let centered = NSMutableParagraphStyle()
        centered.alignment = .center
        centered.paragraphSpacing = 6
        let plain: [NSAttributedString.Key: Any] = [
            .font: body, .foregroundColor: NSColor.labelColor, .paragraphStyle: centered,
        ]
        let credits = NSMutableAttributedString(
            string: "Turns old Time Machine backups into a compact archive of your own files.\n\n"
                + "Timak is free software, shared under the Creative Commons "
                + "Attribution-ShareAlike 4.0 International licence (CC BY-SA 4.0):\n",
            attributes: plain)
        var link = plain
        link[.link] = licenceURL
        credits.append(NSAttributedString(string: licenceURL.absoluteString, attributes: link))
        credits.append(NSAttributedString(
            string: "\n\nYou may share and adapt it, also commercially, if you credit the author, "
                + "say what you changed, and share your version under the same licence. "
                + "It comes with no warranty.",
            attributes: plain))
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "Timak",
            .credits: credits,
        ])
    }
}
