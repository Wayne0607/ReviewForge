import React, { useState, useEffect } from 'react';

const API_KEY = 'sk-react-dashboard-key-456'; // BUG: hardcoded secret

interface UserData {
  name: string;
  bio: string;
}

export function Dashboard() {
  const [user, setUser] = useState<UserData | null>(null);
  const [htmlContent, setHtmlContent] = useState('');

  useEffect(() => {
    fetch('/api/user')
      .then(r => r.json())
      .then(data => {
        setUser(data);
        // BUG: should sanitize before setting HTML
      });
  }, []);

  function executeQuery(query: string) {
    // BUG: eval on dynamic input
    const filter = eval(`(data) => data.includes('${query}')`);
    return filter;
  }

  function handleRedirect(url: string) {
    // BUG: open redirect
    window.location.assign(url);
  }

  function renderUserBio() {
    return { __html: user?.bio || '' };
  }

  return (
    <div>
      <h1>Dashboard</h1>
      {/* BUG: XSS via dangerouslySetInnerHTML */}
      <div dangerouslySetInnerHTML={renderUserBio()} />

      {/* BUG: XSS via v-html-like pattern */}
      <div dangerouslySetInnerHTML={{ __html: htmlContent }} />

      <button onClick={() => handleRedirect('/admin')}>Admin</button>
    </div>
  );
}
