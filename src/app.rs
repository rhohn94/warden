use crate::{
    detector,
    launcher::Launcher,
    models::{AppEntry, AppStatus, PortInfo},
};
use tracing::{debug, error, info};
use obsidian::{
    app::window_attributes,
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
}

impl AppState {
    pub fn new(apps_dir: PathBuf, refresh_secs: u64) -> Self {
        AppState {
            entries: Vec::new(),
            statuses: HashMap::new(),
            in_flight: HashSet::new(),
            apps_dir,
            refresh_secs,
            last_scan: Instant::now(),
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
    launcher: Arc<tokio::sync::Mutex<Launcher>>,
    runtime_handle: tokio::runtime::Handle,
}

impl App {
    pub fn new(
        state: Arc<Mutex<AppState>>,
        scanner_rx: watch::Receiver<Vec<AppEntry>>,
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
            launcher,
            runtime_handle,
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
            drop(state);
            // Background-detect status for every entry; skip any with an in-flight op.
            for entry in entries {
                let in_flight = self.state.lock().unwrap().in_flight.contains(&entry.dir);
                if !in_flight {
                    self.dispatch_detect(entry);
                }
            }
        }

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

    fn draw_ui(&mut self, ctx: &egui::Context) {
        let state = self.state.lock().unwrap();

        egui::CentralPanel::default().show(ctx, |ui| {
            // ── Header ──────────────────────────────────────────────────
            ui.horizontal(|ui| {
                ui.heading("Warden");
                ui.add_space(8.0);
                ui.label(state.apps_dir.to_string_lossy().as_ref());
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.button("Scan now").clicked() {
                        // scanner auto-refreshes; a manual trigger can be added in v0.2
                    }
                });
            });

            ui.separator();

            // ── App rows ─────────────────────────────────────────────────
            let entries: Vec<AppEntry> = state.entries.clone();
            let statuses = state.statuses.clone();
            let in_flight = state.in_flight.clone();
            drop(state); // release lock before UI interactions call back into state

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

                ui.horizontal(|ui| {
                    Badge::new(badge_label, badge_status).ui(ui);
                    ui.label(&entry.name);
                    if let Some(v) = &entry.framework_version {
                        ui.label(v);
                    } else {
                        ui.label("—");
                    }
                    ui.label(&port_str);

                    if is_in_flight {
                        let lbl = if is_running { "Stopping…" } else { "Starting…" };
                        ui.add_enabled(false, egui::Button::new(lbl));
                    } else if is_running {
                        let pid = if let AppStatus::Running { pid } = status {
                            Some(*pid)
                        } else {
                            None
                        };
                        if ui.button("Stop").clicked() {
                            self.dispatch_stop(entry.clone(), pid);
                        }
                        if let Some(port) = port_info.port {
                            if ui.button("Open").clicked() {
                                if let Err(e) = open::that(format!("http://localhost:{}", port)) {
                                    error!("open browser failed: {}", e);
                                }
                            }
                        }
                    } else if ui.button("Start").clicked() {
                        self.dispatch_start(entry.clone());
                    }
                });
            }

            ui.separator();

            // ── Status bar ────────────────────────────────────────────────
            let state = self.state.lock().unwrap();
            ui.horizontal(|ui| {
                ui.label(format!("Auto-refresh: {}s", state.refresh_secs));
                ui.label(format!(
                    "Last scan: {}s ago",
                    state.last_scan.elapsed().as_secs()
                ));
            });
        });
    }

    fn dispatch_start(&self, entry: AppEntry) {
        let state = Arc::clone(&self.state);
        let launcher = Arc::clone(&self.launcher);
        {
            let mut s = state.lock().unwrap();
            s.in_flight.insert(entry.dir.clone());
        }
        self.runtime_handle.spawn(async move {
            let (status, port_info) = {
                let mut l = launcher.lock().await;
                l.start(&entry).await
            };
            let mut s = state.lock().unwrap();
            s.in_flight.remove(&entry.dir);
            s.statuses.insert(entry.dir.clone(), (status, port_info));
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
            s.statuses.insert(entry.dir.clone(), (status, port_info));
        });
    }

    /// Run detection for `entry` in a blocking thread; update statuses unless in-flight.
    fn dispatch_detect(&self, entry: AppEntry) {
        let state = Arc::clone(&self.state);
        let dir = entry.dir.clone();
        self.runtime_handle.spawn(async move {
            let (status, port_info) =
                tokio::task::spawn_blocking(move || detector::detect(&entry))
                    .await
                    .unwrap_or((AppStatus::Unknown, PortInfo::default()));
            info!("detected {}: {:?}", dir.display(), status);
            let mut s = state.lock().unwrap();
            if !s.in_flight.contains(&dir) {
                s.statuses.insert(dir, (status, port_info));
            }
        });
    }
}

// ── AppDelegate ──────────────────────────────────────────────────────────────

impl AppDelegate for App {
    fn resumed(&mut self, event_loop: &ActiveEventLoop) {
        if self.window.is_some() {
            return; // Guard against multiple `resumed` calls (macOS behaviour).
        }

        let attrs = window_attributes("Warden", 640, 480);
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
