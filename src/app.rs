use crate::{
    detector,
    launcher::Launcher,
    log_capture::{LogCapture, LogReceiver},
    models::{AppEntry, AppStatus, PortInfo},
    notifier::Notifier,
};
use tracing::{debug, error, info};
use obsidian::{
    app::window_attributes,
    aura::golden,
    theme::{self, Theme},
    widgets::{Badge, BadgeStatus},
    AppDelegate, EguiOnlyRenderer, EguiWindow,
};
use std::{
    collections::{HashMap, HashSet},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Instant,
};
use tokio::sync::watch;
use winit::{
    event::WindowEvent,
    event_loop::ActiveEventLoop,
    window::{Window, WindowId},
};

// ── Shared state ────────────────────────────────────────────────────────────

pub struct AppState {
    pub entries: Vec<AppEntry>,
    pub statuses: HashMap<PathBuf, (AppStatus, PortInfo)>,
    pub in_flight: HashSet<PathBuf>,
    pub apps_dir: PathBuf,
    pub refresh_secs: u64,
    pub last_scan: Instant,
    /// The currently selected app directory; `None` means no selection.
    pub selected_app: Option<PathBuf>,
    /// Whether to send desktop notifications on app status changes.
    pub notifications_enabled: bool,
    /// Maximum log lines to retain per app in the tail buffer.
    pub log_tail_lines: usize,
    /// Tracks previous statuses and fires desktop notifications on transitions.
    pub notifier: Notifier,
}

impl AppState {
    pub fn new(
        apps_dir: PathBuf,
        refresh_secs: u64,
        notifications_enabled: bool,
        log_tail_lines: usize,
    ) -> Self {
        AppState {
            entries: Vec::new(),
            statuses: HashMap::new(),
            in_flight: HashSet::new(),
            apps_dir,
            refresh_secs,
            last_scan: Instant::now(),
            selected_app: None,
            notifications_enabled,
            log_tail_lines,
            notifier: Notifier::new(notifications_enabled),
        }
    }
}

// ── App ─────────────────────────────────────────────────────────────────────

/// Top-level egui render loop implementing the obsidian `AppDelegate` seam.
pub struct App {
    // wgpu rendering (initialized on first `resumed`)
    wgpu_instance: wgpu::Instance,
    window: Option<Arc<Window>>,
    egui_window: Option<EguiWindow>,
    renderer: Option<EguiOnlyRenderer>,

    // shared app state
    state: Arc<Mutex<AppState>>,
    scanner_rx: watch::Receiver<Vec<AppEntry>>,
    force_scan_tx: watch::Sender<()>,
    launcher: Arc<tokio::sync::Mutex<Launcher>>,
    runtime_handle: tokio::runtime::Handle,

    // ── Log capture state (per-app, owned by the render thread) ─────────────
    /// Ring-buffer of retained log lines per app dir.
    log_captures: HashMap<PathBuf, LogCapture>,
    /// Async receivers delivering lines from spawned child processes.
    log_receivers: HashMap<PathBuf, LogReceiver>,
}

impl App {
    pub fn new(
        state: Arc<Mutex<AppState>>,
        scanner_rx: watch::Receiver<Vec<AppEntry>>,
        force_scan_tx: watch::Sender<()>,
        launcher: Arc<tokio::sync::Mutex<Launcher>>,
        runtime_handle: tokio::runtime::Handle,
    ) -> Self {
        App {
            wgpu_instance: wgpu::Instance::new(&wgpu::InstanceDescriptor {
                backends: wgpu::Backends::all(),
                ..Default::default()
            }),
            window: None,
            egui_window: None,
            renderer: None,
            state,
            scanner_rx,
            force_scan_tx,
            launcher,
            runtime_handle,
            log_captures: HashMap::new(),
            log_receivers: HashMap::new(),
        }
    }

