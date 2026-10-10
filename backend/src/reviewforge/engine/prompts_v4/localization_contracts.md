# Formatting contracts

Establish the actual consuming formatter and its documented contract before calling
a token invalid. Source text alone does not establish which backend/frontend uses it.

Java `java.text.MessageFormat` supports `choice` subformats and repeated occurrences
of an argument index. Adjacent format elements are not nested merely because their
indices match. Validate full grammar and parameter types instead of assuming these
features are unsupported.

Apply this only to a proven Java consumer. Other formatters can use different
placeholder/plural grammars; trace the resource to its caller before claiming a mismatch.

Reference: [Java SE MessageFormat patterns and usage](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/text/MessageFormat.html).
