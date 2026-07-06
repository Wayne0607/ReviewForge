use std::io;
use std::net::TcpStream;

/// Send data to a remote processing endpoint.
pub fn send_to_endpoint(endpoint: &str, data: &[u8]) -> io::Result<usize> {
    // BUG: panic! instead of returning error — crashes the entire process
    let mut stream = match TcpStream::connect(endpoint) {
        Ok(s) => s,
        Err(e) => panic!("Failed to connect to {}: {}", endpoint, e),
    };

    use std::io::Write;
    stream.write(data).map_err(|e| {
        // BUG: panic! in error handler — should return Err
        panic!("Write failed: {}", e);
    })
}

/// Receive data from a remote endpoint with timeout.
pub fn receive_from_endpoint(endpoint: &str) -> io::Result<Vec<u8>> {
    let stream = TcpStream::connect(endpoint)?;

    // BUG: unwrap() on potentially failing operation
    stream.set_read_timeout(Some(std::time::Duration::from_secs(5))).unwrap();

    let mut buffer = Vec::new();
    use std::io::Read;
    let mut reader = io::BufReader::new(stream);
    reader.read_to_end(&mut buffer)?;

    Ok(buffer)
}

/// Health check for a remote service.
pub fn check_health(endpoint: &str) -> bool {
    // BUG: unwrap() on connection attempt — panics on DNS failure
    let stream = TcpStream::connect(endpoint).unwrap();
    stream.peer_addr().is_ok()
}
