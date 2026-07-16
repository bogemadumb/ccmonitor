import AppKit

// CCMonitor — статусбар с лимитами Claude Code и токенами за сессию.
// Логика данных в collector.py; здесь опрос + красивая всплывающая панель (popover).

// python3 ищем по типичным путям (переносимость между машинами)
func findPython() -> String {
    for p in ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
    return "/usr/bin/python3"
}
// collector.py лежит внутри бандла (Resources); fallback — папка разработки
let COLLECTOR: String = {
    if let r = Bundle.main.resourcePath {
        let inBundle = r + "/collector.py"
        if FileManager.default.fileExists(atPath: inBundle) { return inBundle }
    }
    return NSHomeDirectory() + "/Documents/CCMonitor/collector.py"
}()
let PYTHON = findPython()
let REFRESH_SECONDS: TimeInterval = 30
let PANEL_WIDTH: CGFloat = 300
let PAD: CGFloat = 16

// ---------------------------------------------------------------- утилиты вида
func gauge(_ p: Double) -> String {
    switch p {
    case ..<12.5: return "○"; case ..<37.5: return "◔"; case ..<62.5: return "◑"
    case ..<87.5: return "◕"; default: return "●"
    }
}
func severityColor(_ pct: Double) -> NSColor {
    if pct >= 90 { return .systemRed }
    if pct >= 70 { return .systemOrange }
    return NSColor.controlAccentColor
}
func fmtTokens(_ n: Int) -> String {
    if n >= 1_000_000 { return String(format: "%.1fM", Double(n)/1_000_000) }
    if n >= 1_000 { return String(format: "%.0fk", Double(n)/1_000) }
    return String(n)
}
func lbl(_ s: String, _ size: CGFloat, _ weight: NSFont.Weight = .regular,
         _ color: NSColor = .labelColor) -> NSTextField {
    let t = NSTextField(labelWithString: s)
    t.font = .systemFont(ofSize: size, weight: weight)
    t.textColor = color
    t.translatesAutoresizingMaskIntoConstraints = false
    return t
}

// прогресс-бар со скруглением
final class BarView: NSView {
    var progress: CGFloat = 0
    var fill: NSColor = .systemBlue
    override var intrinsicContentSize: NSSize { NSSize(width: NSView.noIntrinsicMetric, height: 7) }
    override func draw(_ dirty: NSRect) {
        let h = bounds.height
        let track = NSBezierPath(roundedRect: bounds, xRadius: h/2, yRadius: h/2)
        NSColor.tertiaryLabelColor.withAlphaComponent(0.25).setFill(); track.fill()
        guard progress > 0 else { return }
        let w = max(h, bounds.width * min(progress, 1))
        let fr = NSRect(x: 0, y: 0, width: w, height: h)
        let path = NSBezierPath(roundedRect: fr, xRadius: h/2, yRadius: h/2)
        fill.setFill(); path.fill()
    }
}

