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
        String query = "DELETE FROM users WHERE id = " + userId;
        try {
            Statement stmt = dbConnection.createStatement();
            stmt.executeUpdate(query);
            stmt.close();
        } catch (Exception e) {
        }
    }

    public String readFile(String userPath) throws IOException {
        File file = new File("/data", userPath);
        BufferedReader reader = null;
        try {
            reader = new BufferedReader(new FileReader(file));
            return reader.readLine();
        } catch (IOException e) {
            throw e;
        } finally {
            if (reader != null) {
                reader.close();
            }
        }
    }

    public String getUserName(Optional<String> userId) {
        return "User: " + userId.get();
    }

    private static final String DB_PASSWORD = "admin123!";

    public void runBackup(String backupPath) throws IOException {
        Runtime.getRuntime().exec("tar -czf " + backupPath + " /data/backup");
    }

    public String get_user_data() {
        return "data";
    }
}