    fn render(&mut self) {
        if self.egui_window.is_none() || self.renderer.is_none() {
            return;
        }

        // Poll scanner for new app entries.
        if self.scanner_rx.has_changed().unwrap_or(false) {
            let entries = self.scanner_rx.borrow_and_update().clone();
            debug!("scanner update: {} entries", entries.len());
            let new_paths: HashSet<PathBuf> = entries.iter().map(|e| e.dir.clone()).collect();
            let mut state = self.state.lock().unwrap();
            let removed_paths: Vec<PathBuf> = state
                .statuses
                .keys()
                .filter(|p| !new_paths.contains(*p))
                .cloned()
                .collect();
            for path in removed_paths {
                state.statuses.remove(&path);
                state.in_flight.remove(&path);
                state.entries.retain(|e| e.dir != path);
            }
            state.entries = entries.clone();
            state.last_scan = Instant::now();
            // Fire notifications for any transitions visible at this scan cycle.
            let pairs: Vec<(String, AppStatus)> = state
                .entries
                .iter()
                .filter_map(|e| {
                    state
                        .statuses
                        .get(&e.dir)
                        .map(|(s, _)| (e.name.clone(), s.clone()))
                })
                .collect();
            state.notifier.check_transitions(&pairs);
            drop(state);
            // Background-detect status for every entry; skip any with an in-flight op.
            for entry in entries {
                let in_flight = self.state.lock().unwrap().in_flight.contains(&entry.dir);
                if !in_flight {
                    self.dispatch_detect(entry);
                }
            }
        }

        // Drain pending log lines from all active receivers into the ring buffers.
        self.drain_log_receivers();

        // Extract owned values so the borrow of self.egui_window ends before the closure.
        let ctx = self.egui_window.as_mut().unwrap().ctx_clone();
        let raw_input = self.egui_window.as_mut().unwrap().take_input();

        let full_output = ctx.run(raw_input, |ctx| {
            self.draw_ui(ctx);
        });

        if let Some(ew) = self.egui_window.as_mut() {
            ew.handle_platform_output(full_output.platform_output);
        }

        let ppp = ctx.pixels_per_point();
        let paint_jobs = ctx.tessellate(full_output.shapes, ppp);

        if let Some(renderer) = self.renderer.as_mut() {
            let _ = renderer.render(paint_jobs, full_output.textures_delta, ppp);
        }
    }

    /// Non-blocking drain: pull all queued lines from every active log receiver
    /// into the corresponding ring-buffer. Called once per frame before `draw_ui`.
    fn drain_log_receivers(&mut self) {
        let capacity = self.state.lock().unwrap().log_tail_lines;
        for (dir, rx) in &mut self.log_receivers {
            let capture = self
                .log_captures
                .entry(dir.clone())
                .or_insert_with(|| LogCapture::new(capacity));
            loop {
                match rx.try_recv() {
                    Ok(line) => capture.push(line),
                    Err(_) => break, // Empty or sender dropped — nothing more to read.
                }
            }
        }
    }

