import AppKit
import UserNotifications

// CCMonitor — статусбар с лимитами Claude Code и Codex.
// Логика данных в collector.py; здесь опрос, панель, окно настроек и уведомления.

typealias J = [String: Any]

func findPython() -> String {
    for p in ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
    return "/usr/bin/python3"
}
// collector.py лежит внутри бандла (Resources).
let COLLECTOR: String = (Bundle.main.resourcePath ?? ".") + "/collector.py"
let PYTHON = findPython()
let REFRESH_SECONDS: TimeInterval = 30
let PANEL_WIDTH: CGFloat = 340
let PAGE_W: CGFloat = 566
let RING: CGFloat = 108
let HOME = NSHomeDirectory()
let USAGE_DIR = HOME + "/.claude/usage-monitor"
let SETTINGS_PATH = USAGE_DIR + "/settings.json"
let NOTIFY_STATE_PATH = USAGE_DIR + "/notify-state.json"
let NOTIFY_LOG_PATH = USAGE_DIR + "/notify.log"
let LAUNCH_LABEL = "com.ccmonitor.app"
let ACCOUNT_ID_PREFIX = "CCMonitor-account-"

// ---------------------------------------------------------------- настройки
final class Settings {
    static let defaults: J = [
        "accounts": ["order": [String](), "hidden": [String]()] as J,
        "menubar": ["source": "active_claude", "window": "five_hour", "number": "used",
                    "show_reset": true, "show_gauge": true, "icon_only": false] as J,
        "notify": ["warn1": 80, "warn2": 95, "warn1_on": true, "warn2_on": true, "exhausted": true,
                   "reset": true, "access_lost": false, "accounts": [String](),
                   "windows": ["five_hour", "seven_day"]] as J,
        "general": ["poll_minutes": 5, "color_warn": 70, "color_crit": 90, "show_tokens": true,
                    "show_cost": true, "show_renewal": false, "launch_at_login": true] as J,
    ]
    var root: J = Settings.defaults
    var stamp: Date?

    static func merge(_ d: J, _ u: J) -> J {
        var out = u
        for (k, dv) in d {
            if let dd = dv as? J { out[k] = merge(dd, (u[k] as? J) ?? [:]) }
            else if out[k] == nil { out[k] = dv }
        }
        return out
    }
    func fileStamp() -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: SETTINGS_PATH))?[.modificationDate] as? Date
    }
    func load() {
        var user: J = [:]
        if let d = FileManager.default.contents(atPath: SETTINGS_PATH),
           let o = (try? JSONSerialization.jsonObject(with: d)) as? J { user = o }
        root = Settings.merge(Settings.defaults, user)
        stamp = fileStamp()
    }
    @discardableResult func reloadIfChanged() -> Bool {
        let s = fileStamp()
        if s != stamp { load(); return true }
        return false
    }
    func save() {
        try? FileManager.default.createDirectory(atPath: USAGE_DIR, withIntermediateDirectories: true)
        if let d = try? JSONSerialization.data(withJSONObject: root, options: [.prettyPrinted, .sortedKeys]) {
            try? d.write(to: URL(fileURLWithPath: SETTINGS_PATH), options: .atomic)
        }
        stamp = fileStamp()
    }
    func value(_ s: String, _ k: String) -> Any? { (root[s] as? J)?[k] ?? (Settings.defaults[s] as? J)?[k] }
    func int(_ s: String, _ k: String) -> Int {
        let v = value(s, k)
        if let n = v as? Int { return n }
        return (v as? NSNumber)?.intValue ?? 0
    }
    func bool(_ s: String, _ k: String) -> Bool {
        let v = value(s, k)
        if let b = v as? Bool { return b }
        return (v as? NSNumber)?.boolValue ?? false
    }
    func str(_ s: String, _ k: String) -> String { value(s, k) as? String ?? "" }
    func strs(_ s: String, _ k: String) -> [String] { value(s, k) as? [String] ?? [] }
    func set(_ s: String, _ k: String, _ v: Any) {
        var sec = root[s] as? J ?? [:]
        sec[k] = v
        root[s] = sec
        save()
    }
}
let store = Settings()


// ---------------------------------------------------------------- палитра, светлая/тёмная по effectiveAppearance
func rgba(_ r: Int, _ g: Int, _ b: Int, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat(r) / 255, green: CGFloat(g) / 255, blue: CGFloat(b) / 255, alpha: a)
}
func hexc(_ v: Int, _ a: CGFloat = 1) -> NSColor { rgba((v >> 16) & 255, (v >> 8) & 255, v & 255, a) }
func pal(_ light: NSColor, _ dark: NSColor) -> NSColor {
    NSColor(name: nil) { ap in ap.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light }
}
enum T {
    static let bg = pal(hexc(0xF5F4F0), hexc(0x101114))
    static let bg2 = pal(hexc(0xEEEDE8), hexc(0x15161A))
    static let card = pal(hexc(0xFFFFFF), hexc(0x17191E))
    static let ink = pal(rgba(20, 18, 14, 0.92), rgba(240, 238, 232, 0.94))
    static let ink2 = pal(hexc(0x5E5B55), hexc(0xA6A69E))
    static let ink3 = pal(hexc(0x9B988F), hexc(0x6E6F6A))
    static let line = pal(rgba(20, 18, 14, 0.08), rgba(255, 255, 255, 0.07))
    static let barBg = pal(hexc(0xECEAE4), rgba(255, 255, 255, 0.08))
    static let barFill = pal(rgba(20, 18, 14, 0.92), hexc(0xE8E6E0))
    static let chip = pal(hexc(0xF0EEE8), rgba(255, 255, 255, 0.06))
    static let dotBorder = pal(hexc(0xC9C6BE), rgba(255, 255, 255, 0.22))
    static let accent = pal(hexc(0x8A2F42), hexc(0xE0596F))
    static let accentSoft = pal(hexc(0xF7E9EC), rgba(224, 89, 111, 0.15))
    static let ok = hexc(0x4CC38A)
    static let alarm = pal(hexc(0xD4351C), hexc(0xFF6A4D))
    static let alarmSoft = pal(hexc(0xFCEBE7), rgba(255, 106, 77, 0.15))
    static let warn = pal(hexc(0xE08A1E), hexc(0xF0A24A))
}

func uiFont(_ size: CGFloat, _ weight: NSFont.Weight = .regular) -> NSFont {
    let name: String
    switch weight {
    case .bold: name = "Inter-Bold"
    case .semibold: name = "Inter-SemiBold"
    case .medium: name = "Inter-Medium"
    default: name = "Inter-Regular"
    }
    let base = NSFont(name: name, size: size) ?? NSFont.systemFont(ofSize: size, weight: weight)
    let tab: [NSFontDescriptor.FeatureKey: Int] = [.typeIdentifier: 6, .selectorIdentifier: 0]
    let desc = base.fontDescriptor.addingAttributes([.featureSettings: [tab]])
    return NSFont(descriptor: desc, size: size) ?? base
}

// ---------------------------------------------------------------- данные
func dnum(_ v: Any?) -> Double? { (v as? NSNumber)?.doubleValue }
func dint(_ v: Any?) -> Int? { (v as? NSNumber)?.intValue }

struct Win {
    let pct: Double
    let secs: Int?
    let hm: String?
    let full: String?
    let rolled: Bool
    let resetsAt: String?
    init?(_ o: Any?) {
        guard let d = o as? J else { return nil }
        pct = dnum(d["used_percentage"]) ?? 0
        secs = dint(d["resets_in_seconds"])
        hm = d["resets_hm"] as? String
        full = d["resets_full"] as? String
        rolled = (d["rolled_over"] as? Bool) == true
        resetsAt = d["resets_at_utc"] as? String
    }
    var started: Bool { secs != nil }
    var dateShort: String? { full?.split(separator: " ").first.map(String.init) }
}

func warnPct() -> Double { Double(store.int("general", "color_warn")) }
func critPct() -> Double { Double(store.int("general", "color_crit")) }

func gauge(_ p: Double) -> String {
    switch p {
    case ..<12.5: return "○"; case ..<37.5: return "◔"; case ..<62.5: return "◑"
    case ..<87.5: return "◕"; default: return "●"
    }
}
func severityColor(_ pct: Double) -> NSColor {
    if pct >= critPct() { return T.alarm }
    if pct >= warnPct() { return T.warn }
    return T.barFill
}
func fmtTokens(_ n: Int) -> String {
    if n >= 1_000_000 { return String(format: "%.1fM", Double(n)/1_000_000) }
    if n >= 1_000 { return String(format: "%.0fk", Double(n)/1_000) }
    return String(n)
}
func fmtHM(_ s: Int) -> String { String(format: "%d:%02d", s / 3600, (s % 3600) / 60) }
func fmtDH(_ s: Int) -> String {
    let d = s / 86400, h = (s % 86400) / 3600, m = (s % 3600) / 60
    if d > 0 { return "\(d) д \(h) ч" }
    if h > 0 { return "\(h) ч \(m) мин" }
    return "\(m) мин"
}
func fmtCompact(_ s: Int) -> String {
    if s >= 86400 { return "\(s / 86400)д \((s % 86400) / 3600)ч" }
    return fmtHM(s)
}
func agoText(_ s: Int) -> String {
    if s < 60 { return "только что" }
    if s < 3600 { return "\(s / 60) мин назад" }
    if s < 86400 { return "\(s / 3600) ч назад" }
    return "\(s / 86400) д назад"
}
func plural(_ n: Int, _ a: String, _ b: String, _ c: String) -> String {
    let m10 = n % 10, m100 = n % 100
    if m10 == 1 && m100 != 11 { return a }
    if (2...4).contains(m10) && !(12...14).contains(m100) { return b }
    return c
}
func localPart(_ email: String) -> String { email.split(separator: "@").first.map(String.init) ?? email }

func errorText(_ err: String) -> String {
    switch err {
    case "token_expired":  return "Токен истёк — открой Claude Code"
    case "relogin_needed": return "Нужен повторный вход в эту подписку"
    case "no_creds":       return "Подписка не подключена"
    case "no_refresh":     return "Нет ключа обновления — войди заново"
    case "account_on_hold": return "Аккаунт ограничен Anthropic — claude.ai/restricted"
    case "network":        return "Нет соединения"
    case "http_429":       return "Лимиты обновляются…"
    case "hidden":         return "Скрыта в настройках"
    default:               return "Лимиты недоступны"
    }
}
typealias Status = (String, NSColor, NSColor?)
func shortStatus(_ err: String) -> Status {
    switch err {
    case "relogin_needed", "no_refresh", "token_expired", "no_creds": return ("нужен вход", T.warn, nil)
    case "account_on_hold": return ("ограничен", T.alarm, T.alarmSoft)
    case "network": return ("нет сети", T.ink2, nil)
    case "http_429": return ("обновляется", T.ink2, nil)
    default: return ("недоступно", T.ink2, nil)
    }
}

func orderedIds(_ ids: [String]) -> [String] {
    let order = store.strs("accounts", "order")
    return ids.enumerated().sorted { a, b in
        let ia = order.firstIndex(of: a.element) ?? (1000 + a.offset)
        let ib = order.firstIndex(of: b.element) ?? (1000 + b.offset)
        return ia < ib
    }.map { $0.element }
}

