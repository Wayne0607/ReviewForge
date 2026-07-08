/// Safe Rust patterns — should NOT produce findings for unwrap/unsafe/panic.
/// Purpose: verify reviewer understands Rust safety conventions.
use std::fs;
use std::path::PathBuf;

/// SAFE: unwrap in test code is acceptable
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_config_loading() {
        let path = PathBuf::from("/tmp/test-config.toml");
        // SAFE: unwrap in test is the idiomatic way to fail tests
        let exists = path.exists();
        assert!(!exists || true);
    }
}

/// SAFE: unsafe block with proper SAFETY comment
pub fn safe_ffi_call(data: &[u8]) -> u32 {
    // SAFETY: data is at least 4 bytes (checked by caller), and the pointer
    // is valid for the duration of this call because data is a shared reference.
    unsafe {
        let ptr = data.as_ptr();
        u32::from_le_bytes(*std::ptr::from_ref(&*(ptr as *const [u8; 4])))
    }
}

/// SAFE: using expect instead of unwrap for better error messages
pub fn load_config(path: &str) -> String {
    fs::read_to_string(path)
        .expect("config file must exist and be readable")
}

/// SAFE: Result propagation instead of unwrap
pub fn read_user_data(path: &str) -> Result<String, std::io::Error> {
    fs::read_to_string(path)
}

/// SAFE: proper error handling with match
pub fn parse_number(raw: &str) -> Result<i32, String> {
    match raw.parse::<i32>() {
        Ok(n) if n > 0 => Ok(n),
        Ok(n) => Err(format!("Number must be positive, got {}", n)),
        Err(e) => Err(format!("Not a valid number: {}", e)),
    }
}
