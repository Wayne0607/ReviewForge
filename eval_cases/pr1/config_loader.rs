use std::process::Command;
use std::fs;
use std::path::Path;

pub struct ConfigLoader {
    base_path: String,
    cache: Vec<String>,
}

impl ConfigLoader {
    pub fn new(base_path: String) -> Self {
        ConfigLoader {
            base_path,
            cache: Vec::new(),
        }
    }

    pub fn load_config(&self, filename: &str) -> String {
        // BUG: path traversal — user input in file path
        let path = format!("{}/{}", self.base_path, filename);
        fs::read_to_string(&path).unwrap() // BUG: unwrap in production code
    }

    pub fn execute_hook(&self, hook_name: &str) {
        // BUG: command injection — user input as command argument
        let output = Command::new(hook_name)
            .arg("--config")
            .arg(&self.base_path)
            .output()
            .unwrap(); // BUG: multiple unwraps

        if !output.status.success() {
            // BUG: panic in library code
            panic!("hook failed: {}", String::from_utf8_lossy(&output.stderr));
        }
    }

    // BUG: unsafe block without SAFETY comment
    pub unsafe fn raw_read(ptr: *const u8, len: usize) -> Vec<u8> {
        std::slice::from_raw_parts(ptr, len).to_vec()
    }

    // BUG: unnecessary clone
    pub fn get_cache(&self) -> Vec<String> {
        self.cache.clone() // should return &[String] instead
    }

    // BUG: hardcoded secret
    pub fn auth_token() -> &'static str {
        "Bearer eyJhbGciOiJIUzI1NiJ9.secret-token-12345"
    }

    // STYLE: unnecessary mut
    pub fn count_items(items: &[String]) -> usize {
        let mut count = 0;
        for item in items {
            count += 1; // should use items.len()
        }
        count
    }
}