// строка "подпись слева — значение справа"
func headerRow(_ caption: String, _ value: NSView) -> NSView {
    let row = NSView(); row.translatesAutoresizingMaskIntoConstraints = false
    let c = lbl(caption.uppercased(), 10, .semibold, .secondaryLabelColor)
    row.addSubview(c); row.addSubview(value)
    value.translatesAutoresizingMaskIntoConstraints = false
    NSLayoutConstraint.activate([
        c.leadingAnchor.constraint(equalTo: row.leadingAnchor),
        c.centerYAnchor.constraint(equalTo: row.centerYAnchor),
        value.trailingAnchor.constraint(equalTo: row.trailingAnchor),
        value.centerYAnchor.constraint(equalTo: row.centerYAnchor),
        row.heightAnchor.constraint(equalToConstant: 16),
    ])
    return row
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    let popover = NSPopover()
    var timer: Timer?
    var lastData: [String: Any]?

    func applicationDidFinishLaunching(_ n: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "CC …"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePanel)
        popover.behavior = .transient
        popover.animates = true
        popover.contentViewController = NSViewController()
        popover.contentViewController?.view = NSView(frame: NSRect(x: 0, y: 0, width: PANEL_WIDTH, height: 120))
        // режим оффскрин-снимка панели в PNG (для проверки вёрстки, без показа на экране)
        if let i = CommandLine.arguments.firstIndex(of: "--snapshot"),
           i + 1 < CommandLine.arguments.count {
            lastData = runCollector()
            let path = CommandLine.arguments[i + 1]
            let v = buildPanel(opaque: true)
            v.layoutSubtreeIfNeeded()
            if let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) {
                v.cacheDisplay(in: v.bounds, to: rep)
                if let png = rep.representation(using: .png, properties: [:]) {
                    try? png.write(to: URL(fileURLWithPath: path))
                }
            }
            exit(0)
        }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: REFRESH_SECONDS, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    @objc func togglePanel() {
        if popover.isShown { popover.performClose(nil) } else { openPanel() }
    }
    func openPanel() {
        guard let b = statusItem.button else { return }
        rebuildPanel()
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: b.bounds, of: b, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        refresh()
    }

    @objc func refresh() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let d = self?.runCollector()
            DispatchQueue.main.async {
                self?.lastData = d
                self?.renderTitle(d)
                if self?.popover.isShown == true { self?.rebuildPanel() }
            }
        }
    }
    func runCollector() -> [String: Any]? {
        let p = Process(); p.executableURL = URL(fileURLWithPath: PYTHON)
        p.arguments = [COLLECTOR]; let pipe = Pipe()
        p.standardOutput = pipe; p.standardError = Pipe()
        do {
            try p.run()
            let out = pipe.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
            return try JSONSerialization.jsonObject(with: out) as? [String: Any]
        } catch { return nil }
    }

    // ---------- строка статусбара
    func renderTitle(_ d: [String: Any]?) {
        let lim = d?["limits"] as? [String: Any]
        if let lim = lim, lim["ok"] as? Bool == true,
           let fh = lim["five_hour"] as? [String: Any], let pct = fh["used_percentage"] as? Double {
            let short = fh["resets_in_short"] as? String ?? "?"
            let stale = (lim["stale"] as? Bool) == true
            let title = "\(gauge(pct)) \(Int(pct))%\(stale ? "*" : "") · \(short)"
            let color: NSColor = pct >= 90 ? .systemRed : pct >= 70 ? .systemOrange : .labelColor
            statusItem.button?.attributedTitle = NSAttributedString(string: title, attributes: [
                .font: NSFont.menuBarFont(ofSize: 0), .foregroundColor: color])
            return
        }
        let err = (lim?["error"] as? String) ?? ""
        let title = err == "token_expired" ? "CC ⚠︎" : "CC …"
        let color: NSColor = err == "token_expired" ? .systemOrange : .secondaryLabelColor
        statusItem.button?.attributedTitle = NSAttributedString(string: title, attributes: [
            .font: NSFont.menuBarFont(ofSize: 0), .foregroundColor: color])
    }

    // ---------- всплывающая панель
    func buildPanel(opaque: Bool) -> NSView {
        let root: NSView
        if opaque {
            let v = NSView(); v.wantsLayer = true
            v.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
            root = v
        } else {
            let v = NSVisualEffectView(); v.material = .popover
            v.blendingMode = .behindWindow; v.state = .active
            root = v
        }
        root.frame = NSRect(x: 0, y: 0, width: PANEL_WIDTH, height: 10)

        let stack = NSStackView(); stack.orientation = .vertical
        stack.alignment = .leading; stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.edgeInsets = NSEdgeInsets(top: PAD, left: PAD, bottom: PAD, right: PAD)
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: root.topAnchor),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            stack.widthAnchor.constraint(equalToConstant: PANEL_WIDTH),
        ])
        func fullWidth(_ v: NSView) {
            v.widthAnchor.constraint(equalToConstant: PANEL_WIDTH - 2*PAD).isActive = true
        }

        // header
        let head = NSView(); head.translatesAutoresizingMaskIntoConstraints = false
        let t1 = lbl("Claude Code", 14, .bold)
        head.addSubview(t1)
        NSLayoutConstraint.activate([
            t1.leadingAnchor.constraint(equalTo: head.leadingAnchor),
            t1.centerYAnchor.constraint(equalTo: head.centerYAnchor),
            head.heightAnchor.constraint(equalToConstant: 20),
        ])
        stack.addArrangedSubview(head); fullWidth(head)

        let lim = lastData?["limits"] as? [String: Any]
        if lim?["ok"] as? Bool == true {
            if let fh = lim?["five_hour"] as? [String: Any] {
                for v in limitBlock("5 часов", fh) { stack.addArrangedSubview(v); fullWidth(v) }
            }
            if let sd = lim?["seven_day"] as? [String: Any] {
                for v in limitBlock("7 дней", sd, fullDate: true) { stack.addArrangedSubview(v); fullWidth(v) }
            }
        } else {
            let err = (lim?["error"] as? String) ?? ""
            let human = err == "token_expired" ? "Токен истёк — открой Claude Code"
                      : err == "network" ? "Нет соединения"
                      : err == "http_429" ? "Лимиты обновляются…"
                      : "Лимиты недоступны"
            let col: NSColor = (err == "http_429") ? .secondaryLabelColor : .systemOrange
            let w = lbl(human, 12, .regular, col); stack.addArrangedSubview(w); fullWidth(w)
        }

        stack.addArrangedSubview(divider()); fullWidth(stack.arrangedSubviews.last!)

        let tok = lastData?["tokens"] as? [String: Any]
        for v in tokenBlock("Токены за сессию (5 ч)", (tok?["session_5h"] as? [String: Any])) {
            stack.addArrangedSubview(v); fullWidth(v)
        }

        stack.addArrangedSubview(divider()); fullWidth(stack.arrangedSubviews.last!)
        let footer = footerRow(); stack.addArrangedSubview(footer); fullWidth(footer)

        root.layoutSubtreeIfNeeded()
        let h = stack.fittingSize.height
        root.frame = NSRect(x: 0, y: 0, width: PANEL_WIDTH, height: h)
        return root
    }

    func rebuildPanel() {
        let root = buildPanel(opaque: false)
        popover.contentSize = root.frame.size
        popover.contentViewController?.view = root
    }

    func limitBlock(_ caption: String, _ o: [String: Any], fullDate: Bool = false) -> [NSView] {
        let pct = o["used_percentage"] as? Double ?? 0
        let val = lbl("\(Int(pct))%", 13, .bold, severityColor(pct))
        let header = headerRow(caption, val)
        let bar = BarView(); bar.progress = CGFloat(pct)/100; bar.fill = severityColor(pct)
        bar.translatesAutoresizingMaskIntoConstraints = false
        bar.heightAnchor.constraint(equalToConstant: 7).isActive = true
        let when = fullDate ? (o["resets_full"] as? String ?? o["resets_hm"] as? String ?? "?")
                            : (o["resets_hm"] as? String ?? "?")
        let inh = o["resets_in_human"] as? String ?? "?"
        let sub = lbl("сброс \(when) · через \(inh)", 10.5, .regular, .secondaryLabelColor)
        return [header, bar, sub]
    }

    func tokenBlock(_ caption: String, _ node: [String: Any]?) -> [NSView] {
        guard let t = node?["tokens"] as? [String: Any] else {
            return [headerRow(caption, lbl("нет данных", 12, .regular, .secondaryLabelColor))]
        }
        let total = t["total"] as? Int ?? 0
        let cost = t["cost_usd"] as? Double ?? 0
        let big = NSStackView(); big.orientation = .horizontal; big.spacing = 6
        big.addArrangedSubview(lbl(fmtTokens(total), 13, .bold))
        big.addArrangedSubview(lbl("~$\(String(format: "%.2f", cost))", 11, .regular, .tertiaryLabelColor))
        let header = headerRow(caption, big)
        let inp = t["input"] as? Int ?? 0, out = t["output"] as? Int ?? 0
        let cw = t["cache_creation"] as? Int ?? 0, cr = t["cache_read"] as? Int ?? 0
        let detail = lbl("ввод \(fmtTokens(inp)) · вывод \(fmtTokens(out)) · кэш \(fmtTokens(cw))↑ \(fmtTokens(cr))↓",
                         10.5, .regular, .tertiaryLabelColor)
        return [header, detail]
    }

    func divider() -> NSView {
        let v = NSBox(); v.boxType = .separator
        v.translatesAutoresizingMaskIntoConstraints = false
        v.heightAnchor.constraint(equalToConstant: 1).isActive = true
        return v
    }

    func footerRow() -> NSView {
        let row = NSView(); row.translatesAutoresizingMaskIntoConstraints = false
        func linkButton(_ title: String, _ sel: Selector) -> NSButton {
            let b = NSButton(title: title, target: self, action: sel)
            b.isBordered = false; b.bezelStyle = .inline
            b.attributedTitle = NSAttributedString(string: title, attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .medium),
                .foregroundColor: NSColor.controlAccentColor])
            b.translatesAutoresizingMaskIntoConstraints = false
            return b
        }
        let upd = linkButton("↻ Обновить", #selector(refresh))
        let quit = linkButton("Выход", #selector(NSApplication.terminate(_:)))
        row.addSubview(upd); row.addSubview(quit)
        NSLayoutConstraint.activate([
            upd.leadingAnchor.constraint(equalTo: row.leadingAnchor),
            upd.centerYAnchor.constraint(equalTo: row.centerYAnchor),
            quit.trailingAnchor.constraint(equalTo: row.trailingAnchor),
            quit.centerYAnchor.constraint(equalTo: row.centerYAnchor),
            row.heightAnchor.constraint(equalToConstant: 18),
        ])
        return row
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