// ---------------------------------------------------------------- запуск collector
func runPython(_ args: [String], stdin: String? = nil) -> Data? {
    let p = Process(); p.executableURL = URL(fileURLWithPath: PYTHON)
    p.arguments = [COLLECTOR] + args
    let out = Pipe(); p.standardOutput = out; p.standardError = FileHandle.nullDevice
    let inPipe = Pipe()
    p.standardInput = stdin == nil ? FileHandle.nullDevice : inPipe
    do {
        try p.run()
        if let s = stdin {
            inPipe.fileHandleForWriting.write(s.data(using: .utf8) ?? Data())
            try? inPipe.fileHandleForWriting.close()
        }
        let data = out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
        return data
    } catch { return nil }
}
func runTool(_ path: String, _ args: [String]) -> String {
    let p = Process(); p.executableURL = URL(fileURLWithPath: path); p.arguments = args
    let out = Pipe(); p.standardOutput = out; p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return "" }
    let d = out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
    return String(data: d, encoding: .utf8) ?? ""
}

// ---------------------------------------------------------------- виджеты
let C_DIM = T.ink3

func monoFont(_ size: CGFloat, _ weight: NSFont.Weight = .regular) -> NSFont { uiFont(size, weight) }
func lbl(_ s: String, _ size: CGFloat, _ weight: NSFont.Weight = .regular,
         _ color: NSColor = T.ink) -> NSTextField {
    let t = NSTextField(labelWithString: s)
    t.font = monoFont(size, weight)
    t.textColor = color
    t.lineBreakMode = .byTruncatingTail
    t.translatesAutoresizingMaskIntoConstraints = false
    return t
}
func attrLbl(_ a: NSAttributedString) -> NSTextField {
    let t = NSTextField(labelWithAttributedString: a)
    t.lineBreakMode = .byTruncatingTail
    t.translatesAutoresizingMaskIntoConstraints = false
    return t
}
func wrapLbl(_ s: String, _ size: CGFloat, _ color: NSColor, width: CGFloat) -> NSTextField {
    let t = lbl(s, size, .regular, color)
    t.lineBreakMode = .byWordWrapping
    t.maximumNumberOfLines = 0
    t.preferredMaxLayoutWidth = width
    t.widthAnchor.constraint(equalToConstant: width).isActive = true
    return t
}
func part(_ s: String, _ size: CGFloat, _ weight: NSFont.Weight, _ color: NSColor,
          kern: CGFloat = 0) -> NSAttributedString {
    NSAttributedString(string: s, attributes: [.font: monoFont(size, weight),
                                               .foregroundColor: color, .kern: kern])
}
func joined(_ parts: NSAttributedString...) -> NSAttributedString {
    let m = NSMutableAttributedString()
    parts.forEach { m.append($0) }
    return m
}

final class ColorBox: NSView {
    var fill: NSColor? { didSet { needsDisplay = true } }
    var radius: CGFloat = 0
    var stroke: NSColor?
    init() { super.init(frame: .zero); translatesAutoresizingMaskIntoConstraints = false }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirty: NSRect) {
        if let f = fill {
            f.setFill()
            if radius > 0 { NSBezierPath(roundedRect: bounds, xRadius: radius, yRadius: radius).fill() }
            else { bounds.fill() }
        }
        if let st = stroke {
            let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: radius, yRadius: radius)
            path.lineWidth = 1; st.setStroke(); path.stroke()
        }
    }
}

final class BarView: NSView {
    var progress: CGFloat = 0 { didSet { needsDisplay = true } }
    var fill: NSColor = T.barFill
    var barHeight: CGFloat = 4
    override var intrinsicContentSize: NSSize { NSSize(width: NSView.noIntrinsicMetric, height: barHeight) }
    override func draw(_ dirty: NSRect) {
        let h = bounds.height
        T.barBg.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: h/2, yRadius: h/2).fill()
        guard progress > 0 else { return }
        let w = max(h, bounds.width * min(progress, 1))
        fill.setFill()
        NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: w, height: h), xRadius: h/2, yRadius: h/2).fill()
    }
}
func makeBar(_ pct: Double, height: CGFloat) -> BarView {
    let b = BarView(); b.barHeight = height
    b.progress = CGFloat(pct) / 100; b.fill = severityColor(pct)
    b.translatesAutoresizingMaskIntoConstraints = false
    b.heightAnchor.constraint(equalToConstant: height).isActive = true
    return b
}

final class RingView: NSView {
    var progress: CGFloat = 0
    var color: NSColor = T.barFill
    let lineW: CGFloat = 8
    override func draw(_ dirty: NSRect) {
        let c = NSPoint(x: bounds.midX, y: bounds.midY)
        let r = (min(bounds.width, bounds.height) - lineW) / 2
        let track = NSBezierPath()
        track.appendArc(withCenter: c, radius: r, startAngle: 0, endAngle: 360)
        track.lineWidth = lineW
        T.barBg.setStroke(); track.stroke()
        guard progress > 0 else { return }
        let p = max(min(progress, 1), 0.02)
        let arc = NSBezierPath()
        arc.appendArc(withCenter: c, radius: r, startAngle: 90, endAngle: 90 - 360 * p, clockwise: true)
        arc.lineWidth = lineW; arc.lineCapStyle = .round
        color.setStroke(); arc.stroke()
    }
}

final class CheckControl: NSView {
    var on: Bool { didSet { needsDisplay = true } }
    var enabled = true { didSet { alphaValue = enabled ? 1 : 0.45; needsDisplay = true } }
    var onChange: ((Bool) -> Void)?
    var key = ""
    let label: NSTextField
    init(_ title: String, on: Bool, size: CGFloat = 13.5, color: NSColor = T.ink) {
        self.on = on
        label = lbl(title, size, .regular, color)
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 24),
            label.trailingAnchor.constraint(equalTo: trailingAnchor),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
            heightAnchor.constraint(equalToConstant: 20),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirty: NSRect) {
        let r = NSRect(x: 0, y: (bounds.height - 16) / 2, width: 16, height: 16)
        let box = NSBezierPath(roundedRect: r.insetBy(dx: 0.5, dy: 0.5), xRadius: 4.5, yRadius: 4.5)
        if on {
            T.accent.setFill(); box.fill()
            let tick = NSBezierPath()
            tick.move(to: NSPoint(x: r.minX + 4, y: r.midY))
            tick.line(to: NSPoint(x: r.minX + 7, y: r.midY - 3))
            tick.line(to: NSPoint(x: r.minX + 12, y: r.midY + 3.5))
            tick.lineWidth = 1.8; tick.lineCapStyle = .round; tick.lineJoinStyle = .round
            NSColor.white.setStroke(); tick.stroke()
        } else {
            T.card.setFill(); box.fill()
            T.dotBorder.setStroke(); box.lineWidth = 1; box.stroke()
        }
    }
    override func mouseDown(with e: NSEvent) {
        guard enabled else { return }
        on.toggle(); onChange?(on)
    }
}
func check(_ title: String, _ on: Bool, size: CGFloat = 13.5, color: NSColor = T.ink,
           _ change: @escaping (Bool) -> Void) -> CheckControl {
    let c = CheckControl(title, on: on, size: size, color: color)
    c.onChange = change
    return c
}

final class PillButton: NSView {
    enum Kind { case primary, secondary, danger, link }
    var onClick: (() -> Void)?
    let kind: Kind
    private var hover = false
    init(_ title: String, _ kind: Kind) {
        self.kind = kind
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        let fg: NSColor
        switch kind {
        case .primary: fg = .white
        case .secondary: fg = T.ink
        case .danger, .link: fg = T.alarm
        }
        let l = lbl(title, kind == .link ? 12 : 13, .medium, fg)
        l.lineBreakMode = .byClipping
        addSubview(l)
        let padX: CGFloat = kind == .link ? 2 : 14, padY: CGFloat = kind == .link ? 2 : 7
        NSLayoutConstraint.activate([
            l.leadingAnchor.constraint(equalTo: leadingAnchor, constant: padX),
            l.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -padX),
            l.topAnchor.constraint(equalTo: topAnchor, constant: padY),
            l.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -padY),
        ])
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self))
    }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirty: NSRect) {
        guard kind != .link else { return }
        let path = NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8)
        (kind == .primary ? T.accent : T.chip).setFill(); path.fill()
        if hover { (kind == .primary ? NSColor.black.withAlphaComponent(0.12) : T.line).setFill(); path.fill() }
        if kind != .primary { T.line.setStroke(); path.lineWidth = 1; path.stroke() }
    }
    override func mouseEntered(with e: NSEvent) { hover = true; needsDisplay = true }
    override func mouseExited(with e: NSEvent) { hover = false; needsDisplay = true }
    override func mouseUp(with e: NSEvent) {
        if bounds.contains(convert(e.locationInWindow, from: nil)) { onClick?() }
    }
}

func chip(_ text: String, fg: NSColor, bg: NSColor) -> NSView {
    let box = ColorBox(); box.fill = bg; box.radius = 5
    let l = lbl(text, 11, .medium, fg); l.lineBreakMode = .byClipping
    inset(l, in: box, 2, 7, 2, 7)
    return box
}

final class ClosurePopup: NSPopUpButton {
    var onChange: ((String) -> Void)?
    @objc func picked() { onChange?(selectedItem?.representedObject as? String ?? "") }
}
func popup(_ items: [(String, String)], selected: String, width: CGFloat,
           _ change: @escaping (String) -> Void) -> ClosurePopup {
    let p = ClosurePopup(frame: .zero, pullsDown: false)
    for (t, v) in items { p.addItem(withTitle: t); p.lastItem?.representedObject = v }
    if let i = items.firstIndex(where: { $0.1 == selected }) { p.selectItem(at: i) }
    p.target = p; p.action = #selector(ClosurePopup.picked); p.onChange = change
    p.font = uiFont(13)
    p.translatesAutoresizingMaskIntoConstraints = false
    p.widthAnchor.constraint(equalToConstant: width).isActive = true
    return p
}
final class NumField: NSTextField, NSTextFieldDelegate {
    var onCommit: ((Int) -> Void)?
    var lo = 1, hi = 100
    convenience init(value: Int, lo: Int, hi: Int, _ commit: @escaping (Int) -> Void) {
        self.init(frame: .zero)
        self.lo = lo; self.hi = hi; onCommit = commit
        stringValue = String(value); alignment = .right
        font = monoFont(13)
        delegate = self
        translatesAutoresizingMaskIntoConstraints = false
        widthAnchor.constraint(equalToConstant: 52).isActive = true
    }
    func controlTextDidEndEditing(_ obj: Notification) {
        let v = min(max(Int(stringValue.trimmingCharacters(in: .whitespaces)) ?? lo, lo), hi)
        stringValue = String(v)
        onCommit?(v)
    }
}

final class IconButton: NSView {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    var onClick: (() -> Void)?
    private let img = NSImageView()
    private var hover = false
    init(symbol: String, tip: String) {
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        toolTip = tip
        let cfg = NSImage.SymbolConfiguration(pointSize: 12, weight: .medium)
        img.image = NSImage(systemSymbolName: symbol, accessibilityDescription: tip)?.withSymbolConfiguration(cfg)
        img.contentTintColor = T.ink
        img.translatesAutoresizingMaskIntoConstraints = false
        addSubview(img)
        NSLayoutConstraint.activate([
            img.centerXAnchor.constraint(equalTo: centerXAnchor),
            img.centerYAnchor.constraint(equalTo: centerYAnchor),
            widthAnchor.constraint(equalToConstant: 28),
            heightAnchor.constraint(equalToConstant: 24),
        ])
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self))
    }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirty: NSRect) {
        T.chip.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 6, yRadius: 6).fill()
        if hover { T.line.setFill(); NSBezierPath(roundedRect: bounds, xRadius: 6, yRadius: 6).fill() }
    }
    override func mouseEntered(with e: NSEvent) { hover = true; needsDisplay = true }
    override func mouseExited(with e: NSEvent) { hover = false; needsDisplay = true }
    override func mouseUp(with e: NSEvent) {
        if bounds.contains(convert(e.locationInWindow, from: nil)) { onClick?() }
    }
}

