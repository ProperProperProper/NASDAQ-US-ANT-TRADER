//! Single launchd-owned supervisor for the configurable paper and AI runtime.
use std::{env, fs::OpenOptions, io::{Read, Write}, os::unix::{fs::PermissionsExt, net::UnixStream, process::CommandExt}, path::{Path, PathBuf}, process::{Child, Command, Stdio}, sync::atomic::{AtomicBool, Ordering}, thread, time::{Duration, Instant}};

const ROOT: &str = "/Users/local.local/Projects/TradingSystem";
const SOCKET: &str = "/tmp/paper-trading-engine.sock";
const BOT_PAUSED: &str = "/Users/local.local/Projects/TradingSystem/.trading_engine_bot_paused";
static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn handle_signal(_: i32) { STOP.store(true, Ordering::SeqCst); }
unsafe extern "C" { fn signal(number: i32, handler: extern "C" fn(i32)) -> usize; fn kill(pid: i32, number: i32) -> i32; fn setpgid(pid: i32, pgid: i32) -> i32; }

struct Worker {
    name: &'static str,
    program: &'static str,
    args: Vec<String>,
    log: &'static str,
    child: Option<Child>,
    next_start: Instant,
    failures: u32,
    enabled: bool,
}

impl Worker {
    fn new(name: &'static str, program: &'static str, args: Vec<String>, log: &'static str) -> Self {
        Self { name, program, args, log, child: None, next_start: Instant::now(), failures: 0, enabled: true }
    }

    fn start(&mut self, account: &str) -> std::io::Result<()> {
        let marker = match self.name {
            "paper" => "competition_bot.py",
            "research" => "scripts/continuous_research_scheduler.py",
            "gateway" => "multi-model-ai-gateway",
            "mlx" => "mlx_sentinel.server",
            _ => self.name,
        };
        let processes = Command::new("/bin/ps").args(["-axo", "pid=,command="]).output()?;
        let duplicate = String::from_utf8_lossy(&processes.stdout).lines().any(|line| {
            let fields = line.split_whitespace().collect::<Vec<_>>();
            let program = fields.get(1).and_then(|value| Path::new(value).file_name()).and_then(|value| value.to_str()).unwrap_or("").to_ascii_lowercase();
            let exact_marker = fields.iter().skip(2).any(|value| *value == marker) || program == marker;
            let expected_program = match self.name {
                "paper" | "research" | "mlx" => program == "python" || program == "python3",
                "gateway" => program == marker,
                _ => true,
            };
            exact_marker && expected_program
        });
        if duplicate {
            return Err(std::io::Error::other(format!("existing {} process found; refusing duplicate", self.name)));
        }
        let log = Path::new(ROOT).join(self.log);
        let stdout = OpenOptions::new().create(true).append(true).open(&log)?;
        let stderr = stdout.try_clone()?;
        let mut command = Command::new(self.program);
        command.args(&self.args).current_dir(ROOT).stdout(Stdio::from(stdout)).stderr(Stdio::from(stderr))
            .env("PAPER_ACCOUNT_ID", account)
            .env("PARENT_ENGINE_PID", std::process::id().to_string())
            .env("OMP_NUM_THREADS", "4").env("OPENBLAS_NUM_THREADS", "4")
            .env("MKL_NUM_THREADS", "4").env("VECLIB_MAXIMUM_THREADS", "4")
            .env("RESEARCH_WORKERS", "4");
        unsafe {
            command.pre_exec(|| {
                if setpgid(0, 0) == 0 { Ok(()) } else { Err(std::io::Error::last_os_error()) }
            });
        }
        let child = command.spawn()?;
        eprintln!("started {} pid={}", self.name, child.id());
        self.child = Some(child);
        Ok(())
    }

    fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            unsafe { kill(-(child.id() as i32), 15); }
            for _ in 0..30 {
                if child.try_wait().ok().flatten().is_some() { return; }
                thread::sleep(Duration::from_millis(100));
            }
            unsafe { kill(-(child.id() as i32), 9); }
            let _ = child.wait();
        }
    }

    fn tick(&mut self, account: &str) {
        if let Some(child) = self.child.as_mut() {
            match child.try_wait() {
                Ok(None) => return,
                Ok(Some(status)) => {
                    eprintln!("{} exited: {}", self.name, status);
                    self.child = None;
                }
                Err(error) => {
                    eprintln!("{} wait error: {}", self.name, error);
                    self.child = None;
                }
            }
            self.failures = self.failures.saturating_add(1);
            self.next_start = Instant::now() + Duration::from_secs((10u64 << self.failures.min(5)).min(300));
        }
        if self.enabled && self.child.is_none() && Instant::now() >= self.next_start {
            if let Err(error) = self.start(account) {
                eprintln!("{} start error: {}", self.name, error);
                self.failures = self.failures.saturating_add(1);
                self.next_start = Instant::now() + Duration::from_secs(30);
            }
        }
    }
}

fn account() -> Result<String, String> {
    let value = env::var("PAPER_ACCOUNT_ID").map_err(|_| "PAPER_ACCOUNT_ID is required")?;
    if value.parse::<u64>().ok().filter(|id| *id > 0).is_none() {
        return Err("invalid paper account ID".into());
    }
    Ok(value)
}

fn active_symbols() -> Result<Vec<String>, Box<dyn std::error::Error>> {
    let value: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(Path::new(ROOT).join("trading_system.json"))?)?;
    let symbols = value.get("active_symbols").and_then(|item| item.as_array()).ok_or("active_symbols is required")?
        .iter().filter_map(|item| item.as_str()).map(str::to_string).collect::<Vec<_>>();
    if symbols.is_empty() || symbols.iter().any(|symbol| !symbol.starts_with("US.")) { return Err("active_symbols must contain US symbols".into()); }
    Ok(symbols)
}

