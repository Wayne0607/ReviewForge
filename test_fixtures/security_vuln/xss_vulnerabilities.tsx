// XSS (Cross-Site Scripting) vulnerabilities across frontend frameworks.
// Purpose: verify all XSS patterns are detected.
import React, { useState } from 'react';

// ============================================================
// React XSS variants
// ============================================================

export function ReactXSS({ userBio }: { userBio: string }) {
    const [content, setContent] = useState('');

    return (
        <div>
            {/* BUG: dangerouslySetInnerHTML with user content */}
            <div dangerouslySetInnerHTML={{ __html: userBio }} />

            {/* BUG: dangerouslySetInnerHTML with dynamic state */}
            <div dangerouslySetInnerHTML={{ __html: content }} />

            {/* BUG: href with javascript: protocol from user input */}
            <a href={`javascript:void(${userBio})`}>Click</a>
        </div>
    );
}

// ============================================================
// Vanilla JS XSS variants
// ============================================================

function renderUserComment(userComment: string) {
    // BUG: innerHTML assignment with unsanitized user input
    document.getElementById('comments')!.innerHTML = userComment;
}

function showMessage(msg: string) {
    // BUG: document.write with user input
    document.write('<div class="msg">' + msg + '</div>');
}

function redirectUser(redirectTo: string) {
    // BUG: open redirect with user-controlled URL
    window.location.href = redirectTo;
}

// ============================================================
// Vue XSS patterns (embedded, tested in .vue files)
// ============================================================
// v-html="userContent"   -- XSS
// :href="userUrl"        -- open redirect if javascript: protocol

// ============================================================
// Svelte XSS patterns (embedded, tested in .svelte files)
// ============================================================
// {@html userContent}   -- XSS

// ============================================================
// Angular XSS patterns (embedded for reference)
// ============================================================
// [innerHTML]="userContent"          -- XSS
// bypassSecurityTrustHtml(content)   -- XSS bypass
