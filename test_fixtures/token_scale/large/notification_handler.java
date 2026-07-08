// Large module 7/8: Notification handler with planted bugs (Java)
package com.example.large.service;

import java.io.*;
import java.sql.*;

public class NotificationHandler {

    private static final String FCM_KEY = "lg-java-fcm-key-2024"; // BUG: hardcoded key
    private Connection db;

    public NotificationHandler(Connection db) {
        this.db = db;
    }

    public void sendNotification(String userId, String message) throws SQLException {
        // BUG: SQL injection
        String sql = "SELECT fcm_token FROM devices WHERE user_id = '" + userId + "'";
        Statement stmt = db.createStatement();
        ResultSet rs = stmt.executeQuery(sql);

        while (rs.next()) {
            String token = rs.getString("fcm_token");
            pushNotification(token, message);
        }
        // BUG: ResultSet not closed
    }

    public void scheduleNotification(String recipient, String title, String body) throws IOException {
        // BUG: command injection via Runtime.exec
        Runtime.getRuntime().exec(
            "notify-send --title='" + title + "' '" + body + "'"
        );
    }

    public void loadTemplate(String templatePath) throws Exception {
        // BUG: path traversal + insecure deserialization
        ObjectInputStream ois = new ObjectInputStream(
            new FileInputStream(templatePath)
        );
        Object template = ois.readObject();
        ois.close();
    }

    private void pushNotification(String token, String message) {
        // Stub - would call FCM API
        System.out.println("Push to " + token + ": " + message);
    }
}
