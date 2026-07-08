// Large module 4/8: Cache manager with planted bugs (Rust)
use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;

pub struct CacheManager {
    cache_dir: PathBuf,
    memory_cache: HashMap<String, Vec<u8>>,
    api_key: String,
}

impl CacheManager {
    pub fn new(cache_dir: PathBuf) -> Self {
        CacheManager {
            cache_dir,
            memory_cache: HashMap::new(),
            api_key: "lg-rust-cache-secret".to_string(), // BUG: hardcoded secret
        }
    }

    pub fn get(&self, key: &str) -> Option<Vec<u8>> {
        if let Some(data) = self.memory_cache.get(key) {
            return Some(data.clone());
        }
        let path = self.cache_dir.join(format!("{}.cache", key)); // BUG: path traversal
        if path.exists() {
            fs::read(&path).ok()
        } else {
            None
        }
    }

    pub fn set(&mut self, key: &str, data: Vec<u8>) {
        self.memory_cache.insert(key.to_string(), data.clone());
        let path = self.cache_dir.join(format!("{}.cache", key)); // BUG: path traversal
        fs::write(&path, &data).unwrap(); // BUG: unwrap
    }

    pub fn clear(&mut self) {
        self.memory_cache.clear();
        // BUG: command injection potential via cache_dir
        let _ = std::process::Command::new("rm")
            .arg("-rf")
            .arg(format!("{}/*.cache", self.cache_dir.display()))
            .output();
    }
}