// левый и правый элемент в одной строке; высота — по левому
func spread(_ left: NSView, _ right: NSView?, baseline: Bool = false) -> NSView {
    let row = NSView(); row.translatesAutoresizingMaskIntoConstraints = false
    left.translatesAutoresizingMaskIntoConstraints = false
    row.addSubview(left)
    var cs = [
        left.leadingAnchor.constraint(equalTo: row.leadingAnchor),
        left.topAnchor.constraint(equalTo: row.topAnchor),
        left.bottomAnchor.constraint(equalTo: row.bottomAnchor),
    ]
    if let r = right {
        r.translatesAutoresizingMaskIntoConstraints = false
        row.addSubview(r)
        left.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        r.setContentCompressionResistancePriority(.required, for: .horizontal)
        cs += [r.trailingAnchor.constraint(equalTo: row.trailingAnchor),
               left.trailingAnchor.constraint(lessThanOrEqualTo: r.leadingAnchor, constant: -8)]
        cs.append(baseline && r is NSTextField ? r.lastBaselineAnchor.constraint(equalTo: left.lastBaselineAnchor)
                                               : r.centerYAnchor.constraint(equalTo: left.centerYAnchor))
    } else {
        cs.append(left.trailingAnchor.constraint(lessThanOrEqualTo: row.trailingAnchor))
    }
    NSLayoutConstraint.activate(cs)
    return row
}
func vstack(_ views: [NSView], spacing: CGFloat, align: NSLayoutConstraint.Attribute = .width) -> NSStackView {
    let s = NSStackView(views: views); s.orientation = .vertical
    s.alignment = align; s.spacing = spacing
    s.translatesAutoresizingMaskIntoConstraints = false
    if align == .width {
        for v in views { v.translatesAutoresizingMaskIntoConstraints = false; v.widthAnchor.constraint(equalTo: s.widthAnchor).isActive = true }
    }
    return s
}
func hstack(_ views: [NSView], spacing: CGFloat, align: NSLayoutConstraint.Attribute = .centerY) -> NSStackView {
    let s = NSStackView(views: views); s.orientation = .horizontal
    s.alignment = align; s.spacing = spacing
    s.translatesAutoresizingMaskIntoConstraints = false
    return s
}
func inset(_ content: NSView, in box: NSView, _ t: CGFloat, _ l: CGFloat, _ b: CGFloat, _ r: CGFloat) {
    content.translatesAutoresizingMaskIntoConstraints = false
    box.addSubview(content)
    NSLayoutConstraint.activate([
        content.topAnchor.constraint(equalTo: box.topAnchor, constant: t),
        content.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: l),
        content.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -r),
        content.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -b),
    ])
}

// ---------------------------------------------------------------- уведомления
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = Notifier()
    static var sink: ((String, String) -> Void)?
    func setup() { UNUserNotificationCenter.current().delegate = self }
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler h: @escaping (UNNotificationPresentationOptions) -> Void) {
        h([.banner, .sound])
    }
    func log(_ s: String) {
        let line = "\(Date()) \(s)\n"
        if let h = FileHandle(forWritingAtPath: NOTIFY_LOG_PATH) {
            h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
        } else {
            try? line.write(toFile: NOTIFY_LOG_PATH, atomically: true, encoding: .utf8)
        }
    }
    func requestAuth(_ done: ((Bool) -> Void)? = nil) {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { ok, err in
            self.log("requestAuthorization granted=\(ok) error=\(err.map { "\($0)" } ?? "nil")")
            DispatchQueue.main.async { done?(ok) }
        }
    }
    func send(_ title: String, _ body: String) {
        if let sink = Notifier.sink { sink(title, body); return }
        let c = UNUserNotificationCenter.current()
        c.getNotificationSettings { s in
            switch s.authorizationStatus {
            case .authorized, .provisional:
                self.post(title, body)
            case .notDetermined:
                self.requestAuth { ok in
                    if ok { self.post(title, body) } else { self.fallback(title, body, why: "auth denied") }
                }
            default:
                self.fallback(title, body, why: "status \(s.authorizationStatus.rawValue)")
            }
        }
    }
    private func post(_ title: String, _ body: String) {
        let content = UNMutableNotificationContent()
        content.title = title; content.body = body; content.sound = .default
        let req = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req) { err in
            if let err = err { self.fallback(title, body, why: "add failed: \(err)") }
            else { self.log("sent via UNUserNotificationCenter: \(title)") }
        }
    }
    func fallback(_ title: String, _ body: String, why: String) {
        log("fallback osascript (\(why)): \(title)")
        _ = runTool("/usr/bin/osascript", ["-e", "on run argv", "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run", title, body])
    }
}

// resets_at от сервера «дрожит» на доли секунды (22:59:59.6 / 23:00:00.2) — ключ окна округляем до 5 минут
func windowStamp(_ iso: String?) -> String {
    guard let iso = iso,
          let re = try? NSRegularExpression(pattern: "\\.\\d+"),
          case let clean = re.stringByReplacingMatches(in: iso, range: NSRange(iso.startIndex..., in: iso), withTemplate: ""),
          let d = ISO8601DateFormatter().date(from: clean) else { return iso ?? "none" }
    return String(Int((d.timeIntervalSince1970 / 300).rounded()) * 300)
}

final class NotifyEngine {
    var state: J = [:]
    var fresh = true
    let path: String
    init(path: String = NOTIFY_STATE_PATH) {
        self.path = path
        if let d = FileManager.default.contents(atPath: path),
           let o = (try? JSONSerialization.jsonObject(with: d)) as? J { state = o; fresh = false }
    }
    func persist() {
        if let d = try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]) {
            try? d.write(to: URL(fileURLWithPath: path), options: .atomic)
        }
    }

    // каждый порог срабатывает один раз за окно: ключ — подписка + окно + resets_at
    func process(_ data: J) {
        var last = state["last"] as? J ?? [:]
        var fired = state["fired"] as? J ?? [:]
        var access = state["access"] as? J ?? [:]
        let silent = fresh        // первый запуск: уже достигнутое только запоминаем, без залпа уведомлений
        let nowTs = Date().timeIntervalSince1970
        let wantAccounts = store.strs("notify", "accounts")
        let wantWindows = store.strs("notify", "windows")
        let w1 = Double(store.int("notify", "warn1")), w2 = Double(store.int("notify", "warn2"))

        func notify(_ title: String, _ body: String) { if !silent { Notifier.shared.send(title, body) } }

        var entities: [(String, String, J, J?)] = []   // id, имя, limits, сырой аккаунт
        for a in data["accounts"] as? [J] ?? [] {
            let id = a["id"] as? String ?? ""
            if (a["active"] as? Bool) != true && (store.strs("accounts", "hidden").contains(id)
                || (a["hidden"] as? Bool) == true || (a["auto_hidden"] as? Bool) == true) { continue }
            let label = a["label"] as? String ?? "Claude"
            let name = label + ((a["email"] as? String).map { " · " + localPart($0) } ?? "")
            entities.append((id, name, a["limits"] as? J ?? [:], a))
        }
        if let cx = data["codex"] as? J, !store.strs("accounts", "hidden").contains("codex") {
            entities.append(("codex", cx["label"] as? String ?? "Codex", cx, nil))
        }

        for (id, name, lim, acct) in entities {
            if !wantAccounts.isEmpty && !wantAccounts.contains(id) { continue }
            if let a = acct, id.hasPrefix(ACCOUNT_ID_PREFIX) {
                let since = dint(a["no_access_since"])
                if let s = since {
                    if store.bool("notify", "access_lost"), dint(access[id]) != s {
                        notify("CCMonitor", "\(name): нужен повторный вход")
                    }
                    access[id] = s
                } else { access.removeValue(forKey: id) }
            }
            guard lim["ok"] as? Bool == true else { continue }
            for (wkey, wname) in [("five_hour", "5 часов"), ("seven_day", "неделя")] where wantWindows.contains(wkey) {
                guard let w = Win(lim[wkey]) else { continue }
                if (lim["stale"] as? Bool) == true && (w.secs ?? 0) == 0 && w.started { continue }
                let r = w.pct > 0 ? windowStamp(w.resetsAt) : "none"
                let lk = "\(id)|\(wkey)", fk = "\(id)|\(wkey)|\(r)"
                var l = last[lk] as? J ?? [:]
                if let prev = l["r"] as? String, prev != r, (l["ex"] as? Bool) == true, w.pct < 100,
                   store.bool("notify", "reset") {
                    notify("CCMonitor", "\(name): окно «\(wname)» сбросилось — можно работать")
                }
                if (l["r"] as? String) != r { l = ["r": r, "ex": false] }
                if w.pct >= 100 { l["ex"] = true }
                last[lk] = l

                var f = fired[fk] as? J ?? [:]
                let hasW1 = (f["w1"] as? Bool) == true, hasW2 = (f["w2"] as? Bool) == true
                let hasEx = (f["ex"] as? Bool) == true
                if w.pct >= 100, store.bool("notify", "exhausted"), !hasEx {
                    notify("CCMonitor", "\(name): лимит «\(wname)» закончился")
                    f["ex"] = true; f["w1"] = true; f["w2"] = true
                } else if w.pct >= w2, store.bool("notify", "warn2_on"), !hasW2 {
                    notify("CCMonitor", "\(name): «\(wname)» — \(Int(w.pct))%")
                    f["w1"] = true; f["w2"] = true
                } else if w.pct >= w1, store.bool("notify", "warn1_on"), !hasW1 {
                    notify("CCMonitor", "\(name): «\(wname)» — \(Int(w.pct))%")
                    f["w1"] = true
                }
                if silent {
                    if w.pct >= 100 { f["ex"] = true }
                    if w.pct >= w2 { f["w2"] = true }
                    if w.pct >= w1 { f["w1"] = true }
                }
                if !f.isEmpty { f["t"] = nowTs; fired[fk] = f }
            }
        }
        for (k, v) in fired { if nowTs - (dnum((v as? J)?["t"]) ?? 0) > 10 * 86400 { fired.removeValue(forKey: k) } }
        state = ["last": last, "fired": fired, "access": access]
        fresh = false
        persist()
    }
}

// ---------------------------------------------------------------- строка статусбара
struct Entity {
    let tag: String
    let lim: J
    let isCodex: Bool
}

