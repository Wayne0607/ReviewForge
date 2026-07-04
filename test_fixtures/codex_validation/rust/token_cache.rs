use std::fs;

pub fn cache_token(user_id: &str, token: &str) -> std::io::Result<()> {
    let path = format!("/tmp/reviewforge-cache/{}.token", user_id);
    fs::write(path, token)
}

pub fn read_token(user_id: &str) -> std::io::Result<String> {
    let path = format!("/tmp/reviewforge-cache/{}.token", user_id);
    fs::read_to_string(path)
}
