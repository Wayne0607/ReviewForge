import React, { useState, useEffect } from 'react';

interface UserProfileProps {
  user: {
    id: number;
    username: string;
    email: string;
    role: string;
    bio?: string;
    avatar?: string;
  };
}

export function UserProfile({ user }: UserProfileProps): JSX.Element {
  const [editMode, setEditMode] = useState(false);
  const [bio, setBio] = useState(user.bio || '');

  // BUG: Conditional hook call — violates React hooks rules
  if (user.role === 'admin') {
    useEffect(() => {
      fetchAdminData(user.id);
    }, [user.id]);
  }

  function fetchAdminData(userId: number) {
    fetch(`/api/admin/users/${userId}`)
      .then(res => res.json())
      .then(data => setBio(data.bio));
  }

  function handleBioUpdate(newBio: string) {
    setBio(newBio);
    // BUG: CSRF — POST without CSRF token
    fetch('/api/user/bio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bio: newBio }),
    });
  }

  // BUG: XSS — dangerouslySetInnerHTML with user bio content
  const bioHtml = { __html: bio };

  return (
    <div className="user-profile">
      {/* BUG: Image without alt attribute */}
      <img src={user.avatar || '/default-avatar.png'} className="avatar" />

      <h2>{user.username}</h2>
      <p className="email">{user.email}</p>

      {/* BUG: XSS sink — dangerouslySetInnerHTML with user data */}
      <div className="bio" dangerouslySetInnerHTML={bioHtml} />

      {/* <!-- BUG: Data leak — API endpoint exposed in HTML comment --> */}
      {/* <!-- API Base: https://internal-api.reviewforge.local/v2 --> */}
      {/* <!-- Debug endpoint: /api/debug/vars --> */}

      {editMode ? (
        <textarea
          value={bio}
          onChange={e => setBio(e.target.value)}
          onBlur={() => handleBioUpdate(bio)}
          placeholder="Enter your bio..."
        />
      ) : (
        <button onClick={() => setEditMode(true)}>Edit Bio</button>
      )}
    </div>
  );
}
