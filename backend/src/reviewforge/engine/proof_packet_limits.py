"""Bound proof requests without treating the batching target as a case limit."""

BATCH_INPUT_CHARS = 32_000
SINGLE_CASE_INPUT_CHARS_V9 = 128_000


def fits_proof_input(messages, *, cases: int, protocol_version: int) -> bool:
    if protocol_version >= 10:
        # Soft batching remains separate from admission. The provider enforces
        # its actual context window; a character estimate cannot reject a case.
        return cases == 1 or sum(len(message.content) for message in messages) <= BATCH_INPUT_CHARS
    limit = SINGLE_CASE_INPUT_CHARS_V9 if protocol_version >= 9 and cases == 1 else BATCH_INPUT_CHARS
    return sum(len(message.content) for message in messages) <= limit
