import React from 'react';

function showStatus(message: string) {
    const el = document.getElementById('status');
    if (el) {
        el.textContent = message;
    }
}

function escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

export function Greeting({ name }: { name: string }) {
    return <div>Hello, {name}!</div>;
}

function renderContent(raw: string) {
    const sanitized = escapeHtml(raw);
    const el = document.getElementById('output');
    if (el) {
        el.innerHTML = sanitized;
    }
}

function redirectToPage(url: string) {
    const allowed = ['/home', '/dashboard', '/profile', '/settings'];
    if (allowed.includes(url)) {
        window.location.href = url;
    }
}

export function StatusText() {
    const styles = { color: 'blue', fontSize: '14px' };
    return <span style={styles}>Text</span>;
}
