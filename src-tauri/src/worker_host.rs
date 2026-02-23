use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex, OnceLock, Weak};
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Emitter};

const WORKER_EVENT_PREFIX: &str = "worker://";
const WORKER_EVENT_JOB_STATE: &str = "worker://job_state";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct EnqueueJobRequest {
    #[serde(rename = "type")]
    pub job_type: String,
    pub account_id: String,
    pub conversation_id: Option<String>,
}

impl EnqueueJobRequest {
    fn validate(&self) -> Result<(), String> {
        if self.account_id.trim().is_empty() {
            return Err("accountId 不能为空".to_string());
        }
        match self.job_type.as_str() {
            "sync_list" | "sync_full" | "sync_incremental" => Ok(()),
            "sync_conversation" => {
                let conv_id = self
                    .conversation_id
                    .as_ref()
                    .map(|v| v.trim())
                    .unwrap_or("");
                if conv_id.is_empty() {
                    Err("sync_conversation 需要 conversationId".to_string())
                } else {
                    Ok(())
                }
            }
            _ => Err(format!("不支持的任务类型: {}", self.job_type)),
        }
    }
}

#[derive(Clone)]
struct WorkerProcess {
    generation: u64,
    child: Arc<Mutex<Child>>,
    stdin: Arc<Mutex<BufWriter<ChildStdin>>>,
    pending: Arc<Mutex<HashMap<String, mpsc::Sender<Result<Value, String>>>>>,
}

impl WorkerProcess {
    fn is_alive(&self) -> bool {
        let mut child_guard = match self.child.lock() {
            Ok(g) => g,
            Err(_) => return false,
        };
        match child_guard.try_wait() {
            Ok(Some(_)) => false,
            Ok(None) => true,
            Err(_) => false,
        }
    }
}

#[derive(Clone)]
struct JobTracking {
    job_type: String,
    account_id: String,
    conversation_id: Option<String>,
}

struct HostInner {
    process: Option<WorkerProcess>,
    tracked_jobs: HashMap<String, JobTracking>,
    restart_attempt: u32,
}

pub struct WorkerHost {
    app: AppHandle,
    python_bin: String,
    worker_script: PathBuf,
    output_dir: PathBuf,
    inner: Mutex<HostInner>,
    next_request_id: AtomicU64,
    next_generation: AtomicU64,
    shutting_down: AtomicBool,
}

impl WorkerHost {
    fn new(app: AppHandle, python_bin: String, worker_script: PathBuf, output_dir: PathBuf) -> Self {
        Self {
            app,
            python_bin,
            worker_script,
            output_dir,
            inner: Mutex::new(HostInner {
                process: None,
                tracked_jobs: HashMap::new(),
                restart_attempt: 0,
            }),
            next_request_id: AtomicU64::new(1),
            next_generation: AtomicU64::new(1),
            shutting_down: AtomicBool::new(false),
        }
    }

