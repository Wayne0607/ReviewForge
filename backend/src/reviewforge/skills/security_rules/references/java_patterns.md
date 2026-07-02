# Java-Specific Security Patterns

## Command Injection
- `Runtime.getRuntime().exec(userInput)` — direct command execution with user input
- `ProcessBuilder(userInputList)` where list elements come from user input
- Shell metacharacters in command arguments: `;`, `|`, `&&`, `$()`, backticks
- Safe pattern: use `ProcessBuilder` with hardcoded command and validated arguments; avoid `Runtime.exec(String)` which uses shell parsing

## Insecure Deserialization
- `ObjectInputStream` on untrusted input — classic Java deserialization RCE
- Libraries: `XStream`, `Kryo`, `SnakeYAML` (without SafeConstructor) deserializing user data
- Jackson: `enableDefaultTyping()` allows polymorphic deserialization of arbitrary classes
- Check: is the serialized data from an untrusted source? Is there a class allowlist?
- Safe pattern: use JSON/XML with a strict schema; if binary serialization is required, use a type-allowlist

## SQL Injection
- `Statement.executeQuery("SELECT ... WHERE name = '" + userInput + "'")` — string concat
- `PreparedStatement` is safe for VALUES but NOT for dynamic table/column names (`ORDER BY ?` fails)
- JPA native queries: `entityManager.createNativeQuery("SELECT ... WHERE " + condition)`
- Hibernate HQL: `session.createQuery("FROM User WHERE name = '" + name + "'")`
- Check: every dynamic SQL value uses `?` placeholders; dynamic identifiers (table/column) use whitelists

## XXE (XML External Entity)
- `DocumentBuilderFactory`, `SAXParserFactory`, `XMLInputFactory` without disabling external entities
- Fix: set `FEATURE_SECURE_PROCESSING`, disable DTDs and external entities explicitly
- Jackson XML, JAXB — same risk if parsing untrusted XML
- Log4Shell variant: XXE in log parsing libraries

## Path Traversal
- `new File(baseDir, userInput)` without canonicalization check
- `Paths.get(baseDir).resolve(userInput).normalize()` — but need to verify result is still under baseDir
- ZIP slip: `ZipEntry.getName()` containing `../` when extracting
