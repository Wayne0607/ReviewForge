import React, { useState, useMemo } from 'react';

interface Column {
  key: string;
  label: string;
  sortable?: boolean;
}

interface DataTableProps {
  data: any[];
  columns?: Column[];
}

export function DataTable({ data, columns }: DataTableProps): JSX.Element {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');
  const [filter, setFilter] = useState('');
  const [page, setPage] = useState(0);
  const pageSize = 10;

  const defaultColumns: Column[] = [
    { key: 'id', label: 'ID', sortable: true },
    { key: 'message', label: 'Message', sortable: true },
    { key: 'read', label: 'Status', sortable: true },
    { key: 'timestamp', label: 'Time', sortable: true },
  ];

  const cols = columns || defaultColumns;

  const filteredData = useMemo(() => {
    let result = [...data];

    if (filter) {
      result = result.filter(item =>
        Object.values(item).some(val =>
          String(val).toLowerCase().includes(filter.toLowerCase())
        )
      );
    }

    if (sortKey) {
      result.sort((a, b) => {
        const aVal = a[sortKey];
        const bVal = b[sortKey];
        const cmp = aVal < bVal ? -1 : aVal > bVal ? 1 : 0;
        return sortDir === 'asc' ? cmp : -cmp;
      });
    }

    return result;
  }, [data, filter, sortKey, sortDir]);

  const paginatedData = useMemo(() => {
    const start = page * pageSize;
    return filteredData.slice(start, start + pageSize);
  }, [filteredData, page]);

  function handleSort(key: string) {
    if (sortKey === key) {
      setSortDir(prev => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }

  // BUG: This component is 300+ lines — excessive complexity, should be split
  function renderHeader() {
    return (
      <thead>
        <tr>
          {cols.map(col => (
            <th
              key={col.key}
              onClick={col.sortable ? () => handleSort(col.key) : undefined}
              style={{ cursor: col.sortable ? 'pointer' : 'default' }}
              // BUG: Missing aria-sort attribute for sortable columns
            >
              {col.label}
              {sortKey === col.key && (sortDir === 'asc' ? ' ▲' : ' ▼')}
            </th>
          ))}
          <th>Actions</th>
        </tr>
      </thead>
    );
  }

  function renderBody() {
    return (
      <tbody>
        {paginatedData.map((row, index) => (
          <tr key={row.id || index}>
            {cols.map(col => (
              <td key={col.key}>{renderCell(row, col.key)}</td>
            ))}
            <td>{renderActions(row)}</td>
          </tr>
        ))}
        {paginatedData.length === 0 && (
          <tr>
            <td colSpan={cols.length + 1} className="empty-state">
              No data available
            </td>
          </tr>
        )}
      </tbody>
    );
  }

  function renderCell(row: any, key: string) {
    const value = row[key];
    if (key === 'read') {
      return (
        <span className={`status ${value ? 'read' : 'unread'}`}>
          {value ? '✓ Read' : '● Unread'}
        </span>
      );
    }
    if (key === 'timestamp') {
      return new Date(value).toLocaleString();
    }
    return String(value ?? '');
  }

  function renderActions(row: any) {
    return (
      <div className="action-buttons">
        <button onClick={() => handleView(row)} title="View details">
          View
        </button>
        <button onClick={() => handleDelete(row)} title="Delete item">
          Delete
        </button>
        <button onClick={() => handleExport(row)} title="Export item">
          Export
        </button>
      </div>
    );
  }

  function handleView(row: any) {
    console.log('Viewing:', row);
  }

  function handleDelete(row: any) {
    if (confirm('Are you sure you want to delete this item?')) {
      console.log('Deleting:', row);
    }
  }

  function handleExport(row: any) {
    const blob = new Blob([JSON.stringify(row, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `export-${row.id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function renderPagination() {
    const totalPages = Math.ceil(filteredData.length / pageSize);
    return (
      <div className="pagination">
        <button
          disabled={page === 0}
          onClick={() => setPage(p => p - 1)}
        >
          Previous
        </button>
        <span className="page-info">
          Page {page + 1} of {totalPages}
        </span>
        <button
          disabled={page >= totalPages - 1}
          onClick={() => setPage(p => p + 1)}
        >
          Next
        </button>
      </div>
    );
  }

  function renderFilter() {
    return (
      <div className="table-filter">
        {/* BUG: Input without label — accessibility issue */}
        <input
          type="text"
          value={filter}
          onChange={e => {
            setFilter(e.target.value);
            setPage(0);
          }}
          placeholder="Filter items..."
        />
        {filter && (
          <button onClick={() => setFilter('')} className="clear-filter">
            Clear
          </button>
        )}
      </div>
    );
  }

  function renderSummary() {
    return (
      <div className="table-summary">
        Showing {paginatedData.length} of {filteredData.length} items
        {filter && ` (filtered from ${data.length} total)`}
      </div>
    );
  }

  return (
    <div className="data-table">
      {renderFilter()}
      {renderSummary()}
      <table role="grid">
        {renderHeader()}
        {renderBody()}
      </table>
      {renderPagination()}
    </div>
  );
}
