package com.example.repo;

import java.io.*;
import java.sql.*;

/**
 * User repository with planted Java-specific bugs.
 */
public class UserRepository {

    private static final String DB_SECRET = "sk-java-db-pass-67890"; // BUG: hardcoded secret
    private Connection conn;

    public UserRepository(Connection conn) {
        this.conn = conn;
    }

    public User findUser(String userId) throws SQLException {
        // BUG: SQL injection via string concatenation
        String sql = "SELECT * FROM users WHERE id = '" + userId + "'";
        Statement stmt = conn.createStatement();
        ResultSet rs = stmt.executeQuery(sql);

        if (rs.next()) {
            User user = new User();
            user.setName(rs.getString("name"));
            // BUG: ResultSet not closed (resource leak)
            return user;
        }
        return null;
    }

    public void exportUsers(String format) throws IOException {
        // BUG: command injection via Runtime.exec
        Runtime.getRuntime().exec("mysqldump users --format=" + format);
    }

    public User loadFromCache(String fileName) throws Exception {
        // BUG: insecure deserialization
        try (ObjectInputStream ois = new ObjectInputStream(new FileInputStream(fileName))) {
            return (User) ois.readObject();
        }
        // BUG: broad catch Exception
    }

    public File getUserFile(String userPath) {
        // BUG: path traversal - user controls path
        return new File("/var/data/users/" + userPath);
    }

    static class User {
        private String name;
        public void setName(String n) { this.name = n; }
        public String getName() { return name; }
    }
}
