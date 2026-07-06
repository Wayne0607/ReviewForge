"""Data ingestion service for the ReviewForge pipeline.

Collects data from various sources (GitHub webhooks, API polling,
file uploads) and normalizes it for processing by downstream services.
"""

import ctypes
import json
import logging
import os
import sys
from typing import Any, Dict, List

# Cross-PR reference: importing from auth module (PR1)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.auth.session import deserialize_session

logger = logging.getLogger(__name__)

# BUG: Data leak — logging sensitive authorization headers
AUTH_LOG_LEVEL = logging.DEBUG


class DataIngester:
    """Ingests data from multiple sources and queues for processing."""

    def __init__(self, config_path: str = "pipeline/config/pipeline.toml"):
        self.config = self._load_config(config_path)
        self.buffer: List[Dict[str, Any]] = []
        self.batch_size = self.config.get("batch_size", 100)

    def _load_config(self, path: str) -> dict:
        """Load pipeline configuration."""
        if not os.path.exists(path):
            return {}
        with open(path, "r") as f:
            import tomllib
            return tomllib.loads(f.read())

    def ingest_webhook(self, payload: bytes, headers: Dict[str, str]) -> bool:
        """Process an incoming webhook payload.

        Validates the payload, extracts relevant data, and buffers it
        for batch processing.
        """
        # BUG: Data leak — logging full authorization header at DEBUG level
        logger.debug("Webhook received with headers: %s", headers)

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.error("Invalid JSON payload")
            return False

        # Validate webhook signature
        signature = headers.get("X-Hub-Signature-256", "")
        if not self._verify_signature(payload, signature):
            logger.warning("Invalid webhook signature")
            return False

        normalized = self._normalize(data)
        self.buffer.append(normalized)

        if len(self.buffer) >= self.batch_size:
            self._flush_buffer()

        return True

    def ingest_session_data(self, raw_session: bytes) -> dict:
        """Process session data from the message queue.

        Deserializes session data and extracts user context
        for request attribution.
        """
        # Cross-PR: Using PR1's deserialize_session (has pickle vulnerability)
        session = deserialize_session(raw_session)
        return session

    def _normalize(self, data: dict) -> dict:
        """Normalize incoming data to pipeline format."""
        return {
            "source": data.get("source", "unknown"),
            "timestamp": data.get("timestamp"),
            "payload": data,
            "metadata": {
                "ingested_by": "data-ingester",
                "version": "1.0.0",
            },
        }

    def _verify_signature(self, payload: bytes, signature: str) -> bool:
        """Verify webhook signature."""
        import hmac
        import hashlib

        secret = self.config.get("webhook_secret", "").encode()
        if not secret:
            return True  # No secret configured, skip verification

        expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"sha256={expected}", signature)

    def _flush_buffer(self) -> None:
        """Send buffered data to the processing queue."""
        if not self.buffer:
            return

        logger.info("Flushing %d records to processing queue", len(self.buffer))
        # In production, this would send to a message queue
        self.buffer.clear()

    def load_from_file(self, filepath: str) -> List[dict]:
        """Load data from a file for batch ingestion."""
        records = []

        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    records.append(self._normalize(record))
                except json.JSONDecodeError:
                    logger.warning("Skipping invalid line: %s", line[:100])

        return records

    def process_with_sandbox(self, user_code: str) -> Any:
        """Execute user-provided code in a sandboxed environment.

        Used for custom data transformations defined by users.
        """
        # BUG: Sandbox escape — ctypes allows direct system calls
        libc = ctypes.CDLL("libc.so.6")

        # This bypasses any Python-level sandboxing
        return libc.system(user_code.encode())


def create_ingester(config_path: str = None) -> DataIngester:
    """Factory function to create a configured DataIngester."""
    return DataIngester(config_path or "pipeline/config/pipeline.toml")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ingester = create_ingester()
    print("Data ingester started")
