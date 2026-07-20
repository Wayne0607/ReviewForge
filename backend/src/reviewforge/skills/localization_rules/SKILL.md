---
name: localization_rules
description: Detects concrete locale-resource language and formatting defects
category: quality
reviewer_type: localization
---

# Localization review method

Treat the locale encoded in the path or filename as a contract. Review only
added or modified entries, and report a finding only when the changed text
provides direct evidence that the contract is broken.

## High-signal defects

- A complete phrase is written in a language that does not match the declared locale.
- A region-specific script is wrong, such as Traditional Chinese wording in `zh_CN`.
- A translation accidentally mixes a foreign sentence into otherwise local text.
- Placeholders, ICU arguments, HTML tags, escapes, or plural branches differ in a way
  that will break formatting or change runtime semantics.
- Mojibake, invalid escapes, or control characters make the resource unreadable.

## Evidence rules

- Quote the smallest distinctive fragment and name both the declared and observed language/script.
- Use the exact changed line. Do not infer defects in unchanged sibling resources.
- Product names, acronyms, API names, URLs, code tokens, and common borrowed technical terms
  are not language mismatches by themselves.
- Do not grade translation quality or recommend stylistic rewrites.
- When the same bad translation appears in several changed bundles, report one primary finding
  and list the other locations in its message instead of emitting repetitive comments.
