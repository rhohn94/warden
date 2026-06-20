/// Ring-buffer log capture: retains the last N lines from a child process.
use std::collections::VecDeque;
use tokio::sync::mpsc;

/// Receives log lines from a child process stdout/stderr pipe.
/// Capacity-bounded: oldest lines are dropped when the buffer is full.
pub struct LogCapture {
    lines: VecDeque<String>,
    capacity: usize,
}

/// Minimum ring-buffer capacity; prevents an unbounded buffer when the caller passes 0.
const MIN_LOG_CAPACITY: usize = 1;

impl LogCapture {
    /// Create a new ring buffer with the given capacity, clamped to at least MIN_LOG_CAPACITY.
    pub fn new(capacity: usize) -> Self {
        let capacity = capacity.max(MIN_LOG_CAPACITY);
        Self {
            lines: VecDeque::with_capacity(capacity),
            capacity,
        }
    }

    /// Push a new line into the ring buffer. Drops the oldest line if at capacity.
    pub fn push(&mut self, line: String) {
        if self.lines.len() == self.capacity {
            self.lines.pop_front();
        }
        self.lines.push_back(line);
    }

    /// Snapshot of current lines in oldest-to-newest order.
    pub fn lines(&self) -> Vec<String> {
        self.lines.iter().cloned().collect()
    }

    /// Number of lines currently stored.
    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.lines.len()
    }
}

/// Sender half for streaming log lines from a child-process reader task.
pub type LogSender = mpsc::UnboundedSender<String>;

/// Receiver half polled in the egui frame loop.
pub type LogReceiver = mpsc::UnboundedReceiver<String>;

/// Creates a channel pair for streaming log lines.
/// The sender goes to the child-process reader task; the receiver is polled
/// in the egui frame loop via `try_recv()`.
pub fn log_channel() -> (LogSender, LogReceiver) {
    mpsc::unbounded_channel()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ring_buffer_capacity() {
        let mut cap = LogCapture::new(500);
        for i in 0..600 {
            cap.push(format!("line {}", i));
        }
        assert_eq!(cap.len(), 500);
        // Lines 0..100 were evicted; line 100 is oldest retained.
        let lines = cap.lines();
        assert_eq!(lines[0], "line 100");
        assert_eq!(lines[499], "line 599");
    }

    #[test]
    fn test_lines_oldest_to_newest() {
        let mut cap = LogCapture::new(10);
        cap.push("a".to_string());
        cap.push("b".to_string());
        cap.push("c".to_string());
        assert_eq!(cap.lines(), vec!["a", "b", "c"]);
    }

    #[test]
    fn test_push_below_capacity() {
        let mut cap = LogCapture::new(500);
        cap.push("x".to_string());
        cap.push("y".to_string());
        cap.push("z".to_string());
        assert_eq!(cap.len(), 3);
        assert_eq!(cap.lines(), vec!["x", "y", "z"]);
    }

    #[test]
    fn test_zero_capacity_clamped_to_one() {
        // LogCapture::new(0) must produce a bounded buffer clamped to capacity 1.
        let mut cap = LogCapture::new(0);
        for i in 0..5 {
            cap.push(format!("line {}", i));
        }
        // Only the most-recent line is retained; buffer never grows unbounded.
        assert_eq!(cap.len(), 1);
        assert_eq!(cap.lines(), vec!["line 4"]);
    }
}