    fn draw_ui(&mut self, ctx: &egui::Context) {
        // Snapshot all shared state before rendering so we can drop the lock.
        let (entries, statuses, in_flight, apps_dir, refresh_secs, last_scan, current_selected) = {
            let state = self.state.lock().unwrap();
            (
                state.entries.clone(),
                state.statuses.clone(),
                state.in_flight.clone(),
                state.apps_dir.clone(),
                state.refresh_secs,
                state.last_scan,
                state.selected_app.clone(),
            )
        };

        // Pending selection change — collected during row rendering, applied after.
        let mut pending_select: Option<Option<PathBuf>> = None;
        // Pending action from the details pane — applied after the panel closes.
        let mut pending_start: Option<AppEntry> = None;
        let mut pending_stop: Option<(AppEntry, Option<u32>)> = None;
        let mut pending_open: Option<u16> = None;

        // ── Details side panel (right) — shown when an app is selected ──────
        if current_selected.is_some() {
            egui::SidePanel::right("details")
                .exact_width(280.0)
                .show(ctx, |ui| {
                    self.draw_details(
                        ui,
                        &current_selected,
                        &entries,
                        &statuses,
                        &in_flight,
                        &mut pending_start,
                        &mut pending_stop,
                        &mut pending_open,
                    );
                });
        }

        // ── Central panel — app list ─────────────────────────────────────────
        egui::CentralPanel::default().show(ctx, |ui| {
            // ── Header ──────────────────────────────────────────────────
            ui.horizontal(|ui| {
                ui.heading("Warden");
                ui.add_space(golden::SPACE[2]); // SPACE_2 = 8px
                ui.label(apps_dir.to_string_lossy().as_ref());
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui
                        .add(
                            egui::Button::new("Scan now")
                                .min_size(egui::vec2(0.0, golden::CONTROL_HEIGHT_SM))
                                .corner_radius(egui::CornerRadius::same(golden::RADIUS_SM)),
                        )
                        .clicked()
                    {
                        let _ = self.force_scan_tx.send(());
                    }
                });
            });

            ui.separator();

            // ── App rows ─────────────────────────────────────────────────
            for entry in &entries {
                let (ref status, ref port_info) = statuses
                    .get(&entry.dir)
                    .cloned()
                    .unwrap_or((AppStatus::Unknown, PortInfo::default()));

                let (badge_label, badge_status) = match status {
                    AppStatus::Running { .. } => ("Running", BadgeStatus::Success),
                    AppStatus::Stopped => ("Stopped", BadgeStatus::Neutral),
                    AppStatus::Unknown => ("Unknown", BadgeStatus::Warning),
                };
                let port_str = port_info
                    .port
                    .map(|p| p.to_string())
                    .unwrap_or_else(|| "—".to_string());
                let is_running = matches!(status, AppStatus::Running { .. });
                let is_in_flight = in_flight.contains(&entry.dir);
                let is_selected = current_selected.as_ref() == Some(&entry.dir);

                // SPACE_3 (12px) vertical padding above each app row.
                ui.add_space(golden::SPACE[3]);
                let row_resp = ui.horizontal(|ui| {
                    Badge::new(badge_label, badge_status).ui(ui);
                    ui.label(&entry.name);
                    if let Some(v) = &entry.framework_version {
                        ui.label(v);
                    } else {
                        ui.label("—");
                    }
                    ui.label(&port_str);

                    let btn_size = egui::vec2(0.0, golden::CONTROL_HEIGHT_SM);
                    let radius = egui::CornerRadius::same(golden::RADIUS_SM);
                    if is_in_flight {
                        let lbl = if is_running { "Stopping…" } else { "Starting…" };
                        ui.add_enabled(
                            false,
                            egui::Button::new(lbl)
                                .min_size(btn_size)
                                .corner_radius(radius),
                        );
                    } else if is_running {
                        let pid = if let AppStatus::Running { pid } = status {
                            Some(*pid)
                        } else {
                            None
                        };
                        if ui
                            .add(
                                egui::Button::new("Stop")
                                    .min_size(btn_size)
                                    .corner_radius(radius),
                            )
                            .clicked()
                        {
                            self.dispatch_stop(entry.clone(), pid);
                        }
                        if let Some(port) = port_info.port {
                            if ui
                                .add(
                                    egui::Button::new("Open")
                                        .min_size(btn_size)
                                        .corner_radius(radius),
                                )
                                .clicked()
                            {
                                if let Err(e) = open::that(format!("http://localhost:{}", port)) {
                                    error!("open browser failed: {}", e);
                                }
                            }
                        }
                    } else if ui
                        .add(
                            egui::Button::new("Start")
                                .min_size(btn_size)
                                .corner_radius(radius),
                        )
                        .clicked()
                    {
                        self.dispatch_start(entry.clone());
                    }

                    // Selection indicator — small visual cue when row is active.
                    if is_selected {
                        ui.label("◀");
                    }
                });

                // Clicking the row background toggles selection (row_resp covers the
                // horizontal strip; button clicks are consumed first so they don't also
                // toggle the selection).
                if row_resp.response.clicked() {
                    pending_select = Some(if is_selected {
                        None
                    } else {
                        Some(entry.dir.clone())
                    });
                }

                // SPACE_3 (12px) vertical padding below each app row.
                ui.add_space(golden::SPACE[3]);
            }

            ui.separator();

            // ── Status bar ────────────────────────────────────────────────
            let state = self.state.lock().unwrap();
            ui.horizontal(|ui| {
                ui.label(format!("Auto-refresh: {}s", refresh_secs));
                ui.label(format!(
                    "Last scan: {}s ago",
                    last_scan.elapsed().as_secs()
                ));
                drop(state);
            });
        });

        // Apply pending selection change.
        if let Some(sel) = pending_select {
            self.state.lock().unwrap().selected_app = sel;
        }

        // Apply pending actions from the details panel.
        if let Some(entry) = pending_start {
            self.dispatch_start(entry);
        }
        if let Some((entry, pid)) = pending_stop {
            self.dispatch_stop(entry, pid);
        }
        if let Some(port) = pending_open {
            if let Err(e) = open::that(format!("http://localhost:{}", port)) {
                error!("open browser failed: {}", e);
            }
        }
    }

    /// Render the details side panel for the selected app.
    ///
    /// Actions (start/stop/open) are communicated back via the `pending_*` out-params
    /// so that dispatch calls happen after the panel borrow is released.
    #[allow(clippy::too_many_arguments)]
    fn draw_details(
        &self,
        ui: &mut egui::Ui,
        selected: &Option<PathBuf>,
        entries: &[AppEntry],
        statuses: &HashMap<PathBuf, (AppStatus, PortInfo)>,
        in_flight: &HashSet<PathBuf>,
        pending_start: &mut Option<AppEntry>,
        pending_stop: &mut Option<(AppEntry, Option<u32>)>,
        pending_open: &mut Option<u16>,
    ) {
        let Some(ref dir) = selected else { return };
        let Some(entry) = entries.iter().find(|e| &e.dir == dir) else { return };
        let (status, port_info) = statuses
            .get(dir)
            .cloned()
            .unwrap_or((AppStatus::Unknown, PortInfo::default()));

        // ── Header ───────────────────────────────────────────────────────
        ui.add_space(golden::SPACE[2]);
        ui.heading(&entry.name);

        let (badge_label, badge_status) = match &status {
            AppStatus::Running { .. } => ("Running", BadgeStatus::Success),
            AppStatus::Stopped => ("Stopped", BadgeStatus::Neutral),
            AppStatus::Unknown => ("Unknown", BadgeStatus::Warning),
        };
        ui.horizontal(|ui| {
            Badge::new(badge_label, badge_status).ui(ui);
            if let AppStatus::Running { pid } = &status {
                ui.label(format!("PID {}", pid));
            }
        });
        ui.separator();

        // ── Metadata table ───────────────────────────────────────────────
        egui::Grid::new("details_meta")
            .num_columns(2)
            .spacing([golden::SPACE[3], golden::SPACE[2]])
            .show(ui, |ui| {
                ui.label("Directory");
                ui.label(dir.to_string_lossy().as_ref());
                ui.end_row();

                ui.label("Grimoire version");
                ui.label(entry.framework_version.as_deref().unwrap_or("—"));
                ui.end_row();

                ui.label("Tech stack");
                ui.label(infer_tech_stack(entry.server_command.as_deref()));
                ui.end_row();
            });
        ui.separator();

        // ── Port section ─────────────────────────────────────────────────
        egui::Grid::new("details_ports")
            .num_columns(2)
            .spacing([golden::SPACE[3], golden::SPACE[2]])
            .show(ui, |ui| {
                ui.label("Known port");
                ui.label(
                    entry
                        .known_port
                        .map(|p| p.to_string())
                        .unwrap_or_else(|| "—".to_string()),
                );
                ui.end_row();

                ui.label("Detected port");
                ui.label(
                    port_info
                        .port
                        .map(|p| p.to_string())
                        .unwrap_or_else(|| "—".to_string()),
                );
                ui.end_row();
            });
        if let (Some(k), Some(d)) = (entry.known_port, port_info.port) {
            if k != d {
                ui.label("⚠ ports differ");
            }
        }
        ui.separator();

        // ── Command ──────────────────────────────────────────────────────
        ui.label("Command");
        ui.add(
            egui::Label::new(
                egui::RichText::new(entry.server_command.as_deref().unwrap_or("—")).monospace(),
            )
            .wrap(),
        );
        ui.separator();

        // ── Actions ──────────────────────────────────────────────────────
        let is_running = matches!(status, AppStatus::Running { .. });
        let is_in_flight = in_flight.contains(dir);

        ui.add_space(golden::SPACE[2]);
        let btn_size = egui::vec2(0.0, golden::CONTROL_HEIGHT_SM);
        let radius = egui::CornerRadius::same(golden::RADIUS_SM);

        if is_in_flight {
            let lbl = if is_running { "Stopping…" } else { "Starting…" };
            ui.add_enabled(
                false,
                egui::Button::new(lbl)
                    .min_size(btn_size)
                    .corner_radius(radius),
            );
        } else if is_running {
            let pid = if let AppStatus::Running { pid } = &status {
                Some(*pid)
            } else {
                None
            };
            ui.horizontal(|ui| {
                if ui
                    .add(
                        egui::Button::new("Stop")
                            .min_size(btn_size)
                            .corner_radius(radius),
                    )
                    .clicked()
                {
                    *pending_stop = Some((entry.clone(), pid));
                }
                if let Some(port) = port_info.port {
                    if ui
                        .add(
                            egui::Button::new("Open")
                                .min_size(btn_size)
                                .corner_radius(radius),
                        )
                        .clicked()
                    {
                        *pending_open = Some(port);
                    }
                }
            });
        } else if ui
            .add(
                egui::Button::new("Start")
                    .min_size(btn_size)
                    .corner_radius(radius),
            )
            .clicked()
        {
            *pending_start = Some(entry.clone());
        }

        // ── Log pane ─────────────────────────────────────────────────────
        let has_log_receiver = self.log_receivers.contains_key(dir);
        ui.separator();
        ui.label("Log output:");
        if is_running && has_log_receiver {
            let lines = self
                .log_captures
                .get(dir)
                .map(|c| c.lines())
                .unwrap_or_default();
            if lines.is_empty() {
                ui.label("No log output yet.");
            } else {
                egui::ScrollArea::vertical()
                    .id_salt("log_scroll")
                    .max_height(160.0)
                    .stick_to_bottom(true)
                    .show(ui, |ui| {
                        for line in &lines {
                            ui.monospace(line);
                        }
                    });
            }
        } else if is_running && !has_log_receiver {
            // Running but not launched by Warden this session.
            ui.label("Log streaming not available — app was not started by Warden this session.");
        } else {
            // Stopped or unknown.
            ui.label("Log streaming not available — app was not started by Warden this session.");
        }
    }

    fn dispatch_start(&mut self, entry: AppEntry) {
        let state = Arc::clone(&self.state);
        let launcher = Arc::clone(&self.launcher);
        let dir = entry.dir.clone();
        {
            let mut s = state.lock().unwrap();
            s.in_flight.insert(dir.clone());
        }

        // Pre-create a log channel whose receiver lives on the render thread.
        // The sender is cloned into the async task, which forwards lines from
        // the child-process reader into it once the process starts.
        use crate::log_capture::log_channel;
        let (log_tx, log_rx) = log_channel();
        let capacity = self.state.lock().unwrap().log_tail_lines;
        self.log_captures
            .entry(dir.clone())
            .or_insert_with(|| LogCapture::new(capacity));
        self.log_receivers.insert(dir.clone(), log_rx);

        self.runtime_handle.spawn(async move {
            let (status, port_info, process_log_rx) = {
                let mut l = launcher.lock().await;
                l.start(&entry).await
            };
            // Bridge lines from the child process's reader to the render-thread receiver.
            if let Some(mut lrx) = process_log_rx {
                tokio::spawn(async move {
                    while let Some(line) = lrx.recv().await {
                        if log_tx.send(line).is_err() {
                            break; // Receiver dropped (e.g. app entry removed).
                        }
                    }
                });
            }
            let mut s = state.lock().unwrap();
            s.in_flight.remove(&entry.dir);
            s.statuses.insert(entry.dir.clone(), (status.clone(), port_info));
            s.notifier.check_transitions(&[(entry.name.clone(), status)]);
        });
    }

    fn dispatch_stop(&self, entry: AppEntry, pid: Option<u32>) {
        let state = Arc::clone(&self.state);
        let launcher = Arc::clone(&self.launcher);
        {
            let mut s = state.lock().unwrap();
            s.in_flight.insert(entry.dir.clone());
        }
        self.runtime_handle.spawn(async move {
            let (status, port_info) = {
                let mut l = launcher.lock().await;
                l.stop(&entry, pid).await
            };
            let mut s = state.lock().unwrap();
            s.in_flight.remove(&entry.dir);
            s.statuses.insert(entry.dir.clone(), (status.clone(), port_info));
            s.notifier.check_transitions(&[(entry.name.clone(), status)]);
        });
    }

    /// Run detection for `entry` in a blocking thread; update statuses unless in-flight.
    fn dispatch_detect(&self, entry: AppEntry) {
        let state = Arc::clone(&self.state);
        let dir = entry.dir.clone();
        let name = entry.name.clone();
        self.runtime_handle.spawn(async move {
            let (status, port_info) =
                tokio::task::spawn_blocking(move || detector::detect(&entry))
                    .await
                    .unwrap_or((AppStatus::Unknown, PortInfo::default()));
            info!("detected {}: {:?}", dir.display(), status);
            let mut s = state.lock().unwrap();
            if !s.in_flight.contains(&dir) {
                s.statuses.insert(dir, (status.clone(), port_info));
                s.notifier.check_transitions(&[(name, status)]);
            }
        });
    }
}