func statusAttributed(_ d: J?) -> (NSAttributedString, String) {
    let font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.menuBarFont(ofSize: 0).pointSize, weight: .regular)
    func plain(_ s: String, _ c: NSColor) -> (NSAttributedString, String) {
        (NSAttributedString(string: s, attributes: [.font: font, .foregroundColor: c]), s)
    }
    let source = store.str("menubar", "source"), window = store.str("menubar", "window")
    let left = store.str("menubar", "number") == "left"
    let showReset = store.bool("menubar", "show_reset"), showGauge = store.bool("menubar", "show_gauge")
    let iconOnly = store.bool("menubar", "icon_only")

    let hiddenIds = store.strs("accounts", "hidden")
    var claude: [Entity] = []
    var active: Entity?
    for a in d?["accounts"] as? [J] ?? [] {
        let id = a["id"] as? String ?? ""
        let isActive = (a["active"] as? Bool) == true
        if !isActive && (hiddenIds.contains(id) || (a["hidden"] as? Bool) == true || (a["auto_hidden"] as? Bool) == true) { continue }
        let e = Entity(tag: a["label"] as? String ?? "Claude", lim: a["limits"] as? J ?? [:], isCodex: false)
        claude.append(e)
        if isActive { active = e }
    }
    let activeEnt = active ?? Entity(tag: "Claude", lim: d?["limits"] as? J ?? [:], isCodex: false)
    var codexEnt: Entity?
    if let cx = d?["codex"] as? J, !hiddenIds.contains("codex") {
        codexEnt = Entity(tag: "Codex", lim: cx, isCodex: true)
    }

    func wins(_ e: Entity) -> [Win] {
        guard e.lim["ok"] as? Bool == true else { return [] }
        let fh = Win(e.lim["five_hour"]), sd = Win(e.lim["seven_day"])
        switch window {
        case "seven_day": return [sd].compactMap { $0 }
        case "both": return [fh, sd].compactMap { $0 }
        default: return [fh].compactMap { $0 }
        }
    }
    func segment(_ e: Entity, prefix: String) -> (String, Double)? {
        let ws = wins(e)
        guard let first = ws.first else { return nil }
        var s = prefix
        if showGauge || iconOnly { s += gauge(first.pct) }
        if !iconOnly {
            if !s.isEmpty && !s.hasSuffix(" ") { s += " " }
            s += ws.map { "\(Int(left ? max(100 - $0.pct, 0) : $0.pct))%" }.joined(separator: " · ")
            if (e.lim["stale"] as? Bool) == true { s += "*" }
            if showReset, let secs = first.secs, !(first.rolled) {
                s += " · " + (window == "seven_day" ? fmtCompact(secs) : fmtHM(secs))
            }
        }
        return (s, ws.map { $0.pct }.max() ?? 0)
    }

    var segs: [(String, Double)] = []
    switch source {
    case "codex":
        if let c = codexEnt, let s = segment(c, prefix: "") { segs = [s] }
    case "both":
        if let s = segment(activeEnt, prefix: "Cl ") { segs.append(s) }
        if let c = codexEnt, let s = segment(c, prefix: "Cx ") { segs.append(s) }
    case "most_loaded":
        var all = claude
        if let c = codexEnt { all.append(c) }
        let best = all.filter { !wins($0).isEmpty }.max { (wins($0).map { $0.pct }.max() ?? 0) < (wins($1).map { $0.pct }.max() ?? 0) }
        if let b = best, let s = segment(b, prefix: iconOnly ? "" : b.tag + " ") { segs = [s] }
    default:
        if let s = segment(activeEnt, prefix: "") { segs = [s] }
    }
    if segs.isEmpty {
        let err = (activeEnt.lim["error"] as? String) ?? ""
        return err == "token_expired" ? plain("CC ⚠︎", T.warn) : plain("CC …", NSColor.secondaryLabelColor)
    }
    let out = NSMutableAttributedString()
    for (i, s) in segs.enumerated() {
        if i > 0 { out.append(NSAttributedString(string: "  ", attributes: [.font: font])) }
        let c: NSColor = s.1 >= critPct() ? T.alarm : s.1 >= warnPct() ? T.warn : NSColor.labelColor
        out.append(NSAttributedString(string: s.0, attributes: [.font: font, .foregroundColor: c]))
    }
    return (out, out.string)
}

// ---------------------------------------------------------------- запуск при входе, выход
func launchctlDomain() -> String { "gui/\(getuid())" }
func launchAtLoginEnabled() -> Bool {
    let out = runTool("/bin/launchctl", ["print-disabled", launchctlDomain()])
    for line in out.split(separator: "\n") where line.contains("\"\(LAUNCH_LABEL)\"") {
        return !line.contains("disabled") || line.contains("enabled")
    }
    return true
}
func setLaunchAtLogin(_ on: Bool) {
    _ = runTool("/bin/launchctl", [on ? "enable" : "disable", "\(launchctlDomain())/\(LAUNCH_LABEL)"])
}
// KeepAlive в plist перезапустил бы приложение после обычного выхода — поэтому выгружаем задачу из launchd:
// до следующего входа в систему (или ручного запуска) оно не вернётся.
func quitForReal() {
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    p.arguments = ["bootout", "\(launchctlDomain())/\(LAUNCH_LABEL)"]
    p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
    try? p.run()
    p.waitUntilExit()
    NSApp.terminate(nil)
}

// ---------------------------------------------------------------- приложение
// своя панель вместо NSPopover: без системной стрелки, фон и рамка из палитры
final class PanelWindow: NSPanel {
    var onEscape: (() -> Void)?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { onEscape?() }
    override func keyDown(with e: NSEvent) {
        if e.keyCode == 53 { onEscape?() } else { super.keyDown(with: e) }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var panel: PanelWindow?
    let panelClip = NSView()
    let panelBorder = ColorBox()
    var panelClosing = false
    var globalMonitor: Any?
    var localMonitor: Any?
    var panelShown: Bool { (panel?.isVisible ?? false) && !panelClosing }
    var timer: Timer?
    var lastData: J?
    let engine = NotifyEngine()
    var settingsController: SettingsController?

    func applicationDidFinishLaunching(_ n: Notification) {
        store.load()
        installMenu()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "CC …"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePanel)
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(otherAppActivated(_:)),
            name: NSWorkspace.didActivateApplicationNotification, object: nil)
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count {
            lastData = loadDemoOrCollect()
            if let a = args.firstIndex(of: "--appearance"), a + 1 < args.count {
                NSApp.appearance = NSAppearance(named: args[a + 1] == "dark" ? .darkAqua : .aqua)
            }
            snapshot(buildPanel(), to: args[i + 1])
            exit(0)
        }
        if let i = args.firstIndex(of: "--snapshot-settings"), i + 1 < args.count {
            lastData = loadDemoOrCollect()
            if let a = args.firstIndex(of: "--appearance"), a + 1 < args.count {
                NSApp.appearance = NSAppearance(named: args[a + 1] == "dark" ? .darkAqua : .aqua)
            }
            let sc = SettingsController(app: self)
            for t in 0..<4 {
                sc.select(tab: t)
                sc.window.contentView?.layoutSubtreeIfNeeded()
                snapshot(sc.window.contentView!, to: args[i + 1] + "-\(t).png")
            }
            exit(0)
        }
        if args.contains("--test-engine") { runEngineTest(); exit(0) }
        Notifier.shared.setup()
        if args.contains("--test-notify") {
            Notifier.shared.send("CCMonitor", "Проверка уведомлений")
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { exit(0) }
            return
        }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: REFRESH_SECONDS, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    func runEngineTest() {
        let tmp = NSTemporaryDirectory() + "engine-test-\(getpid()).json"
        try? FileManager.default.removeItem(atPath: tmp)
        let e = NotifyEngine(path: tmp)
        var events: [String] = []
        Notifier.sink = { events.append($0 + " | " + $1) }
        func data(_ pct: Double, _ reset: String?, id: String = "CCMonitor-account-9") -> J {
            let w: J = ["used_percentage": pct, "resets_at_utc": reset as Any, "resets_in_seconds": reset == nil ? NSNull() : 1000 as Any]
            return ["accounts": [["id": id, "label": "Max", "email": "t@x.com", "active": false,
                                  "limits": ["ok": true, "five_hour": w, "seven_day": NSNull()] as J] as J]]
        }
        func step(_ name: String, _ d: J, expect: Int) {
            events.removeAll(); e.process(d)
            print((events.count == expect ? "OK  " : "FAIL"), name, "->", events.count, events)
        }
        step("первый запуск молча, 85% (порог 80 уже пройден)", data(85, "2026-10-03T10:00:00.4+00:00"), expect: 0)
        step("то же окно, 86% — повтора нет", data(86, "2026-10-03T09:59:59.8+00:00"), expect: 0)
        step("96% — второй порог", data(96, "2026-10-03T10:00:00.2+00:00"), expect: 1)
        step("97% — повтора нет", data(97, "2026-10-03T09:59:59.6+00:00"), expect: 0)
        step("100% — лимит закончился", data(100, "2026-10-03T10:00:00.1+00:00"), expect: 1)
        step("100% снова — повтора нет", data(100, "2026-10-03T09:59:59.9+00:00"), expect: 0)
        step("новое окно, 3% — окно сбросилось", data(3, "2026-10-03T15:00:00.3+00:00"), expect: 1)
        step("новое окно, 4% — тихо", data(4, "2026-10-03T14:59:59.9+00:00"), expect: 0)
        step("новое окно, 81% — первый порог", data(81, "2026-10-03T15:00:00.3+00:00"), expect: 1)
        let e2 = NotifyEngine(path: tmp)
        events.removeAll(); e2.process(data(82, "2026-10-03T15:00:00.1+00:00"))
        print((events.isEmpty ? "OK  " : "FAIL"), "перезапуск приложения — состояние из файла, повтора нет", events)
        (store.root["notify"] as? J).map { var n = $0; n["accounts"] = ["codex"]; store.root["notify"] = n }
        events.removeAll(); e2.process(data(100, "2026-10-03T20:00:00+00:00"))
        print((events.isEmpty ? "OK  " : "FAIL"), "фильтр по аккаунтам: чужой аккаунт молчит", events)
        try? FileManager.default.removeItem(atPath: tmp)
    }

