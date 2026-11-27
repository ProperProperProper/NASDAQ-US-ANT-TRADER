import Cocoa

let botPlist = "/Users/local.local/Library/LaunchAgents/com.local.us-ant-engine.plist"
let engineBinary = "/Users/local.local/Projects/TradingSystem/ai_gateway/target/release/trading_engine"

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let status = NSTextField(labelWithString: "Checking…")
    let account = NSTextField(labelWithString: "SIMULATE · checking local account")
    let openD = NSTextField(labelWithString: "Checking…")
    let gateway = NSTextField(labelWithString: "Checking…")
    let strategy = NSTextField(labelWithString: "Checking…")
    let cache = NSTextField(labelWithString: "Checking…")
    let sweepStatus = NSTextField(labelWithString: "No interval sweep running")
    let sweepMetrics = NSTextField(labelWithString: "Candidates: 0 / 100,000 · AI proposals: 0 · Profitable saved: 0")
    let winner = NSTextField(labelWithString: "Winning strategy: no completed result")
    let progress = NSProgressIndicator()
    let progressLabel = NSTextField(labelWithString: "0%")
    let output = NSTextView()
    var sweepProcess: Process?
    var sweepBuffer = ""

    func applicationDidFinishLaunching(_ notification: Notification) {
        terminateStaleAppInstances()
        window = NSWindow(contentRect: NSMakeRect(0, 0, 720, 560), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "US.ANT Paper Trader"
        window.center()
        buildUI()
        window.makeKeyAndOrderFront(nil)
        refresh()
    }

    private func terminateStaleAppInstances() {
        guard let bundleID = Bundle.main.bundleIdentifier else { return }
        let currentPID = ProcessInfo.processInfo.processIdentifier
        let stale = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
            .filter { $0.processIdentifier != currentPID }
        for application in stale {
            application.terminate()
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 2) {
                if !application.isTerminated { application.forceTerminate() }
            }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let children = [sweepProcess].compactMap { $0 }.filter { $0.isRunning }
        guard !children.isEmpty else { return .terminateNow }
        for child in children { child.terminate() }
        DispatchQueue.global(qos: .utility).async {
            let deadline = Date().addingTimeInterval(5)
            while Date() < deadline && children.contains(where: { $0.isRunning }) {
                Thread.sleep(forTimeInterval: 0.1)
            }
            DispatchQueue.main.async {
                for child in children where child.isRunning { child.interrupt() }
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }

    func label(_ text: String, size: CGFloat = 13) -> NSTextField {
        let item = NSTextField(labelWithString: text)
        item.font = NSFont.systemFont(ofSize: size)
        return item
    }

    func button(_ title: String, _ action: Selector) -> NSButton {
        let item = NSButton(title: title, target: self, action: action)
        item.bezelStyle = .rounded
        return item
    }

    func buildUI() {
        let root = NSView(frame: NSMakeRect(0, 0, 720, 560))
        window.contentView = root

        let title = label("US.ANT Paper Trader", size: 24)
        title.font = NSFont.boldSystemFont(ofSize: 24)
        title.frame = NSMakeRect(24, 505, 420, 32)
        root.addSubview(title)
        let subtitle = label("Native control panel · Moomoo SIMULATE only", size: 13)
        subtitle.textColor = .secondaryLabelColor
        subtitle.frame = NSMakeRect(26, 480, 400, 20)
        root.addSubview(subtitle)

        let runtime = NSBox(frame: NSMakeRect(20, 275, 680, 185))
        runtime.title = "Runtime"
        runtime.titlePosition = .atTop
        root.addSubview(runtime)
        let botCaption = label("Bot status")
        botCaption.frame = NSMakeRect(18, 132, 100, 20)
        runtime.addSubview(botCaption)
        status.frame = NSMakeRect(125, 132, 300, 20)
        runtime.addSubview(status)
        let accountCaption = label("Account")
        accountCaption.frame = NSMakeRect(18, 107, 100, 20)
        runtime.addSubview(accountCaption)
        account.frame = NSMakeRect(125, 107, 300, 20)
        runtime.addSubview(account)
        let openDCaption = label("Moomoo OpenD")
        openDCaption.frame = NSMakeRect(18, 82, 100, 20)
        runtime.addSubview(openDCaption)
        openD.frame = NSMakeRect(125, 82, 300, 20)
        runtime.addSubview(openD)
        let gatewayCaption = label("AI gateway")
        gatewayCaption.frame = NSMakeRect(18, 57, 100, 20)
        runtime.addSubview(gatewayCaption)
        gateway.frame = NSMakeRect(125, 57, 300, 20)
        runtime.addSubview(gateway)
        let strategyCaption = label("Deployed config")
        strategyCaption.frame = NSMakeRect(18, 32, 100, 20)
        runtime.addSubview(strategyCaption)
        strategy.frame = NSMakeRect(125, 32, 300, 20)
        runtime.addSubview(strategy)
        let cacheCaption = label("1m cache")
        cacheCaption.frame = NSMakeRect(18, 7, 100, 20)
        runtime.addSubview(cacheCaption)
        cache.frame = NSMakeRect(125, 7, 300, 20)
        runtime.addSubview(cache)
        let refreshButton = button("Refresh", #selector(refresh))
        refreshButton.frame = NSMakeRect(430, 125, 100, 30)
        runtime.addSubview(refreshButton)
        let startButton = button("Start paper bot", #selector(startBot))
        startButton.frame = NSMakeRect(535, 125, 125, 30)
        runtime.addSubview(startButton)
        let stopButton = button("Stop bot", #selector(stopBot))
        stopButton.frame = NSMakeRect(430, 85, 100, 30)
        runtime.addSubview(stopButton)

        let research = NSBox(frame: NSMakeRect(20, 20, 680, 240))
        research.title = "Interval research"
        research.titlePosition = .atTop
        root.addSubview(research)
        sweepStatus.frame = NSMakeRect(18, 190, 600, 20)
        research.addSubview(sweepStatus)
        sweepMetrics.frame = NSMakeRect(18, 165, 640, 20)
        sweepMetrics.font = NSFont.systemFont(ofSize: 11)
        sweepMetrics.textColor = .secondaryLabelColor
        research.addSubview(sweepMetrics)
        winner.frame = NSMakeRect(18, 140, 640, 20)
        winner.font = NSFont.systemFont(ofSize: 11)
        research.addSubview(winner)
        progress.isIndeterminate = false
        progress.minValue = 0
        progress.maxValue = 1
        progress.doubleValue = 0
        progress.style = .bar
        progress.frame = NSMakeRect(475, 188, 145, 16)
        research.addSubview(progress)
        progressLabel.alignment = .right
        progressLabel.frame = NSMakeRect(625, 188, 35, 20)
        research.addSubview(progressLabel)
        let scroll = NSScrollView(frame: NSMakeRect(18, 45, 644, 90))
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        output.isEditable = false
        output.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        output.backgroundColor = .controlBackgroundColor
        scroll.documentView = output
        research.addSubview(scroll)
        let sweepButton = button("Run 100k randomized + AI tests", #selector(startSweep))
        sweepButton.frame = NSMakeRect(18, 12, 245, 30)
        research.addSubview(sweepButton)
    }

    func runCommand(_ path: String, _ arguments: [String], completion: @escaping (Int32, String) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = arguments
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe
            do {
                try process.run()
                process.waitUntilExit()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                completion(process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
            } catch { completion(-1, error.localizedDescription) }
        }
    }

    @objc func refresh() {
        runCommand("/usr/bin/nc", ["-z", "127.0.0.1", "11111"]) { code, _ in
            DispatchQueue.main.async { self.openD.stringValue = code == 0 ? "READY · 127.0.0.1:11111" : "OFFLINE" }
        }
        let root = "/Users/local.local/Projects/TradingSystem"
        runCommand("/usr/bin/nc", ["-z", "127.0.0.1", "8081"]) { sentinelCode, _ in
            self.runCommand("/usr/bin/nc", ["-z", "127.0.0.1", "8788"]) { gatewayCode, _ in
                DispatchQueue.main.async {
                    self.gateway.stringValue = sentinelCode == 0 && gatewayCode == 0 ? "READY · GPT-OSS Sentinel + gateway" : "OFFLINE · local AI unavailable"
                }
            }
        }
        runCommand("/usr/bin/python3", ["\(root)/scripts/session_monitor.py", "--once"]) { _, output in
            let line = output.split(separator: "\n").last(where: { $0.first == "{" })
            let data = line.flatMap { $0.data(using: .utf8) }
            let value = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let environment = value?["environment"] as? String ?? "UNKNOWN"
            let accountID = value?["account"] as? Int ?? 0
            let side = value?["position_side"] as? String ?? "UNKNOWN"
            let qty = value?["qty"] as? Int ?? 0
            let orders = value?["active_orders"] as? Int ?? 0
            DispatchQueue.main.async {
                self.account.stringValue = value == nil ? "SIMULATE · account unavailable" : "\(environment) · \(accountID) · \(side) \(qty) · orders \(orders)"
            }
        }
        runCommand("/usr/bin/python3", ["-c", "import json, os; p='\(root)/.research_scheduler_state.json'; d=json.load(open(p)) if os.path.exists(p) else {}; print('scheduled: ' + str(d.get('state','NOT RUN')) + (' · ' + str(d.get('session')) if d.get('session') else ''))"]) { _, output in
            DispatchQueue.main.async {
                if self.sweepProcess?.isRunning != true {
                    let value = output.trimmingCharacters(in: .whitespacesAndNewlines)
                    self.sweepStatus.stringValue = value.isEmpty ? "Scheduled research: NOT RUN" : value
                }
            }
        }
        runCommand("/usr/bin/python3", ["-c", "import json; p=json.load(open('\(root)/paper_bot_live_strategy.json')); print(f'{p.get(\"interval\",\"?\")}m · lookback {p.get(\"params\",{}).get(\"grid_lookback\",\"?\")} · {p.get(\"mode\",\"?\")}')"]) { _, output in
            DispatchQueue.main.async { self.strategy.stringValue = output.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "UNAVAILABLE" : output.trimmingCharacters(in: .whitespacesAndNewlines) }
        }
        runCommand("/usr/bin/sqlite3", ["\(root)/quant_dashboard.sqlite3", "select coalesce(max(time_key),'none') || ' · ' || count(*) || ' candles' from candles where symbol='US.ANT' and interval_minutes=1;"]) { _, output in
            DispatchQueue.main.async { self.cache.stringValue = output.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "UNAVAILABLE" : output.trimmingCharacters(in: .whitespacesAndNewlines) }
        }
        runCommand("/usr/bin/python3", ["-c", "import glob,json,os; files=sorted(glob.glob('\(root)/strategy_lab_runs/ANT-MINUTE-*/summary.json')); d=json.load(open(files[-1])) if files else {}; w=d.get('winner') or {}; p=w.get('params') or {}; print('Winning strategy: ' + str(w.get('interval','none')) + ' · PnL ' + str(w.get('total_return_pct','?')) + '% · DD ' + str(w.get('max_drawdown_pct','?')) + '% · trades ' + str(w.get('trades','?')) + ' · params ' + json.dumps(p, separators=(',',':')))"]) { _, output in
            DispatchQueue.main.async { self.winner.stringValue = output.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Winning strategy: no completed result" : output.trimmingCharacters(in: .whitespacesAndNewlines) }
        }
        runCommand("/usr/bin/python3", ["-c", "import glob,json; f=sorted(glob.glob('\(root)/strategy_lab_runs/ANT-MINUTE-*/summary.json')); d=json.load(open(f[-1])) if f else {}; r=d.get('results') or []; print(json.dumps({'run':f[-1].split('/')[-2] if f else 'none','candidates':d.get('randomized_candidates',0),'ai':d.get('ai_proposals',0),'saved':d.get('profitable_saved',0),'rows':['%s  %+.3f%%  %s trades' % (x.get('interval','?'), (x.get('best') or {}).get('stats',{}).get('total_return_pct',0), (x.get('best') or {}).get('stats',{}).get('trades',0)) for x in r]}))"]) { _, output in
            DispatchQueue.main.async {
                guard let data = output.data(using: .utf8), let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
                let candidates = value["candidates"] as? Int ?? 0
                let ai = value["ai"] as? Int ?? 0
                let saved = value["saved"] as? Int ?? 0
                self.sweepMetrics.stringValue = "Candidates: \(String(format: "%,d", candidates)) · AI proposals: \(ai) · Profitable saved: \(saved) · Run \(value["run"] as? String ?? "none")"
                if let rows = value["rows"] as? [String] {
                    self.output.string = "INTERVAL   RETURN       EQUITY       TRADES\n" + rows.joined(separator: "\n")
                }
            }
        }
        runCommand(engineBinary, ["status"]) { code, output in
            let paper = output.split(separator: "\n").first(where: { $0.hasPrefix("paper=") }) ?? ""
            let running = code == 0 && paper.contains("paper=enabled") && !paper.contains("pid=0")
            let pid = paper.split(separator: "pid=").last.map(String.init) ?? ""
            DispatchQueue.main.async {
                self.status.stringValue = running ? "RUNNING · SIMULATE · PID \(pid)" : "STOPPED"
                if self.account.stringValue.isEmpty { self.account.stringValue = "SIMULATE · account unavailable" }
            }
        }
    }

    @objc func startBot() {
        runCommand("/bin/launchctl", ["bootstrap", "gui/\(getuid())", botPlist]) { _, _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                self.runCommand(engineBinary, ["bot-start"]) { _, _ in self.refresh() }
            }
        }
    }

    @objc func stopBot() {
        runCommand(engineBinary, ["bot-stop"]) { _, _ in self.refresh() }
    }

    @objc func startSweep() {
        if sweepProcess?.isRunning == true { return }
        sweepStatus.stringValue = "Running 100k randomized tests over 5 sessions…"
        sweepMetrics.stringValue = "Candidates: 0 / 100,000 · AI proposals: shown on completion · Profitable saved: 0"
        progress.doubleValue = 0
        progressLabel.stringValue = "0%"
        output.string = "INTERVAL   RETURN       EQUITY       TRADES\nWaiting for interval results…"
        sweepBuffer = ""
        let root = "/Users/local.local/Projects/TradingSystem"
        runCommand("/usr/bin/nc", ["-z", "127.0.0.1", "8788"]) { code, _ in
            if code == 0 {
                self.launchSweep(root: root)
                return
            }
            self.runCommand("/bin/launchctl", ["bootstrap", "gui/\(getuid())", botPlist]) { _, _ in }
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.launchSweep(root: root) }
        }
    }

    func launchSweep(root: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        let today = String(ISO8601DateFormatter().string(from: Date()).prefix(10))
        process.arguments = ["\(root)/scripts/app_minute_interval_sweep.py", "--symbol", "US.ANT", "--start", "2026-01-01", "--end", today, "--total-candidates", "100000", "--ai"]
        process.currentDirectoryURL = URL(fileURLWithPath: root)
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if !data.isEmpty, let text = String(data: data, encoding: .utf8) {
                DispatchQueue.main.async { self.handleSweepChunk(text) }
            }
        }
        process.terminationHandler = { task in
            DispatchQueue.main.async {
                if task.terminationStatus == 0 { self.progress.doubleValue = 1; self.progressLabel.stringValue = "100%" }
                self.sweepStatus.stringValue = task.terminationStatus == 0 ? "Sweep complete" : "Sweep failed (exit \(task.terminationStatus))"
                if task.terminationStatus == 0 { self.refresh() }
            }
        }
        sweepProcess = process
        try? process.run()
    }

    func handleSweepChunk(_ text: String) {
        output.string += text
        output.scrollToEndOfDocument(nil)
        sweepBuffer += text
        let lines = sweepBuffer.components(separatedBy: "\n")
        sweepBuffer = lines.last ?? ""
        for line in lines.dropLast() {
            guard let data = line.data(using: .utf8), let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            if let fraction = value["progress"] as? Double {
                progress.doubleValue = min(1, max(0, fraction))
                progressLabel.stringValue = "\(Int(fraction * 100))%"
            }
            let tested = value["tested"] as? Int ?? 0
            let total = value["total"] as? Int ?? 100000
            let saved = value["profitable_saved"] as? Int ?? 0
            if value["tested"] != nil || value["profitable_saved"] != nil {
                self.sweepMetrics.stringValue = "Candidates: \(String(format: "%,d", tested)) / \(String(format: "%,d", total)) · AI proposals: shown on completion · Profitable saved: \(saved)"
            }
            if let interval = value["interval"] as? String {
                sweepStatus.stringValue = "Testing \(interval) · \(progressLabel.stringValue)"
                if value["return_pct"] != nil {
                    let result = value["return_pct"] as? Double ?? 0
                    let equity = value["ending_equity"] as? Double ?? 0
                    let trades = value["trades"] as? Int ?? 0
                    self.output.string += "\n\(interval.padding(toLength: 10, withPad: " ", startingAt: 0)) \(String(format: "%+.3f%%", result).padding(toLength: 11, withPad: " ", startingAt: 0)) \(String(format: "%.2f", equity).padding(toLength: 11, withPad: " ", startingAt: 0)) \(trades)"
                    self.output.scrollToEndOfDocument(nil)
                }
            }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)
app.run()
