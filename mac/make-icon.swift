// Draws the app icon into an .iconset folder: swift make-icon.swift build/Timak.iconset
// The same shapes as timak/web/icon.svg (a box with its lid), in a 100 × 100 grid, y downwards.
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func color(_ hex: UInt32) -> CGColor {
    CGColor(red: CGFloat(hex >> 16 & 0xFF) / 255, green: CGFloat(hex >> 8 & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
}

// x, y, width, height, corner radius, colour
let shapes: [(CGFloat, CGFloat, CGFloat, CGFloat, CGFloat, UInt32)] = [
    (10, 10, 80, 80, 18, 0x0F6E56),              // tile
    (20.91, 28.18, 58.18, 13.64, 3.64, 0xFFFFFF), // lid
    (25.45, 41.82, 49.09, 31.82, 3.64, 0xE1F5EE), // box
    (38.18, 50, 23.64, 8.18, 4.09, 0x0F6E56),     // handle
]

func draw(_ size: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    let ctx = NSGraphicsContext(bitmapImageRep: rep)!.cgContext
    ctx.translateBy(x: 0, y: CGFloat(size))
    ctx.scaleBy(x: CGFloat(size) / 100, y: -CGFloat(size) / 100)
    for (x, y, w, h, r, hex) in shapes {
        ctx.addPath(CGPath(roundedRect: CGRect(x: x, y: y, width: w, height: h), cornerWidth: r, cornerHeight: r,
                           transform: nil))
        ctx.setFillColor(color(hex))
        ctx.fillPath()
    }
    return rep.representation(using: .png, properties: [:])!
}

for base in [16, 32, 128, 256, 512] {
    try draw(base).write(to: out.appendingPathComponent("icon_\(base)x\(base).png"))
    try draw(base * 2).write(to: out.appendingPathComponent("icon_\(base)x\(base)@2x.png"))
}
