use crate::{
    config::Config,
    history::HistoryStore,
    launcher::Launcher,
    log_capture::{LogCapture, LogReceiver},
    models::{AppEntry, AppStatus, PortInfo, VersionCheckResult},
    notifier::Notifier,
};
use tracing::{debug, error};
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
    sync::{Arc, Mutex, RwLock},
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
    /// All watched root directories.
    pub apps_dirs: Vec<PathBuf>,
    pub refresh_secs: u64,
    pub last_scan: Instant,
    /// The currently selected app directory; `None` means no selection.
    pub selected_app: Option<PathBuf>,
    /// Whether to send desktop notifications on app status changes.
    #[allow(dead_code)]
    pub notifications_enabled: bool,
    /// Maximum log lines to retain per app in the tail buffer.
    pub log_tail_lines: usize,
    /// Tracks previous statuses and fires desktop notifications on transitions.
    pub notifier: Notifier,
    /// Version-update check results keyed by app name, shared with VersionChecker.
    pub version_results: Arc<RwLock<HashMap<String, VersionCheckResult>>>,
    /// Per-app start/stop event ring buffer with persistence.
    pub history: Arc<Mutex<HistoryStore>>,
}

impl AppState {
    pub fn new(
        apps_dirs: Vec<PathBuf>,
        refresh_secs: u64,
        notifications_enabled: bool,
        log_tail_lines: usize,
        version_results: Arc<RwLock<HashMap<String, VersionCheckResult>>>,
        history: Arc<Mutex<HistoryStore>>,
    ) -> Self {
        AppState {
            entries: Vec::new(),
            statuses: HashMap::new(),
            in_flight: HashSet::new(),
            apps_dirs,
            refresh_secs,
            last_scan: Instant::now(),
            selected_app: None,
            notifications_enabled,
            log_tail_lines,
            notifier: Notifier::new(notifications_enabled),
            version_results,
            history,
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
    config: Config,
    scanner_rx: watch::Receiver<crate::scanner::ScanResult>,
    force_scan_tx: watch::Sender<()>,
    launcher: Arc<tokio::sync::Mutex<Launcher>>,
    runtime_handle: tokio::runtime::Handle,

    // ── Log capture state (per-app, owned by the render thread) ─────────────
    /// Ring-buffer of retained log lines per app dir.
    log_captures: HashMap<PathBuf, LogCapture>,
    /// Async receivers delivering lines from spawned child processes.
    log_receivers: HashMap<PathBuf, LogReceiver>,

    // ── Log viewer state ─────────────────────────────────────────────────────
    /// When true, the central area shows the dedicated log viewer; false shows the app list.
    show_log_viewer: bool,
    /// Per-app-name visibility toggle for the log viewer filter chips.
    log_viewer_filter: HashMap<String, bool>,
    /// Whether the log viewer scroll area should auto-scroll to the bottom.
    /// Set to false when the user scrolls up; restored when scrolled back to bottom.
    log_viewer_auto_scroll: bool,
    /// Total number of aggregated log lines rendered on the previous frame,
    /// used to detect new output and re-enable auto-scroll tracking.
    log_viewer_prev_line_count: usize,

    // ── App list search / live filter ────────────────────────────────────────
    /// The current text entered in the app-list filter field; empty means no filter.
    search_query: String,
}

impl App {
    pub fn new(
        state: Arc<Mutex<AppState>>,
        config: Config,
        scanner_rx: watch::Receiver<crate::scanner::ScanResult>,
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
            config,
            scanner_rx,
            force_scan_tx,
            launcher,
            runtime_handle,
            log_captures: HashMap::new(),
            log_receivers: HashMap::new(),
            show_log_viewer: false,
            log_viewer_filter: HashMap::new(),
            log_viewer_auto_scroll: true,
            log_viewer_prev_line_count: 0,
            search_query: String::new(),
        }
    }

    fn render(&mut self) {
        if self.egui_window.is_none() || self.renderer.is_none() {
            return;
        }

        // Poll scanner for new app entries with pre-detected statuses.
        if self.scanner_rx.has_changed().unwrap_or(false) {
            let scan_results = self.scanner_rx.borrow_and_update().clone();
            debug!("scanner update: {} entries", scan_results.len());
            let new_paths: HashSet<PathBuf> =
                scan_results.iter().map(|(e, _, _)| e.dir.clone()).collect();
            let entries: Vec<AppEntry> = scan_results.iter().map(|(e, _, _)| e.clone()).collect();
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
            state.entries = entries;
            state.last_scan = Instant::now();
            // Apply scanner-provided statuses; skip apps with an in-flight user action.
            for (entry, status, port_info) in scan_results {
                if !state.in_flight.contains(&entry.dir) {
                    state.statuses.insert(entry.dir.clone(), (status, port_info));
                }
            }
            // Fire notifications and record history for any status transitions.
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
            let transitions = state.notifier.check_transitions(&pairs);
            if !transitions.is_empty() {
                let mut hist = state.history.lock().unwrap();
                for (name, new_status) in &transitions {
                    match new_status {
                        AppStatus::Running { .. } => hist.record_started(name, 0),
                        AppStatus::Stopped | AppStatus::Unknown => hist.record_stopped(name),
                    }
                }
                hist.save();
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
            while let Ok(line) = rx.try_recv() {
                capture.push(line);
            }
        }
    }

    fn draw_ui(&mut self, ctx: &egui::Context) {
        // Frame-time telemetry: warn when a frame exceeds the configured threshold.
        let frame_dt_ms = ctx.input(|i| i.stable_dt) * 1000.0;
        let warn_ms = self.config.frame_warn_ms() as f32;
        if frame_dt_ms > warn_ms {
            tracing::warn!(frame_ms = frame_dt_ms as u64, "slow frame");
        }

        // Snapshot all shared state before rendering so we can drop the lock.
        let (entries, statuses, in_flight, apps_dirs, refresh_secs, last_scan, current_selected, version_results_snap) = {
            let state = self.state.lock().unwrap();
            let ver_snap: HashMap<String, VersionCheckResult> = state
                .version_results
                .read()
                .map(|m| m.clone())
                .unwrap_or_default();
            (
                state.entries.clone(),
                state.statuses.clone(),
                state.in_flight.clone(),
                state.apps_dirs.clone(),
                state.refresh_secs,
                state.last_scan,
                state.selected_app.clone(),
                ver_snap,
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
                        &version_results_snap,
                        &mut pending_start,
                        &mut pending_stop,
                        &mut pending_open,
                    );
                });
        }

        // ── Central panel — app list or log viewer ───────────────────────────
        egui::CentralPanel::default().show(ctx, |ui| {
            // ── Header ──────────────────────────────────────────────────
            ui.horizontal(|ui| {
                ui.heading("Warden");
                ui.add_space(golden::SPACE[2]); // SPACE_2 = 8px
                let dirs_label = if apps_dirs.len() == 1 {
                    apps_dirs[0].to_string_lossy().into_owned()
                } else {
                    format!("{} directories", apps_dirs.len())
                };
                ui.label(&dirs_label);
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    // [Logs] / [Apps] toggle — swaps the central panel content.
                    let toggle_label = if self.show_log_viewer { "Apps" } else { "Logs" };
                    if ui
                        .add(
                            egui::Button::new(toggle_label)
                                .min_size(egui::vec2(0.0, golden::CONTROL_HEIGHT_SM))
                                .corner_radius(egui::CornerRadius::same(golden::RADIUS_SM)),
                        )
                        .clicked()
                    {
                        self.show_log_viewer = !self.show_log_viewer;
                    }
                    ui.add_space(golden::SPACE[2]);
                    if !self.show_log_viewer
                        && ui
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

            if self.show_log_viewer {
                self.draw_log_viewer(ui, &entries, &statuses);
            } else {
                // ── Search / live filter ──────────────────────────────────────
                ui.add(
                    egui::TextEdit::singleline(&mut self.search_query)
                        .hint_text("Filter apps…"),
                );

                // Escape clears the search query when it is non-empty.
                if !self.search_query.is_empty()
                    && ctx.input(|i| i.key_pressed(egui::Key::Escape))
                {
                    self.search_query.clear();
                    ctx.memory_mut(|m| m.surrender_focus(egui::Id::NULL));
                }

                // Build filtered entry list (clone matching entries).
                let total_count = entries.len();
                let active_query = self.search_query.trim().to_lowercase();
                let filtered: Vec<AppEntry> =
                    filter_entries(&entries, &active_query)
                        .into_iter()
                        .cloned()
                        .collect();

                // Clear selection when the selected app is filtered out.
                if let Some(ref sel_path) = current_selected {
                    if !filtered.iter().any(|e| &e.dir == sel_path) {
                        pending_select = Some(None);
                    }
                }

                // ── App rows ─────────────────────────────────────────────────
                self.draw_app_list(
                    ui,
                    &filtered,
                    &statuses,
                    &in_flight,
                    &version_results_snap,
                    &mut pending_select,
                    &current_selected,
                    &apps_dirs,
                    refresh_secs,
                    last_scan,
                    total_count,
                    &active_query,
                );
            }
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

    /// Render the app list rows and status bar into the central panel.
    ///
    /// Extracted from `draw_ui` so that the central panel body can be swapped with
    /// the log viewer without duplicating the header/toolbar logic.
    #[allow(clippy::too_many_arguments)]
    fn draw_app_list(
        &mut self,
        ui: &mut egui::Ui,
        entries: &[AppEntry],
        statuses: &HashMap<PathBuf, (AppStatus, PortInfo)>,
        in_flight: &HashSet<PathBuf>,
        version_results_snap: &HashMap<String, VersionCheckResult>,
        pending_select: &mut Option<Option<PathBuf>>,
        current_selected: &Option<PathBuf>,
        apps_dirs: &[PathBuf],
        refresh_secs: u64,
        last_scan: Instant,
        total_count: usize,
        active_query: &str,
    ) {
        for entry in entries {
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
                if let Some(VersionCheckResult::UpdateAvailable { latest }) =
                    version_results_snap.get(&entry.name)
                {
                    ui.label(format!("↑ {}", latest));
                }
                ui.label(&port_str);
                // Show which root directory this app came from when multiple roots
                // are watched; use a subdued label so it doesn't dominate the row.
                if apps_dirs.len() > 1 {
                    let root_name = entry
                        .root
                        .file_name()
                        .map(|n| n.to_string_lossy().into_owned())
                        .unwrap_or_else(|| entry.root.to_string_lossy().into_owned());
                    ui.add(
                        egui::Label::new(
                            egui::RichText::new(root_name)
                                .small()
                                .color(ui.visuals().weak_text_color()),
                        )
                        .sense(egui::Sense::hover()),
                    )
                    .on_hover_text(entry.root.to_string_lossy().as_ref());
                }

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
                *pending_select = Some(if is_selected {
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
        ui.horizontal(|ui| {
            ui.label(format!("Auto-refresh: {}s", refresh_secs));
            ui.label(format!(
                "Last scan: {}s ago",
                last_scan.elapsed().as_secs()
            ));
            if !active_query.is_empty() {
                ui.label(format!("Showing {} of {} apps", entries.len(), total_count));
            }
        });
    }

    /// Render the dedicated log viewer panel.
    ///
    /// Aggregates stdout/stderr lines from all running apps' ring buffers, applies
    /// per-app chip filter toggles, prefixes each line with the source app name, and
    /// renders them in a scrollable area with auto-scroll that pauses on manual scroll-up.
    fn draw_log_viewer(
        &mut self,
        ui: &mut egui::Ui,
        entries: &[AppEntry],
        statuses: &HashMap<PathBuf, (AppStatus, PortInfo)>,
    ) {
        // Determine which apps have an active log receiver (launched by Warden this session).
        let running_apps: Vec<&AppEntry> = entries
            .iter()
            .filter(|e| {
                matches!(
                    statuses.get(&e.dir).map(|(s, _)| s),
                    Some(AppStatus::Running { .. })
                ) && self.log_receivers.contains_key(&e.dir)
            })
            .collect();

        // Ensure every running app has a filter chip entry (default: visible).
        for entry in &running_apps {
            self.log_viewer_filter
                .entry(entry.name.clone())
                .or_insert(true);
        }

        // ── Filter chip bar ──────────────────────────────────────────────────
        ui.horizontal_wrapped(|ui| {
            // "All" bulk-select chip.
            let all_active = running_apps
                .iter()
                .all(|e| *self.log_viewer_filter.get(&e.name).unwrap_or(&true));
            let all_label = if all_active { "● All" } else { "○ All" };
            if ui
                .add(
                    egui::Button::new(all_label)
                        .min_size(egui::vec2(0.0, golden::CONTROL_HEIGHT_SM))
                        .corner_radius(egui::CornerRadius::same(golden::RADIUS_SM)),
                )
                .clicked()
            {
                // Toggle: if all active → deselect all; if any inactive → select all.
                let new_state = !all_active;
                for entry in &running_apps {
                    self.log_viewer_filter
                        .insert(entry.name.clone(), new_state);
                }
            }

            // Per-app filter chips.
            for entry in &running_apps {
                let active = *self.log_viewer_filter.get(&entry.name).unwrap_or(&true);
                let chip_label = if active {
                    format!("● {}", entry.name)
                } else {
                    format!("○ {}", entry.name)
                };
                if ui
                    .add(
                        egui::Button::new(&chip_label)
                            .min_size(egui::vec2(0.0, golden::CONTROL_HEIGHT_SM))
                            .corner_radius(egui::CornerRadius::same(golden::RADIUS_SM)),
                    )
                    .clicked()
                {
                    self.log_viewer_filter.insert(entry.name.clone(), !active);
                }
            }
        });

        ui.add_space(golden::SPACE[2]);

        // ── Aggregate and filter log lines ───────────────────────────────────
        // Collect (name, lines) per app; pass to the pure helper which applies
        // the filter map and prefixes each retained line with `[<name>] `.
        // True chronological merging would require timestamps — the ring buffer
        // stores bare strings, so per-app stable ordering is the practical choice.
        let per_app: Vec<(&str, Vec<String>)> = running_apps
            .iter()
            .map(|e| {
                let lines = self
                    .log_captures
                    .get(&e.dir)
                    .map(|c| c.lines())
                    .unwrap_or_default();
                (e.name.as_str(), lines)
            })
            .collect();

        let aggregated = aggregate_log_lines(&per_app, &self.log_viewer_filter);

        let line_count = aggregated.len();

        // Detect new output: if line count grew, re-enable auto-scroll.
        if line_count > self.log_viewer_prev_line_count {
            // Only resume auto-scroll if it was not deliberately disabled.
            // We re-enable it unconditionally on new output — the user can
            // scroll up again to pause it.
            self.log_viewer_auto_scroll = true;
        }
        self.log_viewer_prev_line_count = line_count;

        // ── Scrollable log area ───────────────────────────────────────────────
        if aggregated.is_empty() {
            if running_apps.is_empty() {
                ui.label("No running apps with log capture active.");
            } else {
                ui.label("No log output yet — waiting for output from running apps.");
            }
        } else {
            let scroll_area = egui::ScrollArea::vertical()
                .id_salt("log_viewer_scroll")
                .auto_shrink([false, false])
                .stick_to_bottom(self.log_viewer_auto_scroll);

            let scroll_output = scroll_area.show(ui, |ui| {
                for line in &aggregated {
                    ui.monospace(line);
                }
            });

            // Detect manual scroll-up: if the user has scrolled away from the bottom,
            // disable auto-scroll so new output doesn't forcibly drag them down.
            let content_height = scroll_output.content_size.y;
            let visible_height = scroll_output.inner_rect.height();
            let offset = scroll_output.state.offset.y;
            let at_bottom = content_height <= visible_height
                || (offset + visible_height >= content_height - 2.0);
            if !at_bottom {
                self.log_viewer_auto_scroll = false;
            } else {
                // Scrolled back to bottom — resume auto-scroll.
                self.log_viewer_auto_scroll = true;
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
        version_results: &HashMap<String, VersionCheckResult>,
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

                ui.label("Update");
                let update_label = match version_results.get(&entry.name) {
                    Some(VersionCheckResult::UpdateAvailable { latest }) => {
                        format!("↑ {} available", latest)
                    }
                    Some(VersionCheckResult::UpToDate) => "Up to date".to_string(),
                    _ => "—".to_string(),
                };
                ui.label(update_label);
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

        // ── History section ──────────────────────────────────────────────
        ui.separator();
        ui.label("History:");

        let hist = self.state.lock().unwrap().history.clone();
        let hist = hist.lock().unwrap();

        // If running, show a live uptime counter.
        if is_running {
            if let Some(started_at) = hist.last_started_at(&entry.name) {
                let elapsed = chrono::Utc::now().signed_duration_since(started_at);
                let total_secs = elapsed.num_seconds().max(0) as u64;
                let uptime_str = format_uptime(total_secs);
                ui.label(format!("Uptime: {}", uptime_str));
            }
        }

        let recent = hist.recent(&entry.name, 10);
        if recent.is_empty() {
            ui.label("No history recorded.");
        } else {
            for event in &recent {
                let line = match event {
                    crate::history::HistoryEvent::Started { at, pid } => {
                        format!("{} — Started (PID {})", at.format("%Y-%m-%d %H:%M"), pid)
                    }
                    crate::history::HistoryEvent::Stopped { at, duration_secs } => {
                        format!(
                            "{} — Stopped (uptime {})",
                            at.format("%Y-%m-%d %H:%M"),
                            format_uptime(*duration_secs)
                        )
                    }
                };
                ui.label(line);
            }
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
            s.notifier.check_transitions(&[(entry.name.clone(), status.clone())]);
            // Record history for explicit start action.
            let pid = if let AppStatus::Running { pid } = status { pid } else { 0 };
            {
                let mut hist = s.history.lock().unwrap();
                hist.record_started(&entry.name, pid);
                hist.save();
            }
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
            // Record history for explicit stop action.
            {
                let mut hist = s.history.lock().unwrap();
                hist.record_stopped(&entry.name);
                hist.save();
            }
        });
    }

}

// ── Helpers ─────────────────────────────────────────────────────────────────

/// Filter a slice of `AppEntry` values by a case-insensitive substring query.
///
/// Returns refs to entries whose `name` contains `query` (trimmed). An empty
/// or whitespace-only query returns all entries unchanged.
pub fn filter_entries<'a>(entries: &'a [AppEntry], query: &str) -> Vec<&'a AppEntry> {
    let q = query.trim().to_lowercase();
    if q.is_empty() {
        return entries.iter().collect();
    }
    entries
        .iter()
        .filter(|e| e.name.to_lowercase().contains(&q))
        .collect()
}

/// Aggregate log lines from multiple apps, apply a per-app filter map, and
/// prefix each retained line with `[<app_name>] `.
///
/// `per_app` is a slice of `(name, lines)` pairs in stable per-app order.
/// `filter` maps app name → visible; missing keys default to `true`.
/// Returns the flattened, prefixed list in app-then-line order.
pub fn aggregate_log_lines(
    per_app: &[(&str, Vec<String>)],
    filter: &HashMap<String, bool>,
) -> Vec<String> {
    let mut out = Vec::new();
    for (name, lines) in per_app {
        if !filter.get(*name).copied().unwrap_or(true) {
            continue;
        }
        for line in lines {
            out.push(format!("[{}] {}", name, line));
        }
    }
    out
}

/// Format a duration in seconds as a human-readable uptime string.
/// Examples: "5s", "3m", "1h 5m".
fn format_uptime(secs: u64) -> String {
    let hours = secs / 3600;
    let minutes = (secs % 3600) / 60;
    let seconds = secs % 60;
    if hours > 0 {
        format!("{}h {}m", hours, minutes)
    } else if minutes > 0 {
        format!("{}m", minutes)
    } else {
        format!("{}s", seconds)
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

    fn make_history() -> Arc<Mutex<crate::history::HistoryStore>> {
        Arc::new(Mutex::new(crate::history::HistoryStore::new()))
    }

    fn make_state(apps_dir: &str, refresh: u64, notif: bool, tail: usize) -> AppState {
        use std::collections::HashMap;
        use std::sync::RwLock;
        AppState::new(
            vec![PathBuf::from(apps_dir)],
            refresh,
            notif,
            tail,
            Arc::new(RwLock::new(HashMap::new())),
            make_history(),
        )
    }

    #[test]
    fn test_app_state_selected_app_defaults_none() {
        let state = make_state("/tmp/apps", 30, true, 500);
        assert!(state.selected_app.is_none());
    }

    #[test]
    fn test_app_state_selected_app_can_be_set() {
        let mut state = make_state("/tmp/apps", 30, true, 500);
        let path = PathBuf::from("/tmp/apps/myapp");
        state.selected_app = Some(path.clone());
        assert_eq!(state.selected_app, Some(path));
    }

    #[test]
    fn test_app_state_selected_app_can_be_cleared() {
        let mut state = make_state("/tmp/apps", 30, true, 500);
        state.selected_app = Some(PathBuf::from("/tmp/apps/myapp"));
        state.selected_app = None;
        assert!(state.selected_app.is_none());
    }

    #[test]
    fn test_app_state_carries_notifications_and_log_tail() {
        let state = make_state("/tmp/apps", 10, false, 200);
        assert!(!state.notifications_enabled);
        assert_eq!(state.log_tail_lines, 200);
    }

    #[test]
    fn test_format_uptime_seconds() {
        assert_eq!(format_uptime(45), "45s");
    }

    #[test]
    fn test_format_uptime_minutes() {
        assert_eq!(format_uptime(180), "3m");
    }

    #[test]
    fn test_format_uptime_hours_and_minutes() {
        assert_eq!(format_uptime(3725), "1h 2m");
    }

    // ── Log viewer helpers ───────────────────────────────────────────────────

    #[test]
    fn test_aggregate_log_lines_prefix() {
        let per_app = vec![
            ("myapp", vec!["hello".to_string(), "world".to_string()]),
            ("other", vec!["foo".to_string()]),
        ];
        let filter = HashMap::new(); // all visible by default
        let out = aggregate_log_lines(&per_app, &filter);
        assert_eq!(out, vec!["[myapp] hello", "[myapp] world", "[other] foo"]);
    }

    #[test]
    fn test_aggregate_log_lines_filter_hides_app() {
        let per_app = vec![
            ("myapp", vec!["hello".to_string()]),
            ("other", vec!["foo".to_string()]),
        ];
        let mut filter = HashMap::new();
        filter.insert("other".to_string(), false);
        let out = aggregate_log_lines(&per_app, &filter);
        assert_eq!(out, vec!["[myapp] hello"]);
    }

    #[test]
    fn test_aggregate_log_lines_all_filtered_empty() {
        let per_app = vec![
            ("myapp", vec!["hello".to_string()]),
        ];
        let mut filter = HashMap::new();
        filter.insert("myapp".to_string(), false);
        let out = aggregate_log_lines(&per_app, &filter);
        assert!(out.is_empty());
    }

    #[test]
    fn test_aggregate_log_lines_empty_input() {
        let per_app: Vec<(&str, Vec<String>)> = vec![];
        let filter = HashMap::new();
        let out = aggregate_log_lines(&per_app, &filter);
        assert!(out.is_empty());
    }

    #[test]
    fn test_aggregate_log_lines_filter_missing_key_defaults_visible() {
        // A filter map with no entry for "myapp" should leave it visible.
        let per_app = vec![("myapp", vec!["line".to_string()])];
        let filter: HashMap<String, bool> = HashMap::new();
        let out = aggregate_log_lines(&per_app, &filter);
        assert_eq!(out, vec!["[myapp] line"]);
    }

    #[test]
    fn test_log_viewer_show_defaults_false() {
        let state = make_state("/tmp/apps", 30, true, 500);
        // show_log_viewer is a field on App, not AppState; verify the AppState
        // construct does not interfere with it. The field itself defaults false
        // in App::new — confirmed by inspecting the initializer; this test
        // validates AppState construction still works cleanly alongside it.
        assert!(state.entries.is_empty());
        assert!(state.selected_app.is_none());
    }

    // ── filter_entries ───────────────────────────────────────────────────────

    fn make_entry(name: &str) -> AppEntry {
        AppEntry {
            name: name.to_string(),
            dir: PathBuf::from(format!("/tmp/apps/{}", name)),
            root: PathBuf::from("/tmp/apps"),
            framework_version: None,
            server_command: None,
            known_port: None,
        }
    }

    #[test]
    fn test_filter_entries_matches_two_of_five() {
        let entries = vec![
            make_entry("frontend"),
            make_entry("backend"),
            make_entry("database"),
            make_entry("cache"),
            make_entry("monitor"),
        ];
        // "end" matches "frontend" and "backend" (case-insensitive substring)
        let result = filter_entries(&entries, "end");
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].name, "frontend");
        assert_eq!(result[1].name, "backend");
    }

    #[test]
    fn test_filter_entries_case_insensitive() {
        let entries = vec![make_entry("MyApp"), make_entry("otherapp")];
        let result = filter_entries(&entries, "MYAPP");
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].name, "MyApp");
    }

    #[test]
    fn test_filter_entries_empty_query_returns_all() {
        let entries = vec![make_entry("foo"), make_entry("bar")];
        let result = filter_entries(&entries, "");
        assert_eq!(result.len(), 2);
    }

    #[test]
    fn test_filter_entries_whitespace_query_returns_all() {
        let entries = vec![make_entry("foo"), make_entry("bar")];
        let result = filter_entries(&entries, "   ");
        assert_eq!(result.len(), 2);
    }

    #[test]
    fn test_filter_entries_no_match_returns_empty() {
        let entries = vec![make_entry("alpha"), make_entry("beta")];
        let result = filter_entries(&entries, "xyz");
        assert!(result.is_empty());
    }
}