// ── Tech stack heuristic ────────────────────────────────────────────────────

/// Infer the tech stack from the server command string.
/// Returns a static label suitable for display in the details panel.
fn infer_tech_stack(cmd: Option<&str>) -> &'static str {
    match cmd {
        Some(c) if c.contains("node") => "Node.js",
        Some(c) if c.contains("python") || c.contains("uvicorn") => "Python",
        Some(c) if c.contains("ruby") => "Ruby",
        _ => "Unknown",
    }
}

// ── AppDelegate ──────────────────────────────────────────────────────────────

impl AppDelegate for App {
    fn resumed(&mut self, event_loop: &ActiveEventLoop) {
        if self.window.is_some() {
            return; // Guard against multiple `resumed` calls (macOS behaviour).
        }

        let attrs = window_attributes("Warden", 920, 540);
        let window = Arc::new(event_loop.create_window(attrs).expect("window creation failed"));

        // Initialize wgpu synchronously via pollster.
        let surface = self.wgpu_instance.create_surface(window.clone()).unwrap();

        let adapter = pollster::block_on(self.wgpu_instance.request_adapter(
            &wgpu::RequestAdapterOptions {
                power_preference: wgpu::PowerPreference::default(),
                compatible_surface: Some(&surface),
                force_fallback_adapter: false,
            },
        ))
        .expect("no wgpu adapter found");

        let (device, queue) = pollster::block_on(adapter.request_device(
            &wgpu::DeviceDescriptor {
                label: Some("warden"),
                required_features: wgpu::Features::empty(),
                required_limits: wgpu::Limits::default(),
                memory_hints: wgpu::MemoryHints::default(),
            },
            None,
        ))
        .expect("wgpu device request failed");

        let surface_caps = surface.get_capabilities(&adapter);
        let surface_format = surface_caps
            .formats
            .iter()
            .copied()
            .find(|f| f.is_srgb())
            .unwrap_or(surface_caps.formats[0]);
        let alpha_mode = surface_caps.alpha_modes[0];

        let size = window.inner_size();
        let renderer = EguiOnlyRenderer::new(
            &self.wgpu_instance,
            device,
            queue,
            window.clone(),
            surface_format,
            alpha_mode,
            size.width,
            size.height,
        );

        let egui_ctx = egui::Context::default();
        theme::install_bundled_fonts(&egui_ctx);
        theme::set_active(Theme::aura_default(), &egui_ctx);
        let egui_window = EguiWindow::new(window.clone(), egui_ctx);

        self.window = Some(window);
        self.renderer = Some(renderer);
        self.egui_window = Some(egui_window);
    }

