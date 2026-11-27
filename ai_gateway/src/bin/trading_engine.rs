//! Single launchd-owned supervisor for the US.ANT paper and AI runtime.
use std::{env, fs::OpenOptions, io::{Read, Write}, os::unix::{fs::PermissionsExt, net::UnixStream}, path::{Path, PathBuf}, process::{Child, Command, Stdio}, sync::atomic::{AtomicBool, Ordering}, thread, time::{Duration, Instant}};

const ROOT: &str = "/Users/local.local/Projects/TradingSystem";
const SOCKET: &str = "/tmp/us-ant-trading-engine.sock";
const BOT_PAUSED: &str = "/Users/local.local/Projects/TradingSystem/.trading_engine_bot_paused";
static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn handle_signal(_: i32) { STOP.store(true, Ordering::SeqCst); }
unsafe extern "C" { fn signal(number: i32, handler: extern "C" fn(i32)) -> usize; fn kill(pid: i32, number: i32) -> i32; }

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
        let log = Path::new(ROOT).join(self.log);
        let stdout = OpenOptions::new().create(true).append(true).open(&log)?;
        let stderr = stdout.try_clone()?;
        let mut command = Command::new(self.program);
        command.args(&self.args).current_dir(ROOT).stdout(Stdio::from(stdout)).stderr(Stdio::from(stderr))
            .env("US_ANT_PAPER_ACCOUNT_ID", account)
            .env("OMP_NUM_THREADS", "4").env("OPENBLAS_NUM_THREADS", "4")
            .env("MKL_NUM_THREADS", "4").env("VECLIB_MAXIMUM_THREADS", "4")
            .env("US_ANT_RESEARCH_WORKERS", "4");
        let child = command.spawn()?;
        eprintln!("started {} pid={}", self.name, child.id());
        self.child = Some(child);
        Ok(())
    }

    fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            unsafe { kill(child.id() as i32, 15); }
            for _ in 0..30 {
                if child.try_wait().ok().flatten().is_some() { return; }
                thread::sleep(Duration::from_millis(100));
            }
            let _ = child.kill();
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
    let value = env::var("US_ANT_PAPER_ACCOUNT_ID").map_err(|_| "US_ANT_PAPER_ACCOUNT_ID is required")?;
    if value.parse::<u64>().ok().filter(|id| *id > 0).is_none() {
        return Err("invalid paper account ID".into());
    }
    Ok(value)
}

fn control(command: &str) -> std::io::Result<()> {
    let mut stream = UnixStream::connect(SOCKET)?;
    stream.write_all(command.as_bytes())?;
    let mut answer = String::new();
    stream.read_to_string(&mut answer)?;
    println!("{}", answer.trim());
    if answer.starts_with("unknown command") || answer.starts_with("paper bot is paused") {
        return Err(std::io::Error::other(answer));
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arg = env::args().nth(1).unwrap_or_else(|| "serve".into());
    if arg != "serve" && arg != "check" { return Ok(control(&arg)?); }
    let account = account()?;
    let required = ["competition_bot.py", "scripts/continuous_research_scheduler.py", "ops/start_ai_gateway.sh", "scripts/start_mlx_sentinel.sh"];
    for file in required { if !Path::new(ROOT).join(file).is_file() { return Err(format!("missing {file}").into()); } }
    if arg == "check" { println!("engine configuration valid for configured paper account"); return Ok(()); }

    let mut workers = [
        Worker::new("paper", "/usr/bin/python3", vec!["competition_bot.py".into(), "ANT".into(), "--acc-id".into(), account.clone(), "--bar-interval".into(), "1".into(), "--live-paper".into()], "competition_bot.launchd.err"),
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
    eprintln!("US.ANT engine started; four research workers maximum");
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
