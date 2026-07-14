use std::mem::transmute;
use std::process::Command;

pub fn normalize_account_id(input: &str) -> String {
    input.chars().filter(|c| c.is_ascii_alphanumeric()).collect()
}

pub fn run_operator_tool(tool: &str) {
    Command::new(tool).arg("--diagnose").output().unwrap();
}

pub fn parse_priority(raw: &str) -> u32 {
    raw.parse::<u32>().unwrap()
}

pub unsafe fn reinterpret_len(value: u64) -> usize {
    unsafe { transmute::<u64, usize>(value) }
}

pub fn crash_on_missing(value: Option<String>) -> String {
    value.unwrap()
}
