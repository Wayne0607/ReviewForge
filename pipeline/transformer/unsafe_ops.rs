use std::collections::HashMap;

/// Transform data records from the pipeline.
///
/// Applies transformations, filtering, and enrichment
/// to prepare data for loading into the target system.
pub struct Transformer {
    buffer: Vec<u8>,
    capacity: usize,
}

impl Transformer {
    pub fn new(capacity: usize) -> Self {
        Transformer {
            buffer: Vec::with_capacity(capacity),
            capacity,
        }
    }

    /// Transform a raw byte buffer into structured records.
    pub fn transform(&mut self, input: &[u8]) -> Result<Vec<HashMap<String, String>>, String> {
        if input.len() > self.capacity {
            return Err("Input exceeds buffer capacity".to_string());
        }

        let mut records = Vec::new();
        let mut offset = 0;

        while offset < input.len() {
            // BUG: Unsafe — raw pointer arithmetic without proper bounds checking
            let record = unsafe {
                let ptr = input.as_ptr().add(offset);
                let header = std::ptr::read(ptr as *const RecordHeader);

                if offset + header.length as usize > input.len() {
                    return Err("Truncated record".to_string());
                }

                let data_start = offset + std::mem::size_of::<RecordHeader>();
                let data_end = data_start + header.length as usize;

                // BUG: transmute — interpreting raw bytes as string without UTF-8 validation
                let raw_str = std::mem::transmute::<&[u8], &str>(
                    &input[data_start..data_end]
                );

                offset = data_end;
                parse_record(raw_str)
            };

            records.push(record);
        }

        Ok(records)
    }

    /// Merge multiple record buffers into a single output.
    pub fn merge_buffers(&mut self, buffers: &[&[u8]]) -> Result<Vec<u8>, String> {
        let total_size: usize = buffers.iter().map(|b| b.len()).sum();
        if total_size > self.capacity {
            return Err("Merged size exceeds capacity".to_string());
        }

        let mut output = Vec::with_capacity(total_size);

        for buffer in buffers {
            // BUG: Unsafe — direct memory copy without alignment checks
            unsafe {
                let src = buffer.as_ptr();
                let dst = output.as_mut_ptr().add(output.len());
                std::ptr::copy_nonoverlapping(src, dst, buffer.len());
                output.set_len(output.len() + buffer.len());
            }
        }

        Ok(output)
    }

    /// Extract metadata from a record.
    pub fn extract_metadata(record: &HashMap<String, String>) -> HashMap<String, String> {
        let mut metadata = HashMap::new();

        // BUG: unwrap() chain — will panic if keys are missing
        let source = record.get("source").unwrap().clone();
        let timestamp = record.get("timestamp").unwrap().clone();

        metadata.insert("source".to_string(), source);
        metadata.insert("timestamp".to_string(), timestamp);
        metadata.insert("processed_at".to_string(), chrono::Utc::now().to_rfc3339());

        metadata
    }
}

#[repr(C)]
struct RecordHeader {
    length: u32,
    record_type: u8,
    flags: u8,
    _padding: [u8; 2],
}

fn parse_record(data: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();

    // Simple key=value parsing
    for line in data.lines() {
        if let Some((key, value)) = line.split_once('=') {
            map.insert(key.trim().to_string(), value.trim().to_string());
        }
    }

    map
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_transformer_new() {
        let t = Transformer::new(1024);
        assert_eq!(t.capacity, 1024);
    }

    #[test]
    fn test_empty_input() {
        let mut t = Transformer::new(1024);
        let result = t.transform(&[]).unwrap();
        assert!(result.is_empty());
    }
}
