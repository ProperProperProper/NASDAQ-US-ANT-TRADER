import Cocoa

let botPlist = "/Users/local.local/Library/LaunchAgents/com.local.trading-engine.plist"
let engineBinary = "/Users/local.local/Projects/TradingSystem/ai_gateway/target/release/trading_engine"
let projectRoot = "/Users/local.local/Projects/TradingSystem"

func padL(_ str: String, _ length: Int) -> String {
    if str.count >= length { return str }
    return String(repeating: " ", count: length - str.count) + str
}

func padR(_ str: String, _ length: Int) -> String {
    if str.count >= length { return str }
    return str + String(repeating: " ", count: length - str.count)
}

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
    let positionsOutput = NSTextView()
    let configsOutput = NSTextView()
    let tabView = NSTabView()
    let symbolField = NSTextField(string: "US.MRNA")
    let multiSymbolField = NSTextField(string: "US.MRNA,US.ANT,US.NVDA,US.MU,US.SNDK,US.OMH,US.APP")
    var sweepProcess: Process?
    var sweepBuffer = ""

    func applicationDidFinishLaunching(_ notification: Notification) {
        terminateStaleAppInstances()
        window = NSWindow(contentRect: NSMakeRect(0, 0, 940, 720), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "NASDAQ Paper Trading Desk"
        window.minSize = NSSize(width: 900, height: 650)
        buildUI()
        window.center()
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
        let root = NSView(frame: NSMakeRect(0, 0, 940, 720))
        root.autoresizingMask = [.width, .height]
        window.contentView = root

        let title = label("NASDAQ Multi-Symbol Paper Trading Desk", size: 24)
        title.font = NSFont.boldSystemFont(ofSize: 24)
        title.frame = NSMakeRect(24, 665, 760, 32)
        title.autoresizingMask = [.minYMargin, .width]
        root.addSubview(title)
        let subtitle = label("Execution safety · independent strategies · AI research · Moomoo SIMULATE only", size: 13)
        subtitle.textColor = .secondaryLabelColor
        subtitle.frame = NSMakeRect(26, 640, 760, 20)
        subtitle.autoresizingMask = [.minYMargin, .width]
        root.addSubview(subtitle)

        let runtime = NSBox(frame: NSMakeRect(20, 410, 900, 210))
        runtime.autoresizingMask = [.width, .minYMargin]
        runtime.title = "Live execution & safety"
        runtime.titlePosition = .atTop
        root.addSubview(runtime)
        let botCaption = label("Bot status")
        botCaption.frame = NSMakeRect(18, 157, 110, 20)
        runtime.addSubview(botCaption)
        status.frame = NSMakeRect(140, 157, 485, 20)
        status.autoresizingMask = [.width]
        runtime.addSubview(status)
        let accountCaption = label("Account")
        accountCaption.frame = NSMakeRect(18, 130, 110, 20)
        runtime.addSubview(accountCaption)
        account.frame = NSMakeRect(140, 130, 485, 20)
        account.autoresizingMask = [.width]
        runtime.addSubview(account)
        let openDCaption = label("Moomoo OpenD")
        openDCaption.frame = NSMakeRect(18, 103, 110, 20)
        runtime.addSubview(openDCaption)
        openD.frame = NSMakeRect(140, 103, 485, 20)
        openD.autoresizingMask = [.width]
        runtime.addSubview(openD)
        let gatewayCaption = label("AI gateway")
        gatewayCaption.frame = NSMakeRect(18, 76, 110, 20)
        runtime.addSubview(gatewayCaption)
        gateway.frame = NSMakeRect(140, 76, 485, 20)
        gateway.autoresizingMask = [.width]
        runtime.addSubview(gateway)
        let strategyCaption = label("Deployed config")
        strategyCaption.frame = NSMakeRect(18, 49, 110, 20)
        runtime.addSubview(strategyCaption)
        strategy.frame = NSMakeRect(140, 49, 485, 20)
        strategy.autoresizingMask = [.width]
        runtime.addSubview(strategy)
        let cacheCaption = label("1m cache")
        cacheCaption.frame = NSMakeRect(18, 22, 110, 20)
        runtime.addSubview(cacheCaption)
        cache.frame = NSMakeRect(140, 22, 485, 20)
        cache.autoresizingMask = [.width]
        runtime.addSubview(cache)
        let refreshButton = button("Refresh", #selector(refresh))
        refreshButton.frame = NSMakeRect(650, 150, 105, 32)
        refreshButton.autoresizingMask = [.minXMargin]
        runtime.addSubview(refreshButton)
        let startButton = button("Start paper bot", #selector(startBot))
        startButton.frame = NSMakeRect(760, 150, 120, 32)
        startButton.autoresizingMask = [.minXMargin]
        runtime.addSubview(startButton)
        let stopButton = button("Stop bot", #selector(stopBot))
        stopButton.frame = NSMakeRect(650, 108, 105, 32)
        stopButton.autoresizingMask = [.minXMargin]
        runtime.addSubview(stopButton)
        symbolField.frame = NSMakeRect(760, 112, 120, 26)
        symbolField.autoresizingMask = [.minXMargin]
        symbolField.placeholderString = "US.MRNA"
        symbolField.isEditable = false
        runtime.addSubview(symbolField)
        let symbolButton = button("Apply portfolio", #selector(applySymbol))
        symbolButton.frame = NSMakeRect(650, 66, 230, 32)
        symbolButton.autoresizingMask = [.minXMargin]
        runtime.addSubview(symbolButton)

        tabView.frame = NSMakeRect(20, 20, 900, 380)
        tabView.autoresizingMask = [.width, .height]
        root.addSubview(tabView)

        // --- Tab 1: Live Positions & P/L ---
        let tabPositions = NSTabViewItem(identifier: "positions")
        tabPositions.label = "Open Positions & P/L"
        let posView = NSView(frame: tabView.bounds)
        posView.autoresizingMask = [.width, .height]
        let posScroll = NSScrollView(frame: NSMakeRect(12, 12, 876, 320))
        posScroll.autoresizingMask = [.width, .height]
        posScroll.hasVerticalScroller = true
        posScroll.borderType = .bezelBorder
        positionsOutput.isEditable = false
        positionsOutput.isRichText = false
        positionsOutput.frame = posScroll.contentView.bounds
        positionsOutput.autoresizingMask = [.width, .height]
        positionsOutput.isHorizontallyResizable = false
        positionsOutput.textContainer?.widthTracksTextView = true
        positionsOutput.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        positionsOutput.backgroundColor = .controlBackgroundColor
        posScroll.documentView = positionsOutput
        posView.addSubview(posScroll)
        tabPositions.view = posView
        tabView.addTabViewItem(tabPositions)

        // --- Tab 2: Strategy Lab & AI Research ---
        let tabResearch = NSTabViewItem(identifier: "research")
        tabResearch.label = "Strategy Lab & AI Research"
        let resView = NSView(frame: tabView.bounds)
        resView.autoresizingMask = [.width, .height]

        sweepStatus.frame = NSMakeRect(12, 305, 610, 20)
        sweepStatus.autoresizingMask = [.width, .minYMargin]
        resView.addSubview(sweepStatus)
        sweepMetrics.frame = NSMakeRect(12, 282, 850, 18)
        sweepMetrics.autoresizingMask = [.width, .minYMargin]
        sweepMetrics.font = NSFont.systemFont(ofSize: 11)
        sweepMetrics.textColor = .secondaryLabelColor
        resView.addSubview(sweepMetrics)
        winner.frame = NSMakeRect(12, 260, 850, 18)
        winner.autoresizingMask = [.width, .minYMargin]
        winner.font = NSFont.systemFont(ofSize: 11)
        resView.addSubview(winner)
        progress.isIndeterminate = false
        progress.minValue = 0
        progress.maxValue = 1
        progress.doubleValue = 0
        progress.style = .bar
        progress.frame = NSMakeRect(650, 305, 175, 16)
        progress.autoresizingMask = [.minXMargin, .minYMargin]
        resView.addSubview(progress)
        progressLabel.alignment = .right
        progressLabel.frame = NSMakeRect(835, 303, 45, 20)
        progressLabel.autoresizingMask = [.minXMargin, .minYMargin]
        resView.addSubview(progressLabel)
        let scroll = NSScrollView(frame: NSMakeRect(12, 52, 876, 200))
        scroll.autoresizingMask = [.width, .height]
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        output.isEditable = false
        output.frame = scroll.contentView.bounds
        output.autoresizingMask = [.width, .height]
        output.isHorizontallyResizable = false
        output.textContainer?.widthTracksTextView = true
        output.font = NSFont.monospacedSystemFont(ofSize: 10.5, weight: .regular)
        output.backgroundColor = .controlBackgroundColor
        scroll.documentView = output
        resView.addSubview(scroll)
        let sweepButton = button("Test 1–10m → AI optimise every symbol", #selector(startSweep))
        sweepButton.frame = NSMakeRect(12, 12, 335, 32)
        sweepButton.autoresizingMask = [.maxXMargin, .maxYMargin]
        resView.addSubview(sweepButton)
        multiSymbolField.frame = NSMakeRect(355, 15, 533, 26)
        multiSymbolField.autoresizingMask = [.width, .maxYMargin]
        multiSymbolField.placeholderString = "US.MRNA,US.ANT,US.NVDA,US.MU,US.SNDK,US.OMH,US.APP"
        resView.addSubview(multiSymbolField)
        tabResearch.view = resView
        tabView.addTabViewItem(tabResearch)

        // --- Tab 3: Deployed Strategy Configs ---
        let tabConfigs = NSTabViewItem(identifier: "configs")
        tabConfigs.label = "Deployed Configs"
        let confView = NSView(frame: tabView.bounds)
        confView.autoresizingMask = [.width, .height]
        let confScroll = NSScrollView(frame: NSMakeRect(12, 12, 876, 320))
        confScroll.autoresizingMask = [.width, .height]
        confScroll.hasVerticalScroller = true
        confScroll.borderType = .bezelBorder
        configsOutput.isEditable = false
        configsOutput.isRichText = false
        configsOutput.frame = confScroll.contentView.bounds
        configsOutput.autoresizingMask = [.width, .height]
        configsOutput.isHorizontallyResizable = false
        configsOutput.textContainer?.widthTracksTextView = true
        configsOutput.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        configsOutput.backgroundColor = .controlBackgroundColor
        confScroll.documentView = configsOutput
        confView.addSubview(confScroll)
        tabConfigs.view = confView
        tabView.addTabViewItem(tabConfigs)
        tabView.selectTabViewItem(at: 0)
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
        runCommand("/usr/bin/python3", ["-c", "import json; print(json.load(open('\(projectRoot)/trading_system.json'))['active_symbol'])"]) { _, output in
            DispatchQueue.main.async { self.symbolField.stringValue = output.trimmingCharacters(in: .whitespacesAndNewlines) }
        }
        runCommand("/usr/bin/python3", ["-c", "import json; print(','.join(json.load(open('\(projectRoot)/trading_system.json'))['active_symbols']))"]) { _, output in
            DispatchQueue.main.async { self.multiSymbolField.stringValue = output.trimmingCharacters(in: .whitespacesAndNewlines) }
        }
        runCommand("/usr/bin/nc", ["-z", "127.0.0.1", "11111"]) { code, _ in
            DispatchQueue.main.async { self.openD.stringValue = code == 0 ? "READY · 127.0.0.1:11111" : "OFFLINE" }
        }
        let root = projectRoot
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
            let totalAssets = value?["total_assets"] as? Double ?? 0.0
            let cash = value?["cash"] as? Double ?? 0.0
            let marketVal = value?["market_val"] as? Double ?? 0.0
            let rawPositions = value?["positions"] as? [[String: Any]] ?? []

            DispatchQueue.main.async {
                self.account.stringValue = value == nil ? "SIMULATE · account unavailable" : "\(environment) · \(accountID) · \(side) \(qty) · orders \(orders)"

                var table = String(format: "ACCOUNT OVERVIEW · %@ #%d\n", environment, accountID)
                table += String(format: "Total Assets: $%-12.2f  |  Market Value: $%-12.2f  |  Cash: $%-12.2f\n\n", totalAssets, marketVal, cash)

                let h = padR("SYMBOL", 8) + " " + padR("COMPANY", 18) + " " + padL("SHARES", 8) + "    " + padL("AVG COST", 10) + "    " + padL("CURRENT", 10) + "    " + padL("UNREALIZED P/L", 14) + "    " + padL("RETURN", 8) + "\n"
                table += h
                table += String(repeating: "-", count: h.count - 1) + "\n"

                var totalPl = 0.0
                if rawPositions.isEmpty {
                    table += "No open positions (flat portfolio)\n"
                } else {
                    for pos in rawPositions {
                        let code = (pos["code"] as? String ?? "?").replacingOccurrences(of: "US.", with: "")
                        let name = pos["stock_name"] as? String ?? ""
                        let pQty = pos["qty"] as? Double ?? 0.0
                        let cost = pos["cost_price"] as? Double ?? 0.0
                        let current = pos["nominal_price"] as? Double ?? 0.0
                        let plVal = pos["pl_val"] as? Double ?? 0.0
                        let plRatio = (pos["pl_ratio"] as? Double ?? 0.0) * 1.0
                        totalPl += plVal
                        let plSign = plVal >= 0 ? "+" : "-"
                        let plRatioSign = plRatio >= 0 ? "+" : "-"
                        let plStr = String(format: "%@$%.2f", plSign, abs(plVal))
                        let pctStr = String(format: "%@%.2f%%", plRatioSign, abs(plRatio))
                        let costStr = String(format: "$%.2f", cost)
                        let curStr = String(format: "$%.2f", current)
                        let qtyStr = String(format: "%.0f", pQty)
                        let shortName = name.count > 18 ? String(name.prefix(17)) + "…" : name
                        let row = padR(code, 8) + " " + padR(shortName, 18) + " " + padL(qtyStr, 8) + "    " + padL(costStr, 10) + "    " + padL(curStr, 10) + "    " + padL(plStr, 14) + "    " + padL(pctStr, 8) + "\n"
                        table += row
                    }
                    table += String(repeating: "-", count: h.count - 1) + "\n"
                    let plTotalSign = totalPl >= 0 ? "+" : "-"
                    table += String(format: "TOTAL UNREALIZED P/L: %@$%.2f (across %d open positions)\n", plTotalSign, abs(totalPl), rawPositions.count)
                }
                let monoFont = NSFont.monospacedSystemFont(ofSize: 11.5, weight: .regular)
                let attrStr = NSAttributedString(string: table, attributes: [
                    .font: monoFont,
                    .foregroundColor: NSColor.textColor
                ])
                self.positionsOutput.textStorage?.setAttributedString(attrStr)
            }
        }
        runCommand("/usr/bin/python3", ["-c", "import sys,json,collections; sys.path.insert(0,'\(root)'); from trading_config import active_symbols,research_dir; states=[(json.load(open(p)) if p.exists() else {}).get('state','NOT RUN') for s in active_symbols() for p in [research_dir(s)/'scheduler_state.json']]; c=collections.Counter(states); print('Research queue · ' + ' · '.join(str(c[k])+' '+k.lower() for k in sorted(c)))"]) { _, output in
            DispatchQueue.main.async {
                if self.sweepProcess?.isRunning != true {
                    let value = output.trimmingCharacters(in: .whitespacesAndNewlines)
                    self.sweepStatus.stringValue = value.isEmpty ? "Scheduled research: NOT RUN" : value
                }
            }
        }
        runCommand("/usr/bin/python3", ["-c", "import sys,json; sys.path.insert(0,'\(root)'); from trading_config import active_symbols,strategy_path; values=[json.load(open(strategy_path(s))) for s in active_symbols()]; print(str(len(values))+' independent configs · intervals '+','.join(str(x) for x in sorted(set(p.get('interval','?') for p in values)))+' minutes · per-symbol allocation '+format(95/len(values),'.2f')+'%')"]) { _, output in
            DispatchQueue.main.async { self.strategy.stringValue = output.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "UNAVAILABLE" : output.trimmingCharacters(in: .whitespacesAndNewlines) }
        }
        runCommand("/usr/bin/python3", ["-c", "import sys,sqlite3; sys.path.insert(0,'\(root)'); from trading_config import active_symbols; db=sqlite3.connect('\(root)/quant_dashboard.sqlite3'); values=[db.execute(\"select count(*) from candles where symbol=? and interval_minutes=1\",(s,)).fetchone()[0] for s in active_symbols()]; print(str(sum(x>0 for x in values))+'/'+str(len(values))+' symbols ready · '+format(sum(values),',')+' cached 1-minute candles')"]) { _, output in
            DispatchQueue.main.async { self.cache.stringValue = output.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "UNAVAILABLE" : output.trimmingCharacters(in: .whitespacesAndNewlines) }
        }
        runCommand("/usr/bin/python3", ["-c", "import sys,json,collections; sys.path.insert(0,'\(root)'); from trading_config import active_symbols,strategy_path; ids=[str(json.load(open(strategy_path(s))).get('run_id','unknown')) for s in active_symbols()]; baseline=sum(x=='INITIAL-BASELINE' for x in ids); print('Config sources · '+str(len(ids)-baseline)+' research-selected · '+str(baseline)+' baseline · each promoted independently')"]) { _, output in
            DispatchQueue.main.async { self.winner.stringValue = output.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Independent per-symbol configs unavailable" : output.trimmingCharacters(in: .whitespacesAndNewlines) }
        }
        runCommand("/usr/bin/python3", ["-c", "import sys,glob,json; sys.path.insert(0,'\(root)'); from trading_config import active_symbol,active_symbols,research_dir,strategy_path; f=sorted(glob.glob(str(research_dir(active_symbol())/'runs/*/summary.json'))); d=json.load(open(f[-1])) if f else {}; r=d.get('results') or []; configs=[json.load(open(strategy_path(s))) for s in active_symbols()]; print(json.dumps({'run':f[-1].split('/')[-2] if f else 'none','candidates':d.get('randomized_candidates',0),'ai':d.get('ai_proposals',0),'saved':d.get('profitable_saved',0),'rows':['%s  %+.3f%%  %s trades' % (x.get('interval','?'), (x.get('best') or {}).get('stats',{}).get('total_return_pct',0), (x.get('best') or {}).get('stats',{}).get('trades',0)) for x in r],'configs':configs}))"]) { _, output in
            DispatchQueue.main.async {
                guard let data = output.data(using: .utf8), let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
                let candidates = value["candidates"] as? Int ?? 0
                let ai = value["ai"] as? Int ?? 0
                let saved = value["saved"] as? Int ?? 0
                self.sweepMetrics.stringValue = "Candidates: \(candidates) · AI proposals: \(ai) · Profitable saved: \(saved) · Run \(value["run"] as? String ?? "none")"
                if let configs = value["configs"] as? [[String: Any]] {
                    let allocation = configs.isEmpty ? 0.0 : 95.0 / Double(configs.count)
                    let confHeader = padR("SYMBOL", 8) + " " + padR("INTERVAL", 9) + " " + padR("LOOKBACK", 9) + " " + padR("GRIDS", 6) + " " + padR("ALLOC", 8) + " " + padR("TAKE PROFIT", 12) + " " + padR("STOP LOSS", 10) + " " + padR("TRAIL", 9) + " " + "ENTRY / EXIT MODES\n"
                    let configRows = configs.map { config -> String in
                        let symbol = (config["symbol"] as? String ?? "?").replacingOccurrences(of: "US.", with: "")
                        let interval = "\(config["interval"] as? Int ?? 0)m"
                        let params = config["params"] as? [String: Any] ?? [:]
                        let lookback = "\(params["grid_lookback"] as? Int ?? 0)"
                        let grids = "\(params["grid_intervals"] as? Int ?? 0)"
                        let entry = params["entry_mode"] as? String ?? "?"
                        let exit = params["exit_mode"] as? String ?? "?"
                        let takeProfit = String(format: "%.2f%%", (params["take_profit_pct"] as? Double ?? 0) * 100)
                        let stopLoss = String(format: "%.2f%%", (params["stop_loss_pct"] as? Double ?? 0) * 100)
                        let trail = String(format: "%.2f%%", (params["trailing_stop_pct"] as? Double ?? 0) * 100)
                        let allocStr = String(format: "%.2f%%", allocation)
                        return padR(symbol, 8) + " " + padR(interval, 9) + " " + padR(lookback, 9) + " " + padR(grids, 6) + " " + padR(allocStr, 8) + " " + padR(takeProfit, 12) + " " + padR(stopLoss, 10) + " " + padR(trail, 9) + " " + "\(entry) / \(exit)"
                    }
                    let confText = "INDEPENDENT PER-SYMBOL STRATEGY CONFIGURATIONS (SIMULATE)\n" + String(repeating: "-", count: confHeader.count - 1) + "\n" + confHeader + String(repeating: "-", count: confHeader.count - 1) + "\n" + configRows.joined(separator: "\n")
                    let monoFont = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
                    self.configsOutput.textStorage?.setAttributedString(NSAttributedString(string: confText, attributes: [.font: monoFont, .foregroundColor: NSColor.textColor]))
                    if self.sweepProcess?.isRunning != true && self.output.string.isEmpty {
                        self.output.string = "Ready. Select a test or AI sweep below.\n"
                    }
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

    @objc func applySymbol() {
        let requested = multiSymbolField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !requested.isEmpty else { return }
        status.stringValue = "VALIDATING SYMBOL…"
        runCommand(engineBinary, ["portfolio-set", requested]) { code, output in
            DispatchQueue.main.async {
                self.status.stringValue = code == 0 ? output.trimmingCharacters(in: .whitespacesAndNewlines) : "SYMBOL CHANGE REJECTED · \(output)"
                self.refresh()
            }
        }
    }

    @objc func startSweep() {
        if sweepProcess?.isRunning == true { return }
        sweepStatus.stringValue = "Benchmarking every symbol across 1–10m, then AI-optimising each config…"
        sweepMetrics.stringValue = "Stage 1: 70 interval tests · Stage 2: independent 100k + AI search per symbol · 2 workers"
        progress.doubleValue = 0
        progressLabel.stringValue = "0%"
        output.string = "PORTFOLIO PIPELINE\n1. Benchmark all symbols at 1–10m\n2. AI optimise each symbol independently\nWaiting to start…\n"
        sweepBuffer = ""
        let root = projectRoot
        runCommand("/usr/bin/nc", ["-z", "127.0.0.1", "8788"]) { code, _ in
            if code == 0 {
                self.launchSweep(root: root)
                return
            }
            self.runCommand("/bin/launchctl", ["bootstrap", "gui/\(getuid())", botPlist]) { _, _ in }
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.launchSweep(root: root) }
        }
    }

    @objc func startMultiSymbolTest() {
        if sweepProcess?.isRunning == true { return }
        let symbols = multiSymbolField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !symbols.isEmpty else { return }
        sweepStatus.stringValue = "Testing \(symbols) across 1–10 minute intervals…"
        sweepMetrics.stringValue = "Two evaluator workers · paper research only"
        progress.isIndeterminate = true
        progress.startAnimation(nil)
        output.string = "SYMBOL       INTERVAL   RETURN      DRAWDOWN    TRADES\n"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        let end = Date()
        let start = Calendar.current.date(byAdding: .day, value: -30, to: end) ?? end
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withFullDate]
        process.arguments = ["\(projectRoot)/scripts/multi_symbol_backtest.py", "--symbols", symbols,
                             "--start", iso.string(from: start), "--end", iso.string(from: end), "--workers", "2"]
        process.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
            } else if let text = String(data: data, encoding: .utf8) {
                DispatchQueue.main.async { self.handleMultiSymbolChunk(text) }
            }
        }
        process.terminationHandler = { task in
            pipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                self.progress.stopAnimation(nil)
                self.progress.isIndeterminate = false
                self.sweepStatus.stringValue = task.terminationStatus == 0 ? "Multi-symbol test complete" : "Multi-symbol test failed (exit \(task.terminationStatus))"
            }
        }
        sweepBuffer = ""
        sweepProcess = process
        do { try process.run() } catch {
            progress.stopAnimation(nil)
            progress.isIndeterminate = false
            sweepStatus.stringValue = "Could not start test: \(error.localizedDescription)"
        }
    }

    func handleMultiSymbolChunk(_ text: String) {
        sweepBuffer += text
        let lines = sweepBuffer.components(separatedBy: "\n")
        sweepBuffer = lines.last ?? ""
        for line in lines.dropLast() {
            guard let data = line.data(using: .utf8),
                  let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                output.string += line + "\n"
                continue
            }
            if value["status"] as? String == "COMPLETED" {
                let symbol = value["symbol"] as? String ?? "?"
                let interval = value["interval"] as? Int ?? 0
                let result = value["total_return_pct"] as? Double ?? 0
                let drawdown = value["max_drawdown_pct"] as? Double ?? 0
                let trades = value["trades"] as? Int ?? 0
                output.string += String(format: "%-12@ %2dm       %+7.3f%%    %+7.3f%%    %d\n", symbol as NSString, interval, result, drawdown, trades)
            } else if value["state"] as? String == "FETCHING" {
                sweepStatus.stringValue = "Fetching \(value["symbol"] as? String ?? "symbol")…"
            } else if value["state"] as? String == "COMPLETED" {
                let tested = value["tested"] as? Int ?? 0
                sweepMetrics.stringValue = "Completed \(tested) symbol/interval tests · results saved locally"
            }
            output.scrollToEndOfDocument(nil)
        }
    }

    func launchSweep(root: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["\(root)/scripts/portfolio_ai_pipeline.py", "--symbols", multiSymbolField.stringValue, "--total-candidates", "100000", "--workers", "2"]
        process.currentDirectoryURL = URL(fileURLWithPath: root)
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
            } else if let text = String(data: data, encoding: .utf8) {
                DispatchQueue.main.async { self.handleSweepChunk(text) }
            }
        }
        process.terminationHandler = { task in
            pipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                if task.terminationStatus == 0 { self.progress.doubleValue = 1; self.progressLabel.stringValue = "100%" }
                self.sweepStatus.stringValue = task.terminationStatus == 0 ? "Portfolio benchmark and AI optimisation complete" : "Portfolio pipeline failed (exit \(task.terminationStatus))"
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
            if let stage = value["pipeline_stage"] as? String {
                let symbol = value["symbol"] as? String ?? "portfolio"
                switch stage {
                case "BENCHMARK_1_TO_10": self.sweepStatus.stringValue = "Stage 1/2 · benchmarking all symbols across 1–10m"
                case "AI_OPTIMISE": self.sweepStatus.stringValue = "Stage 2/2 · AI optimising \(symbol) independently"
                case "SYMBOL_COMPLETE":
                    let index = value["index"] as? Int ?? 0
                    let total = value["total_symbols"] as? Int ?? 1
                    self.sweepMetrics.stringValue = "AI optimisation complete for \(index)/\(total) symbols"
                    self.progress.doubleValue = Double(index) / Double(max(1, total))
                    self.progressLabel.stringValue = "\(Int(self.progress.doubleValue * 100))%"
                case "WAITING_FOR_RESEARCH_LOCK": self.sweepStatus.stringValue = "Waiting for scheduled research to release \(symbol)…"
                default: break
                }
            }
            if let fraction = value["progress"] as? Double {
                progress.doubleValue = min(1, max(0, fraction))
                progressLabel.stringValue = "\(Int(fraction * 100))%"
            }
            let tested = value["tested"] as? Int ?? 0
            let total = value["total"] as? Int ?? 100000
            let saved = value["profitable_saved"] as? Int ?? 0
            if value["tested"] != nil || value["profitable_saved"] != nil {
                self.sweepMetrics.stringValue = "Candidates: \(tested) / \(total) · AI proposals: shown on completion · Profitable saved: \(saved)"
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
