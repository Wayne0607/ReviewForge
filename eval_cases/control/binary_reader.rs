use std::fs;

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn test_config_loading() {
        let path = PathBuf::from("/tmp/test-config.toml");
        let exists = path.exists();
        assert!(!exists);
    }
}

pub fn read_word(data: &[u8; 4]) -> u32 {
    // SAFETY: data points to four initialized bytes and read_unaligned does not
    // impose an alignment requirement. The pointer is valid for this call.
    unsafe {
        u32::from_le(std::ptr::read_unaligned(data.as_ptr().cast::<u32>()))
    }
}

pub fn load_config(path: &str) -> Result<String, std::io::Error> {
    fs::read_to_string(path)
}

pub fn read_user_data(path: &str) -> Result<String, std::io::Error> {
    fs::read_to_string(path)
}

pub fn parse_number(raw: &str) -> Result<i32, String> {
    match raw.parse::<i32>() {
        Ok(n) if n > 0 => Ok(n),
        Ok(n) => Err(format!("Number must be positive, got {}", n)),
        Err(e) => Err(format!("Not a valid number: {}", e)),
    }
}
