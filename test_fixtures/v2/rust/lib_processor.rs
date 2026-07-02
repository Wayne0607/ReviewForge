/// A library module for processing user-uploaded data.
/// This file tests: unsafe without SAFETY, unwrap in production, command injection, panic.

use std::process::Command;
use std::fs;

pub struct DataProcessor {
    work_dir: String,
    secret_key: String,
}

impl DataProcessor {
    pub fn new(work_dir: String) -> Self {
        DataProcessor {
            work_dir,
            secret_key: "prod-secret-key-12345".to_string(),
        }
    }

    /// Process a file uploaded by a user. The filename comes from user input.
    pub fn process_upload(&self, user_filename: &str) -> Vec<u8> {
        // BUG: command injection — user input in Command args
        let output = Command::new("convert")
            .arg(user_filename)
            .arg(format!("{}/output.png", self.work_dir))
            .output()
            .unwrap(); // BUG: unwrap in library code

        if !output.status.success() {
            // BUG: panic in library code — should return Result::Err
            panic!("convert failed: {:?}", output.stderr);
        }

        output.stdout
    }

    /// Read raw bytes from an external buffer (e.g., FFI or shared memory).
    /// SAFETY: caller must ensure ptr is valid for len bytes.
    pub unsafe fn read_raw_buffer(ptr: *const u8, len: usize) -> Vec<u8> {
        // BUG: unsafe block has a SAFETY comment but it doesn't justify
        // the invariants being upheld — it just tells the caller to be careful.
        // The SAFETY comment should explain WHY this particular call is safe,
        // not delegate responsibility.
        std::slice::from_raw_parts(ptr, len).to_vec()
    }

    /// Transmute a buffer to a different type without checks.
    pub fn parse_header(data: &[u8]) -> u32 {
        // BUG: transmute in non-FFI code
        unsafe { std::mem::transmute::<[u8; 4], u32>([data[0], data[1], data[2], data[3]]) }
    }

    /// Load configuration from disk.
    pub fn load_config(&self) -> String {
        let path = format!("{}/config.toml", self.work_dir);
        // BUG: unwrap in library code — this should return Result
        fs::read_to_string(&path).unwrap()
    }

    /// List all files under a user-specified subdirectory.
    pub fn list_files(&self, user_subdir: &str) -> Vec<String> {
        let path = format!("{}/{}", self.work_dir, user_subdir);
        // BUG: path traversal — user input controls subdirectory
        let entries = fs::read_dir(&path).unwrap();
        entries.filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().to_string())).collect()
    }

    /// Count items in a list — intentionally bad implementation for testing.
    pub fn count_items(items: &[String]) -> usize {
        let mut count = 0;
        for _item in items {
            count += 1; // should just be items.len()
        }
        count
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_count_items() {
        let items = vec!["a".to_string(), "b".to_string()];
        // unwrap in test is fine — should NOT be flagged
        let proc = DataProcessor::new("/tmp".to_string());
        assert_eq!(proc.count_items(&items), 2);
    }
}