    fn spawn_worker(self: &Arc<Self>) -> Result<WorkerProcess, String> {
        let script_dir = self
            .worker_script
            .parent()
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        let script_str = self.worker_script.to_string_lossy().to_string();
        let output_dir_str = self.output_dir.to_string_lossy().to_string();

        let mut cmd = Command::new(&self.python_bin);
        cmd.current_dir(&script_dir)
            .arg(&script_str)
            .arg("--output-dir")
            .arg(&output_dir_str)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let mut child = cmd.spawn().map_err(|e| {
            format!(
                "启动 worker 失败: {} (python={}, script={})",
                e, self.python_bin, script_str
            )
        })?;

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "worker stdin 不可用".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "worker stdout 不可用".to_string())?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| "worker stderr 不可用".to_string())?;

        let generation = self.next_generation.fetch_add(1, Ordering::SeqCst);
        let process = WorkerProcess {
            generation,
            child: Arc::new(Mutex::new(child)),
            stdin: Arc::new(Mutex::new(BufWriter::new(stdin))),
            pending: Arc::new(Mutex::new(HashMap::new())),
        };

        self.spawn_stdout_reader(generation, process.pending.clone(), stdout);
        self.spawn_stderr_reader(generation, stderr);
        self.spawn_monitor(generation, process.child.clone());
        Ok(process)
    }

    fn spawn_stdout_reader(
        self: &Arc<Self>,
        generation: u64,
        pending: Arc<Mutex<HashMap<String, mpsc::Sender<Result<Value, String>>>>>,
        stdout: ChildStdout,
    ) {
        let weak = Arc::downgrade(self);
        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                let line_text = match line {
                    Ok(s) => s,
                    Err(err) => {
                        eprintln!("[worker_host] stdout read error: {}", err);
                        break;
                    }
                };
                if line_text.trim().is_empty() {
                    continue;
                }

                let parsed: Value = match serde_json::from_str(&line_text) {
                    Ok(v) => v,
                    Err(err) => {
                        eprintln!("[worker_host] stdout json parse error: {}", err);
                        eprintln!("[worker_host] line: {}", line_text);
                        continue;
                    }
                };

                if let Some(req_id) = parsed.get("id").and_then(|v| v.as_str()) {
                    let tx_opt = {
                        let mut guard = match pending.lock() {
                            Ok(g) => g,
                            Err(_) => continue,
                        };
                        guard.remove(req_id)
                    };
                    if let Some(tx) = tx_opt {
                        let ok = parsed.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
                        if ok {
                            let _ = tx.send(Ok(parsed.get("result").cloned().unwrap_or(Value::Null)));
                        } else {
                            let msg = parsed
                                .get("error")
                                .and_then(|e| e.get("message"))
                                .and_then(|v| v.as_str())
                                .unwrap_or("worker 返回错误")
                                .to_string();
                            let _ = tx.send(Err(msg));
                        }
                    }
                    continue;
                }

                if let Some(event_name) = parsed.get("event").and_then(|v| v.as_str()) {
                    let payload = parsed.get("payload").cloned().unwrap_or(Value::Null);
                    if let Some(host) = weak.upgrade() {
                        host.on_worker_event(event_name, payload);
                    }
                }
            }

            if let Some(host) = weak.upgrade() {
                host.mark_worker_dead(generation, "stdout closed", true);
            }
        });
    }

    fn spawn_stderr_reader(self: &Arc<Self>, generation: u64, stderr: ChildStderr) {
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines() {
                match line {
                    Ok(s) => {
                        if !s.trim().is_empty() {
                            eprintln!("[worker:{}] {}", generation, s);
                        }
                    }
                    Err(err) => {
                        eprintln!("[worker_host] stderr read error: {}", err);
                        break;
                    }
                }
            }
        });
    }

    fn spawn_monitor(self: &Arc<Self>, generation: u64, child: Arc<Mutex<Child>>) {
        let weak: Weak<Self> = Arc::downgrade(self);
        thread::spawn(move || loop {
            thread::sleep(Duration::from_millis(500));
            let status = {
                let mut guard = match child.lock() {
                    Ok(g) => g,
                    Err(_) => return,
                };
                match guard.try_wait() {
                    Ok(v) => v,
                    Err(err) => {
                        eprintln!("[worker_host] try_wait error: {}", err);
                        None
                    }
                }
            };

            if let Some(exit_status) = status {
                if let Some(host) = weak.upgrade() {
                    let reason = format!("process exited: {}", exit_status);
                    host.mark_worker_dead(generation, &reason, true);
                }
                return;
            }

            if weak.upgrade().is_none() {
                return;
            }
        });
    }

    fn on_worker_event(&self, event_name: &str, payload: Value) {
        if event_name == "job_state" {
            self.update_tracked_jobs(&payload);
        }
        let full_event = format!("{}{}", WORKER_EVENT_PREFIX, event_name);
        let _ = self.app.emit(&full_event, payload);
    }

    fn update_tracked_jobs(&self, payload: &Value) {
        let job_id = payload
            .get("jobId")
            .and_then(|v| v.as_str())
            .map(str::to_string);
        let state = payload.get("state").and_then(|v| v.as_str());
        let Some(job_id_value) = job_id else {
            return;
        };
        let Some(state_value) = state else {
            return;
        };

        let mut inner = match self.inner.lock() {
            Ok(g) => g,
            Err(_) => return,
        };

        match state_value {
            "queued" | "running" => {
                let job_type = payload
                    .get("type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown")
                    .to_string();
                let account_id = payload
                    .get("accountId")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let conversation_id = payload
                    .get("conversationId")
                    .and_then(|v| v.as_str())
                    .map(str::to_string);
                inner.tracked_jobs.insert(
                    job_id_value,
                    JobTracking {
                        job_type,
                        account_id,
                        conversation_id,
                    },
                );
            }
            "done" | "failed" => {
                inner.tracked_jobs.remove(&job_id_value);
            }
            _ => {}
        }
    }

    fn ensure_process(self: &Arc<Self>) -> Result<WorkerProcess, String> {
        {
            let mut inner = self.inner.lock().map_err(|_| "worker 状态锁失败".to_string())?;
            if let Some(proc_ref) = inner.process.clone() {
                if proc_ref.is_alive() {
                    return Ok(proc_ref);
                }
                inner.process = None;
            }
        }

        let process = self.spawn_worker()?;
        {
            let mut inner = self.inner.lock().map_err(|_| "worker 状态锁失败".to_string())?;
            inner.process = Some(process.clone());
            inner.restart_attempt = 0;
        }
        Ok(process)
    }

    fn backoff_duration(attempt: u32) -> Duration {
        match attempt {
            0 => Duration::from_millis(500),
            1 => Duration::from_secs(1),
            _ => Duration::from_secs(2),
        }
    }

    fn mark_worker_dead(self: &Arc<Self>, generation: u64, reason: &str, auto_restart: bool) {
        let (proc_opt, tracked, delay) = {
            let mut inner = match self.inner.lock() {
                Ok(g) => g,
                Err(_) => return,
            };

            let Some(proc_ref) = inner.process.clone() else {
                return;
            };
            if proc_ref.generation != generation {
                return;
            }

            inner.process = None;
            let jobs: Vec<(String, JobTracking)> = inner.tracked_jobs.drain().collect();

            let backoff = if auto_restart {
                let attempt = inner.restart_attempt;
                inner.restart_attempt = inner.restart_attempt.saturating_add(1);
                Some(Self::backoff_duration(attempt))
            } else {
                None
            };

            (Some(proc_ref), jobs, backoff)
        };

        if let Some(proc_ref) = proc_opt {
            let mut drained = Vec::new();
            if let Ok(mut pending_guard) = proc_ref.pending.lock() {
                drained.extend(pending_guard.drain().map(|(_, tx)| tx));
            }
            for tx in drained {
                let _ = tx.send(Err(format!("worker 不可用: {}", reason)));
            }
        }

        for (job_id, job) in tracked {
            let payload = json!({
                "jobId": job_id,
                "state": "failed",
                "type": job.job_type,
                "accountId": job.account_id,
                "conversationId": job.conversation_id,
                "error": {
                    "code": "TASK_INTERRUPTED",
                    "message": format!("Worker 中断: {}", reason),
                    "retryable": true
                }
            });
            let _ = self.app.emit(WORKER_EVENT_JOB_STATE, payload);
        }

        if auto_restart && !self.shutting_down.load(Ordering::SeqCst) {
            if let Some(wait) = delay {
                let weak = Arc::downgrade(self);
                thread::spawn(move || {
                    thread::sleep(wait);
                    if let Some(host) = weak.upgrade() {
                        if host.shutting_down.load(Ordering::SeqCst) {
                            return;
                        }
                        if let Err(err) = host.ensure_process() {
                            eprintln!("[worker_host] restart failed: {}", err);
                        }
                    }
                });
            }
        }
    }

    fn request(
        self: &Arc<Self>,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, String> {
        let mut attempts = 0;
        loop {
            attempts += 1;
            let process = self.ensure_process()?;
            let req_id = format!("req_{}", self.next_request_id.fetch_add(1, Ordering::SeqCst));
            let payload = json!({
                "id": req_id,
                "method": method,
                "params": params.clone(),
            });

            let (tx, rx) = mpsc::channel::<Result<Value, String>>();
            {
                let mut pending_guard = process
                    .pending
                    .lock()
                    .map_err(|_| "worker pending 锁失败".to_string())?;
                pending_guard.insert(req_id.clone(), tx);
            }

            let write_result = (|| -> Result<(), String> {
                let mut stdin_guard = process
                    .stdin
                    .lock()
                    .map_err(|_| "worker stdin 锁失败".to_string())?;
                let text = serde_json::to_string(&payload).map_err(|e| e.to_string())?;
                stdin_guard
                    .write_all(text.as_bytes())
                    .map_err(|e| e.to_string())?;
                stdin_guard.write_all(b"\n").map_err(|e| e.to_string())?;
                stdin_guard.flush().map_err(|e| e.to_string())
            })();

            if let Err(err) = write_result {
                if let Ok(mut pending_guard) = process.pending.lock() {
                    pending_guard.remove(&req_id);
                }
                self.mark_worker_dead(
                    process.generation,
                    &format!("stdin write failed: {}", err),
                    true,
                );
                if attempts < 2 {
                    continue;
                }
                return Err(format!("worker 请求失败: {}", err));
            }

            match rx.recv_timeout(timeout) {
                Ok(result) => return result,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if let Ok(mut pending_guard) = process.pending.lock() {
                        pending_guard.remove(&req_id);
                    }
                    return Err(format!("worker 请求超时: {}", method));
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    if attempts < 2 {
                        continue;
                    }
                    return Err("worker 响应通道断开".to_string());
                }
            }
        }
    }

    fn ping(self: &Arc<Self>) -> Result<(), String> {
        self.request("ping", Value::Object(Default::default()), Duration::from_secs(5))
            .map(|_| ())
    }

    fn shutdown(self: &Arc<Self>) {
        self.shutting_down.store(true, Ordering::SeqCst);
        let _ = self.request("shutdown", Value::Object(Default::default()), Duration::from_secs(2));

        let process_opt = {
            let mut inner = match self.inner.lock() {
                Ok(g) => g,
                Err(_) => return,
            };
            inner.process.take()
        };

        if let Some(proc_ref) = process_opt {
            if let Ok(mut child_guard) = proc_ref.child.lock() {
                let deadline = std::time::Instant::now() + Duration::from_secs(2);
                loop {
                    match child_guard.try_wait() {
                        Ok(Some(_)) => break,
                        Ok(None) => {
                            if std::time::Instant::now() >= deadline {
                                let _ = child_guard.kill();
                                break;
                            }
                            thread::sleep(Duration::from_millis(100));
                        }
                        Err(_) => {
                            let _ = child_guard.kill();
                            break;
                        }
                    }
                }
            }
        }
    }
}

static HOST: OnceLock<Arc<WorkerHost>> = OnceLock::new();

pub fn init_worker_host(
    app: AppHandle,
    python_bin: String,
    worker_script: PathBuf,
    output_dir: PathBuf,
) -> Result<(), String> {
    let host = Arc::new(WorkerHost::new(app, python_bin, worker_script, output_dir));
    host.ensure_process()?;
    host.ping()?;
    HOST.set(host).map_err(|_| "WorkerHost 已初始化".to_string())
}

fn get_host() -> Result<Arc<WorkerHost>, String> {
    HOST.get()
        .cloned()
        .ok_or_else(|| "WorkerHost 未初始化".to_string())
}

pub fn enqueue_job(req: EnqueueJobRequest) -> Result<String, String> {
    req.validate()?;
    let host = get_host()?;
    let params = serde_json::to_value(req).map_err(|e| e.to_string())?;
    let result = host.request("enqueue_job", params, Duration::from_secs(20))?;
    result
        .get("jobId")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| "worker 返回缺少 jobId".to_string())
}

pub fn shutdown_worker_host() {
    if let Some(host) = HOST.get() {
        host.shutdown();
    }
}
