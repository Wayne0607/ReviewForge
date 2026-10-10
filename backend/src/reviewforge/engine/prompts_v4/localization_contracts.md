# Localization verification boundaries

For direct language/script mismatches, the resource's declared locale is the
contract. Ask whether the changed phrase violates that contract; a page reference
does not decide it. Compare before/after and each site's own locale, preserving
proper names and code tokens. Do not infer a missing locale or a runtime failure
from this local content check.

## Formatting contracts

Establish the actual consuming formatter and its documented contract before calling
a token invalid. Source text alone does not establish which backend/frontend uses it.

Java `java.text.MessageFormat` supports `choice` subformats and repeated occurrences
of an argument index. Adjacent format elements are not nested merely because their
indices match. Validate full grammar and parameter types instead of assuming these
features are unsupported.

Apply this only to a proven Java consumer. Other formatters can use different
placeholder/plural grammars; trace the resource to its caller before claiming a mismatch.

Reference: [Java SE MessageFormat patterns and usage](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/text/MessageFormat.html).

## i18next and ICU

i18next's default interpolation uses double braces such as `{{name}}`. Prefix
and suffix can be overridden. ICU syntax requires an enabled formatter plugin
such as i18next-icu; importing react-i18next does not enable ICU by itself.

Trace the specific resource through loading/preprocessing to the actual
formatter call and initialization options. Backend formatting, conversions,
plugins and per-call overrides can change the contract. Neither neighboring
messages nor import-only hits establish that configuration. A failed search
cannot prove an alternative formatter is active, even alongside successful
unrelated observations.

References: [i18next interpolation and options](https://www.i18next.com/translation-function/interpolation),
[react-i18next ICU setup](https://react.i18next.com/misc/using-with-icu-format).