    func loadDemoOrCollect() -> J? {
        if let i = CommandLine.arguments.firstIndex(of: "--data"), i + 1 < CommandLine.arguments.count,
           let d = FileManager.default.contents(atPath: CommandLine.arguments[i + 1]) {
            return (try? JSONSerialization.jsonObject(with: d)) as? J
        }
        return runCollector()
    }
    func snapshot(_ v: NSView, to path: String) {
        v.layoutSubtreeIfNeeded()
        if let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) {
            v.cacheDisplay(in: v.bounds, to: rep)
            if let png = rep.representation(using: .png, properties: [:]) {
                try? png.write(to: URL(fileURLWithPath: path))
            }
        }
    }

    // у приложения без меню (LSUIElement) не работают Cmd+C/V в полях ввода — меню «Правка» нужно явно
    func installMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "Закрыть окно", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Правка"); editItem.submenu = edit
        edit.addItem(withTitle: "Вырезать", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Копировать", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Вставить", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Выбрать всё", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        NSApp.mainMenu = main
    }

    @objc func togglePanel() {
        if panelShown { closePanel() } else { openPanel() }
    }
    func makePanelIfNeeded() -> PanelWindow {
        if let p = panel { return p }
        let p = PanelWindow(contentRect: NSRect(x: 0, y: 0, width: PANEL_WIDTH, height: 200),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.level = .popUpMenu
        p.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        p.isReleasedWhenClosed = false
        p.hidesOnDeactivate = false
        p.onEscape = { [weak self] in self?.closePanel() }
        let host = NSView()
        panelClip.wantsLayer = true
        panelClip.layer?.cornerRadius = 12
        panelClip.layer?.cornerCurve = .continuous
        panelClip.layer?.masksToBounds = true
        panelClip.translatesAutoresizingMaskIntoConstraints = false
        panelBorder.radius = 12
        panelBorder.stroke = T.line
        panelBorder.translatesAutoresizingMaskIntoConstraints = false
        host.addSubview(panelClip); host.addSubview(panelBorder)
        for v in [panelClip, panelBorder] {
            NSLayoutConstraint.activate([
                v.topAnchor.constraint(equalTo: host.topAnchor), v.bottomAnchor.constraint(equalTo: host.bottomAnchor),
                v.leadingAnchor.constraint(equalTo: host.leadingAnchor), v.trailingAnchor.constraint(equalTo: host.trailingAnchor),
            ])
        }
        p.contentView = host
        panel = p
        return p
    }
    func panelFrame(height: CGFloat) -> NSRect {
        guard let bw = statusItem.button?.window else { return NSRect(x: 0, y: 0, width: PANEL_WIDTH, height: height) }
        let bf = bw.frame
        let vis = (bw.screen ?? NSScreen.main)?.visibleFrame ?? bf
        var x = bf.midX - PANEL_WIDTH / 2
        x = min(x, vis.maxX - PANEL_WIDTH - 8)
        x = max(x, vis.minX + 8)
        let top = min(bf.minY - 6, vis.maxY)
        return NSRect(x: x, y: top - height, width: PANEL_WIDTH, height: height)
    }
    func openPanel() {
        guard statusItem.button != nil else { return }
        store.reloadIfChanged()
        let p = makePanelIfNeeded()
        panelClosing = false
        installMonitors()
        rebuildPanel()
        let wasVisible = p.isVisible
        if !wasVisible { p.alphaValue = 0 }
        p.orderFrontRegardless()
        p.makeKey()
        NSAnimationContext.runAnimationGroup { c in
            c.duration = 0.12
            p.animator().alphaValue = 1
        }
        refresh()
    }
    func closePanel() {
        guard let p = panel, p.isVisible, !panelClosing else { return }
        panelClosing = true
        removeMonitors()
        NSAnimationContext.runAnimationGroup({ c in
            c.duration = 0.12
            p.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            guard let self = self, self.panelClosing else { return }
            p.orderOut(nil)
            self.panelClosing = false
        })
    }
    func installMonitors() {
        guard globalMonitor == nil else { return }
        let mask: NSEvent.EventTypeMask = [.leftMouseDown, .rightMouseDown, .otherMouseDown]
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: mask) { [weak self] _ in
            guard let self = self else { return }
            let at = NSEvent.mouseLocation
            if let bf = self.statusItem.button?.window?.frame, bf.contains(at) { return }
            DispatchQueue.main.async { self.closePanel() }
        }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: mask) { [weak self] e in
            guard let self = self else { return e }
            if e.window === self.panel { return e }
            if let bf = self.statusItem.button?.window?.frame, bf.contains(NSEvent.mouseLocation) { return e }
            self.closePanel()
            return e
        }
    }
    func removeMonitors() {
        if let g = globalMonitor { NSEvent.removeMonitor(g) }
        if let l = localMonitor { NSEvent.removeMonitor(l) }
        globalMonitor = nil; localMonitor = nil
    }
    @objc func otherAppActivated(_ n: Notification) {
        let app = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
        if app?.processIdentifier != ProcessInfo.processInfo.processIdentifier { closePanel() }
    }
    @objc func openSettings() {
        closePanel()
        if settingsController == nil { settingsController = SettingsController(app: self) }
        settingsController?.show()
    }

    @objc func refresh() {
        if store.reloadIfChanged() { applySettingsChange(refetch: false) }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let d = self?.runCollector()
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.lastData = d
                self.renderTitle()
                if let d = d { self.engine.process(d) }
                if self.panelShown { self.rebuildPanel() }
                self.settingsController?.dataChanged()
            }
        }
    }
    func runCollector() -> J? {
        guard let out = runPython([]) else { return nil }
        return (try? JSONSerialization.jsonObject(with: out)) as? J
    }

    // вызывается после любого изменения настроек
    func applySettingsChange(refetch: Bool) {
        renderTitle()
        if panelShown { rebuildPanel() }
        if refetch { refresh() }
    }

    func renderTitle() {
        let (a, plain) = statusAttributed(lastData)
        statusItem.button?.attributedTitle = a
        statusItem.button?.toolTip = plain
    }

    // ---------- панель (вариант D)
    func buildPanel() -> NSView {
        let rootBox = ColorBox(); rootBox.fill = T.bg
        let root: NSView = rootBox
        root.frame = NSRect(x: 0, y: 0, width: PANEL_WIDTH, height: 10)
        let stack = vstack([], spacing: 0)
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: root.topAnchor),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            stack.widthAnchor.constraint(equalToConstant: PANEL_WIDTH),
        ])
        let accounts = lastData?["accounts"] as? [J] ?? []
        if let a = accounts.first(where: { ($0["active"] as? Bool) == true }) ?? accounts.first {
            stack.addArrangedSubview(heroView(a))
        } else {
            let box = ColorBox(); box.fill = T.accentSoft
            inset(lbl("Подписки не настроены", 13, .regular, T.warn), in: box, 18, 18, 16, 18)
            stack.addArrangedSubview(box)
        }
        let others = othersView(activeId: accounts.first(where: { ($0["active"] as? Bool) == true })?["id"] as? String)
        if let o = others { stack.addArrangedSubview(o) }
        stack.addArrangedSubview(footerView())
        for v in stack.arrangedSubviews { v.widthAnchor.constraint(equalToConstant: PANEL_WIDTH).isActive = true }
        root.layoutSubtreeIfNeeded()
        root.frame = NSRect(x: 0, y: 0, width: PANEL_WIDTH, height: stack.fittingSize.height)
        return root
    }

    func rebuildPanel() {
        let p = makePanelIfNeeded()
        let root = buildPanel()
        panelClip.subviews.forEach { $0.removeFromSuperview() }
        root.translatesAutoresizingMaskIntoConstraints = true
        root.frame = NSRect(origin: .zero, size: root.frame.size)
        panelClip.addSubview(root)
        p.setFrame(panelFrame(height: root.frame.height), display: true)
        p.invalidateShadow()
    }

    func heroView(_ a: J) -> NSView {
        let box = ColorBox(); box.fill = T.accentSoft
        let label = a["label"] as? String ?? "Claude"
        let email = a["email"] as? String ?? ""
        let topLeft = attrLbl(joined(part("● ", 12, .regular, T.ok),
                                     part("Сейчас работает · \(label)", 12, .regular, T.ink2)))
        let top = spread(topLeft, lbl(localPart(email), 12, .regular, T.ink2))
        let lim = a["limits"] as? J
        var rows: [NSView] = [top]

        if lim?["ok"] as? Bool == true {
            let fh = Win(lim?["five_hour"]), sd = Win(lim?["seven_day"])
            let ring = RingView(); ring.translatesAutoresizingMaskIntoConstraints = false
            let p5 = fh?.pct ?? 0
            ring.progress = CGFloat(p5) / 100; ring.color = severityColor(p5)
            let pctText = "\(Int(p5))%"
            var bigSize: CGFloat = 42
            while bigSize > 24, NSAttributedString(string: pctText, attributes: [.font: uiFont(bigSize, .bold)]).size().width > 78 { bigSize -= 2 }
            let center = vstack([lbl(pctText, bigSize, .bold), lbl("5 часов", 11, .regular, T.ink2)],
                                spacing: 0, align: .centerX)
            ring.addSubview(center)
            NSLayoutConstraint.activate([
                ring.widthAnchor.constraint(equalToConstant: RING), ring.heightAnchor.constraint(equalToConstant: RING),
                center.centerXAnchor.constraint(equalTo: ring.centerXAnchor),
                center.centerYAnchor.constraint(equalTo: ring.centerYAnchor),
            ])
            var cd: NSView
            if let f = fh, let s = f.secs {
                let big = attrLbl(joined(part(fmtHM(s), 22, .semibold, T.ink),
                                         part(f.hm.map { "  в \($0)" } ?? "", 12, .regular, C_DIM)))
                cd = vstack([lbl("Окно сбросится через", 12, .regular, T.ink2), big], spacing: 1)
            } else {
                cd = vstack([lbl("5-часовое окно", 12, .regular, T.ink2),
                             lbl("не начато", 22, .semibold, T.ink2)], spacing: 1)
            }
            var rightCol: [NSView] = [cd]
            if let w = sd {
                let head = spread(lbl("Неделя", 12, .regular, T.ink2),
                                  lbl("\(Int(w.pct))%", 12, .semibold, w.pct >= warnPct() ? severityColor(w.pct) : T.ink))
                let when: String
                if let s = w.secs { when = "через \(fmtDH(s))" + (w.dateShort.map { " · \($0)" } ?? "") }
                else { when = "окно не начато" }
                rightCol.append(vstack([head, makeBar(w.pct, height: 5), lbl(when, 11, .regular, C_DIM)], spacing: 4))
            }
            let rc = vstack(rightCol, spacing: 10)
            rc.widthAnchor.constraint(equalToConstant: PANEL_WIDTH - 36 - RING - 18).isActive = true
            let mid = hstack([ring, rc], spacing: 18)
            rows.append(mid)
        } else {
            let err = (lim?["error"] as? String) ?? ""
            rows.append(wrapLbl(errorText(err), 12, T.warn, width: PANEL_WIDTH - 36))
        }
        if store.bool("general", "show_renewal"),
           let r = (a["subscription"] as? J)?["renewal"] as? J,
           let d = r["date_local"] as? String, let inh = r["in_human"] as? String {
            rows.append(lbl("оплата ≈ \(d) · через \(inh)", 11, .regular, C_DIM))
        }
        inset(vstack(rows, spacing: 14), in: box, 18, 18, 16, 18)
        return box
    }

    func miniCell(_ cap: String, _ w: Win?) -> NSView {
        guard let w = w else {
            return vstack([spread(lbl(cap, 11, .regular, T.ink2), lbl("—", 11, .regular, T.ink2)),
                           makeBar(0, height: 4)], spacing: 3)
        }
        let hot = w.pct >= warnPct()
        let pctColor: NSColor = hot ? severityColor(w.pct) : T.ink
        let val = lbl("\(Int(w.pct))%", 11, w.pct > 0 ? .semibold : .regular, pctColor)
        return vstack([spread(lbl(cap, 11, .regular, T.ink2), val), makeBar(w.pct, height: 4)], spacing: 3)
    }
    func weekCaption(_ w: Win?) -> String {
        if let w = w, let s = w.secs, w.pct < 100 { return "неделя · \(fmtDH(s))" }
        return "неделя"
    }

    func othersView(activeId: String?) -> NSView? {
        let hiddenIds = store.strs("accounts", "hidden")
        var items: [String: J] = [:]
        var ids: [String] = []
        for a in lastData?["accounts"] as? [J] ?? [] {
            let id = a["id"] as? String ?? ""
            if id == activeId || hiddenIds.contains(id) || (a["hidden"] as? Bool) == true
                || (a["auto_hidden"] as? Bool) == true { continue }
            items[id] = a; ids.append(id)
        }
        if let cx = lastData?["codex"] as? J, !hiddenIds.contains("codex") {
            items["codex"] = cx; ids.append("codex")
        }
        guard !ids.isEmpty else { return nil }
        var views: [NSView] = [attrLbl(part("ДРУГИЕ ПОДПИСКИ", 10.5, .semibold, C_DIM, kern: 0.7))]
        for id in orderedIds(ids) {
            views.append(id == "codex" ? codexItem(items[id]!) : claudeItem(items[id]!))
        }
        let box = NSView(); box.translatesAutoresizingMaskIntoConstraints = false
        inset(vstack(views, spacing: 12), in: box, 14, 18, 14, 18)
        return box
    }

    func titleRow(_ left: NSAttributedString, _ status: Status?) -> NSView {
        let right: NSView? = status.map { st in
            st.2 != nil ? chip(st.0, fg: st.1, bg: st.2!) : lbl(st.0, 11, .medium, st.1)
        }
        return spread(attrLbl(left), right, baseline: true)
    }
    func grid(_ a: NSView, _ b: NSView) -> NSView {
        let s = hstack([a, b], spacing: 12, align: .top); s.distribution = .fillEqually
        return s
    }

    func claudeItem(_ a: J) -> NSView {
        let label = a["label"] as? String ?? "Claude"
        let email = a["email"] as? String ?? ""
        let title = joined(part(label, 13.5, .semibold, T.ink),
                           part(email.isEmpty ? "" : "  " + localPart(email), 12, .regular, T.ink2))
        let lim = a["limits"] as? J
        var rows: [NSView] = []
        if lim?["ok"] as? Bool == true {
            let fh = Win(lim?["five_hour"]), sd = Win(lim?["seven_day"])
            var status: Status?
            let full = [fh, sd].compactMap { $0 }.filter { $0.pct >= 100 }
            if let until = full.compactMap({ $0.full }).last { status = ("лимит до \(until)", T.alarm, T.alarmSoft) }
            else if (lim?["stale"] as? Bool) == true, let age = dint(lim?["age_seconds"]), age > 900 {
                status = ("данные \(agoText(age))", T.ink2, nil)
            }
            rows.append(titleRow(title, status))
            rows.append(grid(miniCell("5 ч", fh), miniCell(weekCaption(sd), sd)))
        } else {
            let err = (lim?["error"] as? String) ?? ""
            rows.append(titleRow(title, shortStatus(err)))
            rows.append(wrapLbl(errorText(err), 11, T.ink2, width: PANEL_WIDTH - 36))
        }
        if store.bool("general", "show_renewal"),
           let r = (a["subscription"] as? J)?["renewal"] as? J, let d = r["date_short"] as? String {
            rows.append(lbl("оплата ≈ \(d)", 10.5, .regular, C_DIM))
        }
        return vstack(rows, spacing: 6)
    }

    func codexItem(_ cx: J) -> NSView {
        let ok = cx["ok"] as? Bool == true
        let label = cx["label"] as? String ?? "Codex"
        let title = part(label, 13.5, .semibold, T.ink)
        var rows: [NSView] = []
        var status: Status?
        let fh = ok ? Win(cx["five_hour"]) : nil, sd = ok ? Win(cx["seven_day"]) : nil
        if ok {
            if [fh, sd].compactMap({ $0 }).contains(where: { $0.pct >= 100 }) {
                status = ("лимит до \([fh, sd].compactMap { $0 }.filter { $0.pct >= 100 }.compactMap { $0.full }.last ?? "?")", T.alarm, T.alarmSoft)
            } else if let n = dint((cx["reset_credits"] as? J)?["available"]), n > 0 {
                status = ("↺ \(n) \(plural(n, "сброс", "сброса", "сбросов"))", T.accent, T.accentSoft)
            }
        } else { status = ("нет данных", T.ink2, nil) }
        rows.append(titleRow(NSAttributedString(attributedString: joined(title)), status))
        if ok {
            if (fh?.rolled ?? false) || (sd?.rolled ?? false) {
                rows.append(lbl("окно закрылось · расход обнулён", 10.5, .regular, T.ink2))
            }
            rows.append(grid(miniCell("5 ч", fh), miniCell(weekCaption(sd), sd)))
            if (cx["source"] as? String) != "app-server" {
                let age = dint(cx["age_seconds"]).map { " · данные \(agoText($0))" } ?? ""
                rows.append(lbl("по журналам сессий\(age)", 10.5, .regular, T.ink2))
            }
        } else {
            let e = cx["error"] as? String ?? ""
            let human = e == "no_snapshot" ? "Локальный срез не собран"
                      : e == "no_sessions" ? "Codex ещё не запускался" : "Данные недоступны"
            rows.append(lbl(human, 11, .regular, T.ink2))
        }
        if let err = cx["app_server_error"] as? String, !err.isEmpty {
            rows.append(wrapLbl("app-server: \(err)", 10.5, T.warn, width: PANEL_WIDTH - 36))
        }
        return vstack(rows, spacing: 6)
    }

    func footerView() -> NSView {
        let box = NSView(); box.translatesAutoresizingMaskIntoConstraints = false
        let line = ColorBox(); line.fill = T.line
        line.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(line)
        let refreshBtn = IconButton(symbol: "arrow.clockwise", tip: "Обновить")
        refreshBtn.onClick = { [weak self] in self?.refresh() }
        let gear = IconButton(symbol: "gearshape", tip: "Настройки")
        gear.onClick = { [weak self] in self?.openSettings() }
        let buttons = hstack([refreshBtn, gear], spacing: 4)

        var parts: [String] = []
        let t = ((lastData?["tokens"] as? J)?["session_5h"] as? J)?["tokens"] as? J
        if store.bool("general", "show_tokens") {
            parts.append(t.map { "\(fmtTokens(dint($0["total"]) ?? 0)) токенов" } ?? "нет токенов за 5 ч")
        }
        if store.bool("general", "show_cost"), let t = t {
            parts.append("≈ $" + String(format: "%.2f", dnum(t["cost_usd"]) ?? 0))
        }
        let text = lbl(parts.joined(separator: " · "), 12, .regular, T.ink2)
        let row = spread(text, buttons)
        inset(row, in: box, 10, 18, 10, 18)
        NSLayoutConstraint.activate([
            line.topAnchor.constraint(equalTo: box.topAnchor),
            line.leadingAnchor.constraint(equalTo: box.leadingAnchor),
            line.trailingAnchor.constraint(equalTo: box.trailingAnchor),
            line.heightAnchor.constraint(equalToConstant: 1),
            row.heightAnchor.constraint(greaterThanOrEqualToConstant: 24),
            text.centerYAnchor.constraint(equalTo: buttons.centerYAnchor),
        ])
        return box
    }
}

