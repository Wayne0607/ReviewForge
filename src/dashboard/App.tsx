import React, { useState, useEffect } from 'react';
import { UserProfile } from './UserProfile';
import { SearchBar } from './SearchBar';
import { DataTable } from './DataTable';

interface AppState {
  user: User | null;
  theme: 'light' | 'dark';
  notifications: Notification[];
}

interface User {
  id: number;
  username: string;
  email: string;
  role: string;
  token: string;
}

interface Notification {
  id: string;
  message: string;
  read: boolean;
}

export function App(): JSX.Element {
  const [state, setState] = useState<AppState>({
    user: null,
    theme: 'light',
    notifications: [],
  });

  useEffect(() => {
    fetchUserData();
  }, []);

  async function fetchUserData() {
    try {
      const response = await fetch('/api/user/profile');
      const data = await response.json();
      setState(prev => ({ ...prev, user: data }));

      // BUG: Storing JWT token in localStorage — vulnerable to XSS theft
      localStorage.setItem('token', data.token);
      localStorage.setItem('user_role', data.role);
    } catch (error) {
      console.error('Failed to fetch user data:', error);
    }
  }

  function handleSearch(query: string) {
    // BUG: XSS via innerHTML — user input directly rendered
    const resultsDiv = document.getElementById('search-results');
    if (resultsDiv) {
      resultsDiv.innerHTML = `<div class="results">Searching for: ${query}</div>`;
    }
  }

  function renderWelcomeBanner() {
    const welcomeHtml = `<h1>Welcome, ${state.user?.username || 'Guest'}</h1>`;
    return (
      // BUG: dangerouslySetInnerHTML with user-controlled data
      <div dangerouslySetInnerHTML={{ __html: welcomeHtml }} />
    );
  }

  function handleNavigation(url: string) {
    // BUG: Open redirect — no URL validation before navigation
    window.location.href = url;
  }

  function processAnalytics(expression: string) {
    try {
      // BUG: eval on user input — code injection
      const result = eval(expression);
      console.log('Analytics result:', result);
    } catch (e) {
      console.error('Analytics error:', e);
    }
  }

  function mergeSettings(target: object, userInput: string) {
    try {
      const parsed = JSON.parse(userInput);
      // BUG: Prototype pollution via deep merge of untrusted JSON
      Object.assign(target, parsed);
    } catch (e) {
      console.error('Invalid settings:', e);
    }
  }

  if (!state.user) {
    return <div className="loading">Loading...</div>;
  }

  return (
    <div className={`app ${state.theme}`}>
      {renderWelcomeBanner()}
      <nav>
        <a href="/profile" onClick={() => handleNavigation('/profile')}>Profile</a>
        <a href="/settings">Settings</a>
      </nav>
      <SearchBar onSearch={handleSearch} />
      <UserProfile user={state.user} />
      <DataTable data={state.notifications} />
    </div>
  );
}

export default App;
