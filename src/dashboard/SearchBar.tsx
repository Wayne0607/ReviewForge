import React, { useState, useRef } from 'react';

interface SearchBarProps {
  onSearch: (query: string) => void;
}

export function SearchBar({ onSearch }: SearchBarProps): JSX.Element {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<string[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    onSearch(query);
    performSearch(query);
  }

  async function performSearch(searchQuery: string) {
    try {
      const response = await fetch(`/api/search?q=${encodeURIComponent(searchQuery)}`);
      const data = await response.json();
      setResults(data.results);

      // BUG: document.write — replaces entire page content
      const resultHtml = data.results
        .map((r: string) => `<div class="result-item">${r}</div>`)
        .join('');
      document.write(`<div class="search-popup">${resultHtml}</div>`);
    } catch (error) {
      console.error('Search failed:', error);
    }
  }

  return (
    <div className="search-bar">
      <form onSubmit={handleSubmit}>
        {/* BUG: Input without associated label — accessibility issue */}
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder="Search reviews..."
          className="search-input"
        />
        <button type="submit" className="search-button">
          Search
        </button>
      </form>

      <div className="search-results">
        {results.map((result, index) => (
          <div key={index} className="result-item">
            {result}
          </div>
        ))}
      </div>
    </div>
  );
}
