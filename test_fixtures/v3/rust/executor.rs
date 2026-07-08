/// Command executor - user-facing API. Contains planted security bugs.
use std::process::Command;

pub struct Executor {
    work_dir: String,
    api_key: String,
}

impl Executor {
    pub fn new(work_dir: String) -> Self {
        Executor {
            work_dir,
            api_key: "sk-secret-rust-key-12345".to_string(),
        }
    }

    /// Run a user-specified command. The cmd_name comes from user input.
    pub fn run(&self, cmd_name: &str) -> String {
        // BUG: command injection - user controls the command executable
        let output = Command::new(cmd_name)
            .arg("--work-dir")
            .arg(&self.work_dir)
            .output()
            .unwrap(); // BUG: unwrap in library code

        String::from_utf8_lossy(&output.stdout).to_string()
    }

    /// Execute a shell script with user input.
    pub fn run_shell(&self, user_script: &str) {
        // BUG: command injection via shell -c
        Command::new("bash")
            .arg("-c")
            .arg(user_script)
            .spawn()
            .unwrap(); // BUG: another unwrap
    }

    /// Parse raw bytes from untrusted source.
    pub unsafe fn parse_raw(&self, ptr: *const u8, len: usize) -> &[u8] {
        // BUG: unsafe without SAFETY comment
        std::slice::from_raw_parts(ptr, len)
    }

    /// Read a user-specified file path.
    pub fn read_user_file(&self, user_path: &str) -> String {
        // BUG: path traversal - user controls the path
        let full = format!("{}/{}", self.work_dir, user_path);
        std::fs::read_to_string(&full).unwrap() // BUG: unwrap + path traversal
    }
}