// ---------------------------------------------------------------- окно настроек
struct AcctRow {
    let id: String
    let title: String
    let sub: String
    let subWarn: Bool
    let deletable: Bool
    let hideable: Bool
    let hidden: Bool
}

final class SelRow: NSTableRowView {
    override func drawSelection(in r: NSRect) {
        T.accentSoft.setFill()
        r.fill()
    }
    override var interiorBackgroundStyle: NSView.BackgroundStyle { .normal }
}

final class TabButton: NSView {
    var onClick: (() -> Void)?
    var selected = false { didSet { needsDisplay = true; label.textColor = selected ? .white : T.ink } }
    let label: NSTextField
    init(_ title: String) {
        label = lbl(title, 13, .regular, T.ink)
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
            heightAnchor.constraint(equalToConstant: 30),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirty: NSRect) {
        guard selected else { return }
        T.accent.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 7, yRadius: 6).fill()
    }
    override func mouseDown(with e: NSEvent) { onClick?() }
}

final class SettingsController: NSObject, NSTableViewDataSource, NSTableViewDelegate, NSWindowDelegate {
    unowned let app: AppDelegate
    let window: NSWindow
    let content = NSView()
    var tabs: [TabButton] = []
    var current = 0
    var rows: [AcctRow] = []
    var table: NSTableView?
    var statusLine: NSTextField?
    var previewLabel: NSTextField?
    var busy = false
    let dragType = NSPasteboard.PasteboardType("com.ccmonitor.app.row")
    static let tabNames = ["Аккаунты", "Строка меню", "Уведомления", "Общее"]

