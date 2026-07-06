<template>
  <div class="admin-panel">
    <h2>Admin Panel</h2>

    <div class="admin-section">
      <h3>User Management</h3>
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Username</th>
            <th>Role</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="user in users" :key="user.id">
            <td>{{ user.id }}</td>
            <td>{{ user.username }}</td>
            <td>{{ user.role }}</td>
            <td>{{ user.active ? 'Active' : 'Inactive' }}</td>
            <td>
              <button @click="toggleUser(user)">
                {{ user.active ? 'Deactivate' : 'Activate' }}
              </button>
              <button @click="deleteUser(user)" class="danger">Delete</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="admin-section">
      <h3>System Configuration</h3>
      <form @submit.prevent="updateConfig">
        <div class="form-group">
          <label>API Rate Limit:</label>
          <input v-model.number="config.rateLimit" type="number" />
        </div>
        <div class="form-group">
          <label>Max File Size (MB):</label>
          <input v-model.number="config.maxFileSize" type="number" />
        </div>
        <div class="form-group">
          <label>Debug Mode:</label>
          <input v-model="config.debug" type="checkbox" />
        </div>
        <button type="submit">Save Configuration</button>
      </form>
    </div>

    <div class="admin-section">
      <h3>Bulk Operations</h3>
      <button @click="exportAllUsers">Export All Users</button>
      <button @click="runCleanup">Run Cleanup Job</button>
    </div>
  </div>
</template>

<script>
export default {
  name: 'AdminPanel',

  data() {
    return {
      users: [],
      config: {
        rateLimit: 100,
        maxFileSize: 10,
        debug: false,
      },
    };
  },

  mounted() {
    this.fetchUsers();
    this.fetchConfig();
  },

  methods: {
    async fetchUsers() {
      const response = await fetch('/api/admin/users');
      this.users = await response.json();
    },

    async fetchConfig() {
      const response = await fetch('/api/admin/config');
      this.config = await response.json();
    },

    async toggleUser(user) {
      // BUG: No CSRF token in state-changing request
      await fetch(`/api/admin/users/${user.id}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      user.active = !user.active;
    },

    async deleteUser(user) {
      if (!confirm(`Delete user ${user.username}?`)) return;

      await fetch(`/api/admin/users/${user.id}`, { method: 'DELETE' });
      this.users = this.users.filter(u => u.id !== user.id);
    },

    async updateConfig() {
      // BUG: No CSRF token
      await fetch('/api/admin/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(this.config),
      });
    },

    async exportAllUsers() {
      const response = await fetch('/api/admin/users/export');
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'users-export.csv';
      a.click();
    },

    async runCleanup() {
      // BUG: No CSRF token, no confirmation
      await fetch('/api/admin/cleanup', { method: 'POST' });
      alert('Cleanup job started');
    },
  },
};
</script>

<style scoped>
.admin-panel {
  padding: 24px;
}
.admin-section {
  margin-bottom: 32px;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th, td {
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid #ddd;
}
.danger {
  color: #dc3545;
}
.form-group {
  margin-bottom: 12px;
}
.form-group label {
  display: block;
  margin-bottom: 4px;
}
</style>
