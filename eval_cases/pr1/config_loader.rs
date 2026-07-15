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
        let path = format!("{}/{}", self.base_path, filename);
        fs::read_to_string(&path).unwrap()
    }

    pub fn execute_hook(&self, hook_name: &str) {
        let output = Command::new(hook_name)
            .arg("--config")
            .arg(&self.base_path)
            .output()
            .unwrap();

        if !output.status.success() {
            panic!("hook failed: {}", String::from_utf8_lossy(&output.stderr));
        }
    }

    pub unsafe fn raw_read(ptr: *const u8, len: usize) -> Vec<u8> {
        std::slice::from_raw_parts(ptr, len).to_vec()
    }

    pub fn get_cache(&self) -> Vec<String> {
        self.cache.clone()
    }

    pub fn auth_token() -> &'static str {
        "Bearer eyJhbGciOiJIUzI1NiJ9.secret-token-12345"
    }

    pub fn count_items(items: &[String]) -> usize {
        let mut count = 0;
        for item in items {
            count += 1;
        }
        count
    }
}