    fn window_event(
        &mut self,
        event_loop: &ActiveEventLoop,
        _window_id: WindowId,
        event: WindowEvent,
    ) {
        if let Some(ew) = self.egui_window.as_mut() {
            let response = ew.on_window_event(&event);
            if response.repaint {
                ew.window().request_redraw();
            }
        }

        match event {
            WindowEvent::CloseRequested => event_loop.exit(),
            WindowEvent::Resized(size) => {
                if let Some(renderer) = self.renderer.as_mut() {
                    renderer.resize(size.width, size.height);
                }
                if let Some(w) = self.window.as_ref() {
                    w.request_redraw();
                }
            }
            WindowEvent::RedrawRequested => {
                self.render();
            }
            _ => {}
        }
    }

    fn about_to_wait(&mut self, _event_loop: &ActiveEventLoop) {
        if let Some(w) = self.window.as_ref() {
            w.request_redraw();
        }
    }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_infer_tech_stack_node() {
        assert_eq!(infer_tech_stack(Some("node server.js")), "Node.js");
    }

    #[test]
    fn test_infer_tech_stack_node_npx() {
        assert_eq!(infer_tech_stack(Some("npx node index.js")), "Node.js");
    }

    #[test]
    fn test_infer_tech_stack_python() {
        assert_eq!(infer_tech_stack(Some("python main.py")), "Python");
    }

