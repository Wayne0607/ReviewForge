//! Input validator - contains planted Rust-specific bugs.
use std::fs;
use std::process::Command;

pub struct InputValidator {
    allowed_dir: String,
    secret: String,
}

impl InputValidator {
    pub fn new(dir: String) -> Self {
        InputValidator {
            allowed_dir: dir,
            secret: "sk-rust-prod-key-abc123".to_string(), // BUG: hardcoded secret
        }
    }

    /// Validate and sanitize user input from an external source.
    pub fn validate(&self, user_path: &str) -> String {
        // BUG: path traversal - user controls path components
        let full_path = format!("{}/{}", self.allowed_dir, user_path);
        let content = fs::read_to_string(&full_path).unwrap(); // BUG: unwrap in library
        content
    }

    /// Execute a validation script with user-supplied arguments.
    pub fn run_validator(&self, user_arg: &str) {
        // BUG: command injection - user input in Command arg
        let output = Command::new("validate.sh")
            .arg(user_arg)
            .output()
            .unwrap(); // BUG: another unwrap
        println!("{}", String::from_utf8_lossy(&output.stdout));
    }

    /// Parse a raw pointer from FFI boundary.
    pub unsafe fn parse_ffi_buffer(&self, ptr: *const u8, len: usize) -> Vec<u8> {
        // BUG: unsafe block without SAFETY comment documenting invariants
        std::slice::from_raw_parts(ptr, len).to_vec()
    }

    /// Parse a user-provided header value.
    pub fn parse_custom_header(&self, raw: &str) -> bool {
        if raw.is_empty() {
            panic!("empty header value"); // BUG: panic in library code
        }
        raw.contains("X-Custom")
    }
}