    init(app: AppDelegate) {
        self.app = app
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 804, height: 500),
                          styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: false)
        super.init()
        window.title = "Настройки CCMonitor"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()

        let side = ColorBox(); side.fill = T.bg2
        let bg = ColorBox(); bg.fill = T.bg
        bg.translatesAutoresizingMaskIntoConstraints = false
        content.translatesAutoresizingMaskIntoConstraints = false
        let root = NSView()
        window.contentView = root
        root.addSubview(bg); root.addSubview(side); root.addSubview(content)
        NSLayoutConstraint.activate([
            side.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            side.topAnchor.constraint(equalTo: root.topAnchor),
            side.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            side.widthAnchor.constraint(equalToConstant: 190),
            bg.leadingAnchor.constraint(equalTo: side.trailingAnchor),
            bg.topAnchor.constraint(equalTo: root.topAnchor), bg.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            bg.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            content.leadingAnchor.constraint(equalTo: side.trailingAnchor),
            content.topAnchor.constraint(equalTo: root.topAnchor), content.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            content.trailingAnchor.constraint(equalTo: root.trailingAnchor),
        ])
        let head = lbl("CCMonitor", 12, .regular, T.ink2)
        var views: [NSView] = [head]
        for (i, n) in SettingsController.tabNames.enumerated() {
            let t = TabButton(n); t.onClick = { [weak self] in self?.select(tab: i) }
            tabs.append(t); views.append(t)
        }
        let sideStack = vstack(views, spacing: 2)
        sideStack.setCustomSpacing(10, after: head)
        side.addSubview(sideStack)
        NSLayoutConstraint.activate([
            sideStack.topAnchor.constraint(equalTo: side.topAnchor, constant: 48),
            sideStack.leadingAnchor.constraint(equalTo: side.leadingAnchor, constant: 10),
            sideStack.trailingAnchor.constraint(equalTo: side.trailingAnchor, constant: -10),
        ])
        head.leadingAnchor.constraint(equalTo: sideStack.leadingAnchor, constant: 10).isActive = true
        select(tab: 0)
    }

    func show() {
        select(tab: current)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }
    func windowWillClose(_ n: Notification) { table = nil }

    func dataChanged() {
        guard window.isVisible else { return }
        switch current {
        case 0: if !busy { reloadAccounts() }
        case 1: updatePreview()
        default: break
        }
    }

    func select(tab i: Int) {
        current = i
        for (j, t) in tabs.enumerated() { t.selected = (j == i) }
        content.subviews.forEach { $0.removeFromSuperview() }
        table = nil; statusLine = nil; previewLabel = nil
        let page: NSView
        switch i {
        case 0: page = accountsPage()
        case 1: page = barPage()
        case 2: page = notifPage()
        default: page = generalPage()
        }
        page.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(page)
        NSLayoutConstraint.activate([
            page.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
            page.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            page.topAnchor.constraint(equalTo: content.topAnchor, constant: 44),
            page.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20),
        ])
    }

    func changed(refetch: Bool = false) { app.applySettingsChange(refetch: refetch) }
    func title(_ s: String) -> NSTextField { lbl(s, 22, .semibold) }
    func note(_ s: String) -> NSTextField { wrapLbl(s, 12, T.ink2, width: PAGE_W) }
    func formGrid(_ rows: [[NSView]]) -> NSGridView {
        let g = NSGridView(views: rows)
        g.translatesAutoresizingMaskIntoConstraints = false
        g.columnSpacing = 16; g.rowSpacing = 12
        g.column(at: 0).width = 170
        for i in 0..<g.numberOfRows { g.row(at: i).yPlacement = .center }
        return g
    }
    func rowCap(_ s: String) -> NSTextField { lbl(s, 13.5, .regular, T.ink2) }
    func pageStack(_ views: [NSView]) -> NSView {
        let s = vstack(views, spacing: 14, align: .leading)
        let page = NSView()
        s.translatesAutoresizingMaskIntoConstraints = false
        page.addSubview(s)
        NSLayoutConstraint.activate([
            s.topAnchor.constraint(equalTo: page.topAnchor),
            s.leadingAnchor.constraint(equalTo: page.leadingAnchor),
            s.trailingAnchor.constraint(equalTo: page.trailingAnchor),
        ])
        return page
    }

    // ---------- вкладка «Аккаунты»
    func buildRows() -> [AcctRow] {
        var all: [AcctRow] = []
        let hiddenIds = store.strs("accounts", "hidden")
        for a in app.lastData?["accounts"] as? [J] ?? [] {
            let id = a["id"] as? String ?? ""
            let label = a["label"] as? String ?? "Claude"
            let email = a["email"] as? String ?? "—"
            let active = (a["active"] as? Bool) == true
            let lim = a["limits"] as? J
            let since = a["no_access_since_local"] as? String
            let auto = (a["auto_hidden"] as? Bool) == true
            var bits = [email]
            var warn = false
            if let s = since {
                bits.append(auto ? "скрыта: нет доступа с \(s)" : "нет доступа с \(s)"); warn = true
            } else if active {
                var t = "активна"
                if let age = dint(lim?["age_seconds"]), lim?["ok"] as? Bool == true { t += " · данные \(agoText(age))" }
                bits.append(t)
            } else if let exp = a["refresh_expires_local"] as? String {
                bits.append("вход действует до \(exp)")
            } else if id == "keychain-active" {
                bits.append("вход через сам Claude Code")
            }
            all.append(AcctRow(id: id, title: label.hasPrefix("Claude") ? label : "Claude " + label,
                               sub: bits.joined(separator: " · "), subWarn: warn,
                               deletable: id.hasPrefix(ACCOUNT_ID_PREFIX), hideable: !active,
                               hidden: hiddenIds.contains(id) && !active))
        }
        if let cx = app.lastData?["codex"] as? J {
            var sub = "через расширение VS Code"
            if cx["ok"] as? Bool == true {
                if let n = dint((cx["reset_credits"] as? J)?["available"]), n > 0 {
                    sub += " · \(n) \(plural(n, "бесплатный сброс", "бесплатных сброса", "бесплатных сбросов"))"
                }
            } else { sub = "нет данных" }
            all.append(AcctRow(id: "codex", title: cx["label"] as? String ?? "Codex", sub: sub, subWarn: false,
                               deletable: false, hideable: true, hidden: hiddenIds.contains("codex")))
        }
        let ids = orderedIds(all.map { $0.id })
        return ids.compactMap { id in all.first { $0.id == id } }
    }

    func accountsPage() -> NSView {
        rows = buildRows()
        let head = title("Аккаунты")
        let hint = note("Порядок в панели — перетаскиванием. Активный аккаунт Claude Code всегда сверху.")
        let tv = NSTableView()
        tv.headerView = nil
        tv.rowHeight = 54
        tv.intercellSpacing = NSSize(width: 0, height: 1)
        tv.backgroundColor = .clear
        tv.gridStyleMask = .solidHorizontalGridLineMask
        tv.gridColor = T.ink.withAlphaComponent(0.06)
        tv.style = .plain
        tv.allowsEmptySelection = true
        let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("c"))
        col.resizingMask = .autoresizingMask
        tv.addTableColumn(col)
        tv.dataSource = self; tv.delegate = self
        tv.registerForDraggedTypes([dragType])
        tv.draggingDestinationFeedbackStyle = .gap
        table = tv
        let scroll = NSScrollView()
        scroll.documentView = tv
        scroll.drawsBackground = false; scroll.hasVerticalScroller = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        let card = ColorBox(); card.fill = T.card; card.stroke = T.line; card.radius = 12
        card.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(scroll)
        let h = min(CGFloat(max(rows.count, 1)) * 55 - 1, 275)
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: card.topAnchor), scroll.bottomAnchor.constraint(equalTo: card.bottomAnchor),
            scroll.leadingAnchor.constraint(equalTo: card.leadingAnchor), scroll.trailingAnchor.constraint(equalTo: card.trailingAnchor),
            card.heightAnchor.constraint(equalToConstant: h), card.widthAnchor.constraint(equalToConstant: PAGE_W),
        ])
        col.width = PAGE_W

        let add = PillButton("Добавить подписку Claude…", .primary)
        add.onClick = { [weak self] in self?.startLogin(target: "auto") }
        let relog = PillButton("Перевойти в выбранную", .secondary)
        relog.onClick = { [weak self] in self?.reloginSelected() }
        let btns = hstack([add, relog], spacing: 8)
        let st = lbl("", 12, .regular, T.ink2)
        statusLine = st
        let foot = note("«Добавить» открывает вход Anthropic в браузере; ключи сохраняются в Связку ключей. Аккаунт без доступа больше 3 дней прячется сам и помечается здесь.")
        return pageStack([head, hint, card, btns, st, foot])
    }
    func reloadAccounts() {
        let sel = table?.selectedRow ?? -1
        let selId = (sel >= 0 && sel < rows.count) ? rows[sel].id : nil
        rows = buildRows()
        table?.reloadData()
        if let id = selId, let i = rows.firstIndex(where: { $0.id == id }) {
            table?.selectRowIndexes(IndexSet(integer: i), byExtendingSelection: false)
        }
    }
    func setStatus(_ s: String, warn: Bool = false) {
        statusLine?.stringValue = s
        statusLine?.textColor = warn ? T.warn : T.ink2
    }

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }
    func tableView(_ tv: NSTableView, rowViewForRow row: Int) -> NSTableRowView? { SelRow() }
    func tableView(_ tv: NSTableView, viewFor tc: NSTableColumn?, row: Int) -> NSView? {
        let r = rows[row]
        let grip = lbl("⋮⋮", 13, .regular, C_DIM)
        let name = lbl(r.title, 13.5, .semibold)
        let sub = lbl(r.sub, 12, .regular, r.subWarn ? T.warn : T.ink2)
        let texts = vstack([name, sub], spacing: 2, align: .leading)
        texts.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let show = check("показывать", !r.hidden, size: 12, color: T.ink2) { [weak self] on in self?.setHidden(r.id, !on) }
        show.enabled = r.hideable
        show.setContentHuggingPriority(.required, for: .horizontal)
        var items: [NSView] = [grip, texts, show]
        if r.deletable {
            let del = PillButton("Удалить", .link)
            del.onClick = { [weak self] in self?.confirmDelete(r) }
            del.setContentHuggingPriority(.required, for: .horizontal)
            items.append(del)
        }
        let cell = NSView()
        let h = hstack(items, spacing: 12)
        h.distribution = .fill
        texts.setContentHuggingPriority(.defaultLow, for: .horizontal)
        inset(h, in: cell, 0, 14, 0, 14)
        return cell
    }
    func tableView(_ tv: NSTableView, pasteboardWriterForRow row: Int) -> NSPasteboardWriting? {
        let it = NSPasteboardItem(); it.setString(String(row), forType: dragType); return it
    }
    func tableView(_ tv: NSTableView, validateDrop info: NSDraggingInfo, proposedRow row: Int,
                   proposedDropOperation op: NSTableView.DropOperation) -> NSDragOperation {
        op == .above ? .move : []
    }
    func tableView(_ tv: NSTableView, acceptDrop info: NSDraggingInfo, row: Int,
                   dropOperation op: NSTableView.DropOperation) -> Bool {
        guard let s = info.draggingPasteboard.string(forType: dragType), let from = Int(s) else { return false }
        var to = row
        if from < to { to -= 1 }
        guard from != to else { return false }
        let r = rows.remove(at: from); rows.insert(r, at: to)
        tv.moveRow(at: from, to: to)
        store.set("accounts", "order", rows.map { $0.id })
        changed()
        return true
    }

    func setHidden(_ id: String, _ hide: Bool) {
        var h = store.strs("accounts", "hidden").filter { $0 != id }
        if hide { h.append(id) }
        store.set("accounts", "hidden", h)
        reloadAccounts()
        changed(refetch: true)
    }

    func confirmDelete(_ r: AcctRow) {
        let a = NSAlert()
        a.alertStyle = .warning
        a.messageText = "Удалить подписку «\(r.title)»?"
        a.informativeText = "Ключи доступа стираются из Связки ключей, кэш лимитов — тоже. Подписку можно добавить заново."
        let del = a.addButton(withTitle: "Удалить"); del.hasDestructiveAction = true
        a.addButton(withTitle: "Отмена")
        a.beginSheetModal(for: window) { [weak self] resp in
            guard resp == .alertFirstButtonReturn, let self = self else { return }
            self.setStatus("Удаляю…")
            DispatchQueue.global().async {
                let out = runPython(["--delete", r.id])
                let ok = (out.flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? J })?["ok"] as? Bool == true
                DispatchQueue.main.async {
                    if ok {
                        store.set("accounts", "order", store.strs("accounts", "order").filter { $0 != r.id })
                        store.set("accounts", "hidden", store.strs("accounts", "hidden").filter { $0 != r.id })
                        self.setStatus("Удалена: \(r.title)")
                        self.app.lastData = self.app.lastData.map { d in
                            var d = d
                            d["accounts"] = (d["accounts"] as? [J] ?? []).filter { ($0["id"] as? String) != r.id }
                            return d
                        }
                        self.reloadAccounts()
                        self.app.refresh()
                    } else { self.setStatus("Не удалось удалить \(r.title)", warn: true) }
                }
            }
        }
    }

    func reloginSelected() {
        let i = table?.selectedRow ?? -1
        guard i >= 0, i < rows.count else {
            return info("Выберите подписку в списке", "Нажмите на строку подписки, в которую нужно перевойти.")
        }
        let r = rows[i]
        guard r.id.hasPrefix(ACCOUNT_ID_PREFIX) else {
            return info("Эта строка не из CCMonitor",
                        r.id == "codex" ? "Codex читается из расширения VS Code — вход делается там."
                                        : "Активная подписка входит через сам Claude Code. Чтобы вести её отдельно, нажмите «Добавить подписку Claude…».")
        }
        startLogin(target: r.id)
    }
    func info(_ title: String, _ text: String) {
        let a = NSAlert(); a.messageText = title; a.informativeText = text
        a.beginSheetModal(for: window)
    }

    func startLogin(target: String) {
        guard !busy else { return }
        busy = true; setStatus("Готовлю ссылку входа…")
        DispatchQueue.global().async {
            let out = runPython(["--login-url"]).flatMap { String(data: $0, encoding: .utf8) }?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            DispatchQueue.main.async {
                guard out.hasPrefix("https://"), let url = URL(string: out) else {
                    self.busy = false; self.setStatus("Не удалось получить ссылку входа", warn: true); return
                }
                if ProcessInfo.processInfo.environment["CCMON_NO_OPEN"] == nil { NSWorkspace.shared.open(url) }
                self.askCode(target: target)
            }
        }
    }
    func askCode(target: String) {
        let a = NSAlert()
        a.messageText = target == "auto" ? "Добавить подписку Claude" : "Перевход в подписку"
        a.informativeText = "В браузере открылся вход Anthropic. Войдите в нужный аккаунт, скопируйте код со страницы и вставьте его сюда."
        let f = NSTextField(frame: NSRect(x: 0, y: 0, width: 360, height: 24))
        f.placeholderString = "код со страницы входа"
        a.accessoryView = f
        a.addButton(withTitle: "Войти"); a.addButton(withTitle: "Отмена")
        a.window.initialFirstResponder = f
        a.beginSheetModal(for: window) { [weak self] resp in
            guard let self = self else { return }
            let code = f.stringValue
            guard resp == .alertFirstButtonReturn, !code.trimmingCharacters(in: .whitespaces).isEmpty else {
                self.busy = false; self.setStatus("Вход отменён"); return
            }
            self.setStatus("Обмениваю код…")
            DispatchQueue.global().async {
                let out = runPython(["--login-code", target], stdin: code)
                let res = out.flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? J }
                DispatchQueue.main.async {
                    self.busy = false
                    if res?["ok"] as? Bool == true {
                        let id = res?["service"] as? String ?? ""
                        var order = store.strs("accounts", "order")
                        if !order.contains(id) { order.append(id); store.set("accounts", "order", order) }
                        store.set("accounts", "hidden", store.strs("accounts", "hidden").filter { $0 != id })
                        self.setStatus("Готово: \(res?["label"] as? String ?? "") · \(res?["email"] as? String ?? "")")
                        self.app.refresh()
                    } else {
                        self.setStatus(self.loginError(res?["error"] as? String ?? "нет ответа"), warn: true)
                    }
                }
            }
        }
    }
    func loginError(_ e: String) -> String {
        if e.hasPrefix("exchange_failed") { return "Anthropic отклонил код (\(e.dropFirst(16))). Нажмите «Добавить» ещё раз и вставьте новый код." }
        switch e {
        case "no_pending": return "Нет начатого входа — нажмите «Добавить» ещё раз."
        case "pending_expired": return "Ссылка входа устарела — нажмите «Добавить» ещё раз."
        case "state_mismatch": return "Код от другой ссылки — вставьте код со страницы, открытой последней."
        case "bad_code": return "Пустой код."
        case "no_free_slot": return "Все 9 мест под подписки заняты."
        case "keychain_failed": return "Не удалось записать в Связку ключей."
        default: return "Ошибка входа: \(e)"
        }
    }

    // ---------- вкладка «Строка меню»
    func barPage() -> NSView {
        let head = title("Строка меню")
        let d = app.lastData
        let active = (d?["accounts"] as? [J])?.first { ($0["active"] as? Bool) == true }
        let lim = active?["limits"] as? J ?? d?["limits"] as? J
        let fh = Win(lim?["five_hour"]), sd = Win(lim?["seven_day"])
        let both = "Оба: \(Int(fh?.pct ?? 0))% · \(Int(sd?.pct ?? 0))%"
        let p5 = Int(fh?.pct ?? 0)
        let src = popup([("Активный Claude", "active_claude"), ("Codex", "codex"), ("Claude и Codex", "both"),
                         ("Самый загруженный лимит", "most_loaded")], selected: store.str("menubar", "source"), width: 260) {
            store.set("menubar", "source", $0); self.barChanged()
        }
        let win = popup([("5 часов", "five_hour"), ("Неделя", "seven_day"), (both, "both")],
                        selected: store.str("menubar", "window"), width: 260) {
            store.set("menubar", "window", $0); self.barChanged()
        }
        let num = popup([("Израсходовано (\(p5)%)", "used"), ("Осталось (\(100 - p5)%)", "left")],
                        selected: store.str("menubar", "number"), width: 260) {
            store.set("menubar", "number", $0); self.barChanged()
        }
        let c1 = check("время до сброса", store.bool("menubar", "show_reset")) { store.set("menubar", "show_reset", $0); self.barChanged() }
        let c2 = check("значок-индикатор ◔", store.bool("menubar", "show_gauge")) { store.set("menubar", "show_gauge", $0); self.barChanged() }
        let c3 = check("только значок, без текста", store.bool("menubar", "icon_only")) { store.set("menubar", "icon_only", $0); self.barChanged() }
        let g = formGrid([[rowCap("Что показывать"), src], [rowCap("Окно"), win], [rowCap("Число"), num],
                          [rowCap("Дополнительно"), vstack([c1, c2, c3], spacing: 6, align: .leading)]])
        let cap = lbl("Предпросмотр", 12, .regular, T.ink2)
        let strip = ColorBox(); strip.fill = T.chip; strip.stroke = T.line; strip.radius = 10
        strip.translatesAutoresizingMaskIntoConstraints = false
        let pv = lbl("", 13, .medium)
        previewLabel = pv
        let clock = lbl(timeNow(), 13, .regular, T.ink2)
        let inner = hstack([pv, clock], spacing: 14)
        inner.translatesAutoresizingMaskIntoConstraints = false
        strip.addSubview(inner)
        NSLayoutConstraint.activate([
            inner.trailingAnchor.constraint(equalTo: strip.trailingAnchor, constant: -14),
            inner.topAnchor.constraint(equalTo: strip.topAnchor, constant: 8),
            inner.bottomAnchor.constraint(equalTo: strip.bottomAnchor, constant: -8),
            inner.leadingAnchor.constraint(greaterThanOrEqualTo: strip.leadingAnchor, constant: 14),
            strip.widthAnchor.constraint(equalToConstant: PAGE_W),
        ])
        let page = pageStack([head, g, cap, strip])
        updatePreview()
        return page
    }
    func timeNow() -> String {
        let f = DateFormatter(); f.dateFormat = "HH:mm"; return f.string(from: Date())
    }
    func barChanged() { updatePreview(); changed() }
    func updatePreview() {
        guard let pv = previewLabel else { return }
        pv.attributedStringValue = statusAttributed(app.lastData).0
    }

    // ---------- вкладка «Уведомления»
    func notifPage() -> NSView {
        let head = title("Уведомления")
        func on(_ key: String, _ v: Bool) {
            store.set("notify", key, v)
            if v { Notifier.shared.requestAuth() }
        }
        let f1 = NumField(value: store.int("notify", "warn1"), lo: 1, hi: 100) { store.set("notify", "warn1", $0) }
        let f2 = NumField(value: store.int("notify", "warn2"), lo: 1, hi: 100) { store.set("notify", "warn2", $0) }
        let r1 = hstack([check("Предупредить, когда лимит дошёл до", store.bool("notify", "warn1_on")) { on("warn1_on", $0) },
                         f1, lbl("%", 13)], spacing: 6)
        let r2 = hstack([check("Второе предупреждение на", store.bool("notify", "warn2_on")) { on("warn2_on", $0) },
                         f2, lbl("%", 13)], spacing: 6)
        let r3 = check("Лимит закончился", store.bool("notify", "exhausted")) { on("exhausted", $0) }
        let r4 = check("Окно сбросилось — можно работать", store.bool("notify", "reset")) { on("reset", $0) }
        let r5 = check("Аккаунт потерял доступ (нужен вход)", store.bool("notify", "access_lost")) { on("access_lost", $0) }
        let list = vstack([r1, r2, r3, r4, r5], spacing: 10, align: .leading)

        let allRows = buildRows().filter { !$0.hidden }
        let chosen = store.strs("notify", "accounts")
        var boxes: [CheckControl] = []
        func writeAccounts() {
            let on = boxes.filter { $0.on }
            boxes.forEach { $0.enabled = true }
            if on.count == 1 { on[0].enabled = false }
            let ids = on.map { $0.key }
            store.set("notify", "accounts", ids.count == boxes.count ? [String]() : ids)
        }
        for r in allRows {
            let b = check(r.title.replacingOccurrences(of: "Claude ", with: ""),
                          chosen.isEmpty || chosen.contains(r.id)) { _ in writeAccounts() }
            b.key = r.id
            boxes.append(b)
        }
        if boxes.filter({ $0.on }).count == 1 { boxes.first { $0.on }?.enabled = false }
        let wins = store.strs("notify", "windows")
        let w5 = check("5 часов", wins.contains("five_hour")) { _ in }
        let w7 = check("неделя", wins.contains("seven_day")) { _ in }
        let writeWins = { store.set("notify", "windows", [w5.on ? "five_hour" : nil, w7.on ? "seven_day" : nil].compactMap { $0 }) }
        w5.onChange = { _ in writeWins() }; w7.onChange = { _ in writeWins() }
        let g = formGrid([[rowCap("Для каких"), hstack(boxes, spacing: 14)], [rowCap("Окна"), hstack([w5, w7], spacing: 14)]])
        let foot = note("Каждый порог срабатывает один раз за окно.")
        return pageStack([head, list, g, foot])
    }

    // ---------- вкладка «Общее»
    func generalPage() -> NSView {
        let head = title("Общее")
        let poll = popup([("раз в 5 мин", "5"), ("раз в 10 мин", "10"), ("раз в 15 мин", "15")],
                         selected: String(store.int("general", "poll_minutes")), width: 200) {
            store.set("general", "poll_minutes", Int($0) ?? 5); self.changed(refetch: false)
        }
        let warn = NumField(value: store.int("general", "color_warn"), lo: 1, hi: 100) {
            store.set("general", "color_warn", $0); self.changed()
        }
        let crit = NumField(value: store.int("general", "color_crit"), lo: 1, hi: 100) {
            store.set("general", "color_crit", $0); self.changed()
        }
        let colors = hstack([lbl("оранжевый с", 13), warn, lbl("%, красный с", 13), crit, lbl("%", 13)], spacing: 8)
        let t1 = check("блок токенов за 5 часов", store.bool("general", "show_tokens")) { store.set("general", "show_tokens", $0); self.changed() }
        let t2 = check("условная стоимость в $", store.bool("general", "show_cost")) { store.set("general", "show_cost", $0); self.changed() }
        let t3 = check("дата оплаты подписки", store.bool("general", "show_renewal")) { store.set("general", "show_renewal", $0); self.changed() }
        let real = launchAtLoginEnabled()
        if store.bool("general", "launch_at_login") != real { store.set("general", "launch_at_login", real) }
        let t4 = check("запускать при входе в систему", real) { on in
            store.set("general", "launch_at_login", on); setLaunchAtLogin(on)
        }
        let g = formGrid([[rowCap("Опрос лимитов"), poll], [rowCap("Цвет полосы"), colors],
                          [rowCap("Панель"), vstack([t1, t2, t3], spacing: 6, align: .leading)], [rowCap("Система"), t4]])
        let foot = note("Чаще раза в 5 минут Anthropic не отвечает — сервер режет запросы.")
        let page = pageStack([head, g, foot])
        let quit = PillButton("Выйти из CCMonitor", .danger)
        quit.onClick = { quitForReal() }
        page.addSubview(quit)
        NSLayoutConstraint.activate([
            quit.trailingAnchor.constraint(equalTo: page.trailingAnchor),
            quit.bottomAnchor.constraint(equalTo: page.bottomAnchor),
        ])
        return page
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
