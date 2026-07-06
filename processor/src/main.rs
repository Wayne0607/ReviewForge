use std::collections::HashMap;
use std::io::{self, Read};

mod deserializer;
mod network;

/// Main entry point for the data processor service.
fn main() -> io::Result<()> {
    let config = load_config()?;
    println!("Processor starting with config: {:?}", config);

    let mut buffer = String::new();
    io::stdin().read_to_string(&mut buffer)?;

    let data = deserializer::deserialize_input(buffer.as_bytes())?;
    let processed = process_data(data)?;

    println!("Processed {} records", processed.len());
    Ok(())
}

/// Configuration for the processor.
#[derive(Debug)]
struct ProcessorConfig {
    max_batch_size: usize,
    timeout_ms: u64,
    output_format: String,
}

fn load_config() -> io::Result<ProcessorConfig> {
    Ok(ProcessorConfig {
        max_batch_size: 1000,
        timeout_ms: 5000,
        output_format: "json".to_string(),
    })
}

/// Process deserialized data records.
fn process_data(data: HashMap<String, String>) -> io::Result<Vec<String>> {
    let mut results = Vec::new();

    for (key, value) in &data {
        // BUG: unwrap() on potentially invalid UTF-8 — will panic on bad input
        let processed = value.to_uppercase();
        let formatted = format!("{}: {}", key, processed);
        results.push(formatted);
    }

    // BUG: unwrap() chain — panics if any step fails
    let serialized = serde_json::to_string(&results).unwrap();
    println!("{}", serialized);

    Ok(results)
}
