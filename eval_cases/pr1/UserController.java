package com.example.controller;

import java.io.*;
import java.sql.Connection;
import java.sql.Statement;
import java.util.Optional;

public class UserController {

    private Connection dbConnection;

    public UserController(Connection dbConnection) {
        this.dbConnection = dbConnection;
    }

    public void deleteUser(String userId) {
        // BUG: SQL injection via string concatenation
        String query = "DELETE FROM users WHERE id = " + userId;
        try {
            Statement stmt = dbConnection.createStatement();
            stmt.executeUpdate(query);
            stmt.close();
        } catch (Exception e) {
            // BUG: exception swallowed silently
        }
    }

    public String readFile(String userPath) throws IOException {
        // BUG: path traversal — user input in file path
        File file = new File("/data", userPath);
        BufferedReader reader = null;
        try {
            reader = new BufferedReader(new FileReader(file));
            return reader.readLine();
        } catch (IOException e) {
            throw e;
        } finally {
            // BUG: reader not properly closed; should use try-with-resources
            if (reader != null) {
                reader.close();
            }
        }
    }

    // BUG: Optional as method parameter (anti-pattern)
    public String getUserName(Optional<String> userId) {
        // BUG: Optional.get() without isPresent check
        return "User: " + userId.get();
    }

    // BUG: hardcoded credential
    private static final String DB_PASSWORD = "admin123!";

    // BUG: Runtime.exec with user input
    public void runBackup(String backupPath) throws IOException {
        Runtime.getRuntime().exec("tar -czf " + backupPath + " /data/backup");
    }

    // STYLE: method name doesn't follow Java convention
    public String get_user_data() {
        return "data";
    }
}
