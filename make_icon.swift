import AppKit

// Renders a 1024x1024 app icon: gradient squircle with an equalizer / stem-split motif.
let S: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon_1024.png"

let img = NSImage(size: NSSize(width: S, height: S))
img.lockFocus()

let ctx = NSGraphicsContext.current!
ctx.imageInterpolation = .high

// Rounded-square background (macOS-style inset + corner radius).
let inset: CGFloat = 88
let rect = NSRect(x: inset, y: inset, width: S - 2 * inset, height: S - 2 * inset)
let radius = rect.width * 0.2237
let bg = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)

let top = NSColor(calibratedRed: 0.42, green: 0.36, blue: 0.98, alpha: 1)   // indigo
let bot = NSColor(calibratedRed: 0.72, green: 0.33, blue: 0.98, alpha: 1)   // violet
let grad = NSGradient(starting: top, ending: bot)!
grad.draw(in: bg, angle: -90)

// Equalizer bars, centered vertically, varying heights (audio-levels motif).
let heights: [CGFloat] = [0.46, 0.78, 0.58, 0.98, 0.64, 0.40]
let n = heights.count
let areaW = rect.width * 0.62
let areaH = rect.height * 0.62
let barGap = areaW * 0.05
let barW = (areaW - barGap * CGFloat(n - 1)) / CGFloat(n)
let startX = rect.midX - areaW / 2
let cy = rect.midY

for (i, h) in heights.enumerated() {
    let bh = areaH * h
    let x = startX + CGFloat(i) * (barW + barGap)
    let r = NSRect(x: x, y: cy - bh / 2, width: barW, height: bh)
    let alpha: CGFloat = 0.82 + 0.18 * CGFloat((i % 2))
    NSColor(calibratedWhite: 1, alpha: alpha).setFill()
    NSBezierPath(roundedRect: r, xRadius: barW * 0.42, yRadius: barW * 0.42).fill()
}

img.unlockFocus()

guard let tiff = img.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else {
    FileHandle.standardError.write("Failed to render icon\n".data(using: .utf8)!)
    exit(1)
}
try! png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