fn control(command: &str) -> std::io::Result<()> {
    let mut stream = UnixStream::connect(SOCKET)?;
    stream.write_all(command.as_bytes())?;
    let mut answer = String::new();
    stream.read_to_string(&mut answer)?;
    println!("{}", answer.trim());
    if answer.starts_with("unknown command") || answer.starts_with("paper bot is paused") || answer.starts_with("symbol change rejected") || answer.starts_with("symbol change failed") {
        return Err(std::io::Error::other(answer));
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<String> = env::args().skip(1).collect();
    let arg = arguments.first().cloned().unwrap_or_else(|| "serve".into());
    if arg != "serve" && arg != "check" { return Ok(control(&arguments.join(" "))?); }
    let account = account()?;
    let required = ["competition_bot.py", "scripts/continuous_research_scheduler.py", "ops/start_ai_gateway.sh", "scripts/start_mlx_sentinel.sh"];
    for file in required { if !Path::new(ROOT).join(file).is_file() { return Err(format!("missing {file}").into()); } }
    if arg == "check" { println!("engine configuration valid for configured paper account"); return Ok(()); }

    let symbols = active_symbols()?;
    let mut paper_args = vec!["competition_bot.py".into()];
    paper_args.extend(symbols.iter().cloned());
    paper_args.extend(["--acc-id".into(), account.clone(), "--bar-interval".into(), "1".into(), "--live-paper".into()]);
    let mut workers = [
        Worker::new("paper", "/usr/bin/python3", paper_args, "competition_bot.launchd.err"),
        Worker::new("research", "/usr/bin/python3", vec!["scripts/continuous_research_scheduler.py".into()], "logs/research-scheduler.log"),
        Worker::new("gateway", "/bin/zsh", vec!["ops/start_ai_gateway.sh".into()], "logs/multi-model-ai-gateway.err"),
        Worker::new("mlx", "/bin/zsh", vec!["scripts/start_mlx_sentinel.sh".into()], "logs/mlx-sentinel.err"),
    ];
    workers[0].enabled = !Path::new(BOT_PAUSED).exists();
    unsafe { signal(2, handle_signal); signal(15, handle_signal); }
    let socket = PathBuf::from(SOCKET);
    if socket.exists() {
        if UnixStream::connect(&socket).is_ok() { return Err("engine already running".into()); }
        std::fs::remove_file(&socket)?;
    }
    let listener = std::os::unix::net::UnixListener::bind(&socket)?;
    std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600))?;
    listener.set_nonblocking(true)?;
    eprintln!("paper engine started for {}; four research workers maximum", symbols.join(","));
    while !STOP.load(Ordering::SeqCst) {
        for worker in &mut workers { worker.tick(&account); }
        match listener.accept() {
            Ok((mut stream, _)) => {
                let mut buffer = [0u8; 64];
                let size = stream.read(&mut buffer).unwrap_or(0);
                let command = std::str::from_utf8(&buffer[..size]).unwrap_or("").trim();
                let answer = match command {
                    "status" => format!("{}", workers.iter().map(|w| format!("{}={} pid={}", w.name, if w.enabled { "enabled" } else { "stopped" }, w.child.as_ref().map_or(0, Child::id))).collect::<Vec<_>>().join("\n")),
                    "bot-stop" => { workers[0].enabled = false; let _ = std::fs::write(BOT_PAUSED, b"paused\n"); workers[0].stop(); "paper bot stopped".into() },
                    "bot-start" => { let _ = std::fs::remove_file(BOT_PAUSED); workers[0].enabled = true; workers[0].failures = 0; workers[0].next_start = Instant::now(); "paper bot start requested".into() },
                    "bot-restart" if workers[0].enabled => { workers[0].stop(); workers[0].failures = 0; workers[0].next_start = Instant::now(); "paper bot restart requested".into() },
                    "bot-restart" => "paper bot is paused".into(),
                    value if value.starts_with("portfolio-set ") || value.starts_with("symbol-set ") => {
                        let requested = value.split_once(' ').map(|(_, rest)| rest.trim()).unwrap_or("");
                        workers[0].stop();
                        workers[1].stop();
                        let result = Command::new("/usr/bin/python3").args(["scripts/swap_symbol.py", requested]).current_dir(ROOT)
                            .env("PAPER_ACCOUNT_ID", &account).output();
                        match result {
                            Ok(output) if output.status.success() => {
                                match active_symbols() {
                                    Ok(changed) => {
                                        let mut args = vec!["competition_bot.py".into()];
                                        args.extend(changed.iter().cloned());
                                        args.extend(["--acc-id".into(), account.clone(), "--bar-interval".into(), "1".into(), "--live-paper".into()]);
                                        workers[0].args = args;
                                        workers[0].failures = 0;
                                        workers[0].next_start = Instant::now();
                                        workers[1].failures = 0;
                                        workers[1].next_start = Instant::now();
                                        format!("active portfolio changed to {}", changed.join(","))
                                    }
                                    Err(error) => format!("symbol changed but configuration reload failed: {error}"),
                                }
                            }
                            Ok(output) => {
                                workers[0].next_start = Instant::now();
                                workers[1].next_start = Instant::now();
                                format!("symbol change rejected: {}", String::from_utf8_lossy(&output.stderr).trim())
                            }
                            Err(error) => {
                                workers[0].next_start = Instant::now();
                                workers[1].next_start = Instant::now();
                                format!("symbol change failed: {error}")
                            }
                        }
                    },
                    _ => "unknown command".into(),
                };
                let _ = stream.write_all(answer.as_bytes());
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
            Err(error) => eprintln!("control socket error: {error}"),
        }
        thread::sleep(Duration::from_millis(500));
    }
    for worker in &mut workers { worker.stop(); }
    let _ = std::fs::remove_file(&socket);
    Ok(())
}
