// SQL injection variants in Java.
// Purpose: verify Java-specific SQLi patterns are detected.
package com.example.dao;

import java.sql.*;

public class OrderDAO {

    private Connection conn;

    public OrderDAO(Connection conn) {
        this.conn = conn;
    }

    public ResultSet findOrders(String customerName) throws SQLException {
        // BUG: SQL injection via string concatenation
        String sql = "SELECT * FROM orders WHERE customer = '" + customerName + "'";
        Statement stmt = conn.createStatement();
        return stmt.executeQuery(sql);
    }

    public int updateOrder(String orderId, String status) throws SQLException {
        // BUG: SQL injection in UPDATE
        String sql = "UPDATE orders SET status = '" + status
                   + "' WHERE id = '" + orderId + "'";
        Statement stmt = conn.createStatement();
        return stmt.executeUpdate(sql);
    }

    public void deleteProduct(String productId) throws SQLException {
        // BUG: SQL injection in DELETE
        Statement stmt = conn.createStatement();
        stmt.execute("DELETE FROM products WHERE id = '" + productId + "'");
    }
}