    #[test]
    fn test_infer_tech_stack_uvicorn() {
        assert_eq!(infer_tech_stack(Some("uvicorn app:main --reload")), "Python");
    }

    #[test]
    fn test_infer_tech_stack_ruby() {
        assert_eq!(infer_tech_stack(Some("ruby app.rb")), "Ruby");
    }

    #[test]
    fn test_infer_tech_stack_unknown_binary() {
        assert_eq!(infer_tech_stack(Some("./current/bin/myapp")), "Unknown");
    }

    #[test]
    fn test_infer_tech_stack_none() {
        assert_eq!(infer_tech_stack(None), "Unknown");
    }

    #[test]
    fn test_app_state_selected_app_defaults_none() {
        use std::path::PathBuf;
        let state = AppState::new(PathBuf::from("/tmp/apps"), 30, true, 500);
        assert!(state.selected_app.is_none());
    }

    #[test]
    fn test_app_state_selected_app_can_be_set() {
        use std::path::PathBuf;
        let mut state = AppState::new(PathBuf::from("/tmp/apps"), 30, true, 500);
        let path = PathBuf::from("/tmp/apps/myapp");
        state.selected_app = Some(path.clone());
        assert_eq!(state.selected_app, Some(path));
    }

    #[test]
    fn test_app_state_selected_app_can_be_cleared() {
        use std::path::PathBuf;
        let mut state = AppState::new(PathBuf::from("/tmp/apps"), 30, true, 500);
        state.selected_app = Some(PathBuf::from("/tmp/apps/myapp"));
        state.selected_app = None;
        assert!(state.selected_app.is_none());
    }

    #[test]
    fn test_app_state_carries_notifications_and_log_tail() {
        use std::path::PathBuf;
        let state = AppState::new(PathBuf::from("/tmp/apps"), 10, false, 200);
        assert!(!state.notifications_enabled);
        assert_eq!(state.log_tail_lines, 200);
    }
}
