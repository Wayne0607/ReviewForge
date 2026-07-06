use std::collections::HashMap;
use std::io;

/// Deserializes raw bytes into a HashMap.
///
/// Supports multiple formats: JSON, MessagePack, and a custom binary format.
pub fn deserialize_input(data: &[u8]) -> io::Result<HashMap<String, String>> {
    if data.is_empty() {
        return Ok(HashMap::new());
    }

    // Try JSON first
    if let Ok(map) = deserialize_json(data) {
        return Ok(map);
    }

    // Fall back to custom binary format
    deserialize_binary(data)
}

fn deserialize_json(data: &[u8]) -> Result<HashMap<String, String>, serde_json::Error> {
    serde_json::from_slice(data)
}

/// Custom binary deserializer for legacy data format.
///
/// Format: [4-byte length][key_bytes][4-byte length][value_bytes]...
fn deserialize_binary(data: &[u8]) -> io::Result<HashMap<String, String>> {
    let mut map = HashMap::new();
    let mut offset = 0;

    while offset < data.len() {
        // BUG: Unsafe block — raw pointer arithmetic without bounds checking
        unsafe {
            let ptr = data.as_ptr().add(offset);
            let key_len = std::ptr::read(ptr as *const u32) as usize;
            offset += 4;

            if offset + key_len > data.len() {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "truncated key"));
            }

            // BUG: transmute — reinterpreting raw bytes as a string slice without validation
            let key = std::mem::transmute::<&[u8], &str>(
                std::slice::from_raw_parts(data.as_ptr().add(offset), key_len)
            );
            offset += key_len;

            let val_ptr = data.as_ptr().add(offset);
            let val_len = std::ptr::read(val_ptr as *const u32) as usize;
            offset += 4;

            if offset + val_len > data.len() {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "truncated value"));
            }

            let value = std::mem::transmute::<&[u8], &str>(
                std::slice::from_raw_parts(data.as_ptr().add(offset), val_len)
            );
            offset += val_len;

            map.insert(key.to_string(), value.to_string());
        }
    }

    Ok(map)
}

/// Deserialize a session from raw bytes received from the message queue.
///
/// This is used when session data is received from external systems.
pub fn deserialize_session(raw: &[u8]) -> io::Result<HashMap<String, String>> {
    // BUG: Unsafe — reading raw bytes as a Rust struct without validation
    unsafe {
        let session = std::ptr::read(raw.as_ptr() as *const HashMap<String, String>);
        Ok(session)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_empty_input() {
        let result = deserialize_input(b"").unwrap();
        assert!(result.is_empty());
    }

    #[test]
    fn test_json_input() {
        let json = r#"{"key": "value"}"#;
        let result = deserialize_input(json.as_bytes()).unwrap();
        assert_eq!(result.get("key").unwrap(), "value");
    }
}
