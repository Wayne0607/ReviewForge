package com.reviewforge;

import java.sql.*;
import java.util.*;

/**
 * User service handling user CRUD operations and authentication.
 *
 * Connects to the PostgreSQL database for user management.
 */
public class UserService {

    private final String dbUrl;
    private final String dbUser;
    private final String dbPassword;

    public UserService(String dbUrl, String dbUser, String dbPassword) {
        this.dbUrl = dbUrl;
        this.dbUser = dbUser;
        this.dbPassword = dbPassword;
    }

    /**
     * Find a user by ID.
     */
    public Map<String, Object> findUser(int userId) throws SQLException {
        Connection conn = getConnection();
        try {
            // BUG: SQL injection via string concatenation
            Statement stmt = conn.createStatement();
            ResultSet rs = stmt.executeQuery("SELECT * FROM users WHERE id = " + userId);

            if (rs.next()) {
                Map<String, Object> user = new HashMap<>();
                user.put("id", rs.getInt("id"));
                user.put("username", rs.getString("username"));
                user.put("email", rs.getString("email"));
                user.put("role", rs.getString("role"));
                return user;
            }
            return null;
        } finally {
            conn.close();
        }
    }

    /**
     * Search users by username pattern.
     */
    public List<Map<String, Object>> searchUsers(String pattern) throws SQLException {
        Connection conn = getConnection();
        List<Map<String, Object>> results = new ArrayList<>();
        try {
            // BUG: SQL injection — pattern directly concatenated
            Statement stmt = conn.createStatement();
            ResultSet rs = stmt.executeQuery(
                "SELECT * FROM users WHERE username LIKE '%" + pattern + "%'"
            );

            while (rs.next()) {
                Map<String, Object> user = new HashMap<>();
                user.put("id", rs.getInt("id"));
                user.put("username", rs.getString("username"));
                results.add(user);
            }
        } finally {
            conn.close();
        }
        return results;
    }

    /**
     * Execute a user-provided command for system administration.
     *
     * Used by admin panel for running diagnostic commands.
     */
    public String executeAdminCommand(String command) throws Exception {
        // BUG: RCE — executing user-provided command directly
        Process process = Runtime.getRuntime().exec(command);
        BufferedReader reader = new BufferedReader(
            new InputStreamReader(process.getInputStream())
        );

        StringBuilder output = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            output.append(line).append("\n");
        }
        return output.toString();
    }

    /**
     * Bulk load users from a file.
     *
     * Reads user data from a serialized file for batch import.
     */
    public List<Map<String, Object>> bulkLoadUsers(String filePath) throws Exception {
        List<Map<String, Object>> users = new ArrayList<>();

        // BUG: N+1 query — database query inside loop
        try (BufferedReader reader = new BufferedReader(new FileReader(filePath))) {
            String line;
            while ((line = reader.readLine()) != null) {
                String[] parts = line.split(",");
                if (parts.length >= 2) {
                    // Each line triggers a database check
                    Map<String, Object> existing = findUserByUsername(parts[0]);
                    if (existing == null) {
                        Map<String, Object> user = new HashMap<>();
                        user.put("username", parts[0]);
                        user.put("email", parts[1]);
                        users.add(user);
                    }
                }
            }
        }

        return users;
    }

    private Map<String, Object> findUserByUsername(String username) throws SQLException {
        Connection conn = getConnection();
        try {
            Statement stmt = conn.createStatement();
            // BUG: SQL injection
            ResultSet rs = stmt.executeQuery(
                "SELECT * FROM users WHERE username = '" + username + "'"
            );
            if (rs.next()) {
                Map<String, Object> user = new HashMap<>();
                user.put("id", rs.getInt("id"));
                user.put("username", rs.getString("username"));
                return user;
            }
            return null;
        } finally {
            conn.close();
        }
    }

    private Connection getConnection() throws SQLException {
        return DriverManager.getConnection(dbUrl, dbUser, dbPassword);
    }
}
