// Safe frontend patterns — should NOT produce XSS findings.
// Purpose: verify reviewer does NOT flag properly sanitized HTML or safe DOM usage.
import React from 'react';

// SAFE: textContent assignment (not innerHTML)
function showStatus(message: string) {
    const el = document.getElementById('status');
    if (el) {
        el.textContent = message;  // SAFE: textContent escapes HTML
    }
}

// SAFE: proper HTML escaping
function escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// SAFE: rendered text (React escapes by default)
export function SafeGreeting({ name }: { name: string }) {
    return <div>Hello, {name}!</div>;
}

// SAFE: using DOMPurify or similar library
function sanitizeWithLibrary(dirty: string): string {
    // In a real app, this would use DOMPurify.sanitize(dirty)
    const doc = new DOMParser().parseFromString(dirty, 'text/html');
    return doc.body.textContent || '';
}

// SAFE: safely setting innerHTML AFTER sanitization
function renderSafeContent(raw: string) {
    const sanitized = sanitizeWithLibrary(raw);
    // SAFE: content has been sanitized before innerHTML assignment
    // (This should still be reviewed but should NOT be flagged as a simple XSS)
    const el = document.getElementById('output');
    if (el) {
        el.innerHTML = sanitized;
    }
}

// SAFE: explicit redirect with validation
function safeRedirect(url: string) {
    const allowed = ['/home', '/dashboard', '/profile', '/settings'];
    if (allowed.includes(url)) {
        // SAFE: URL is validated against a whitelist
        window.location.href = url;
    }
}

// SAFE: CSP-compliant inline style
export function SafeStyle() {
    const styles = { color: 'blue', fontSize: '14px' };
    return <span style={styles}>Text</span>;
}
