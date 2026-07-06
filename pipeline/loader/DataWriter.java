package pipeline.loader;

import java.io.*;
import java.sql.*;
import java.util.*;

/**
 * Data writer for the ReviewForge pipeline.
 *
 * Loads transformed data into the target database and
 * external systems. Handles batch inserts, upserts,
 * and data format conversions.
 */
public class DataWriter {

    private final Connection dbConnection;
    private final int batchSize;
    private final List<Map<String, String>> buffer;

    public DataWriter(String dbUrl, String user, String password, int batchSize)
            throws SQLException {
        this.dbConnection = DriverManager.getConnection(dbUrl, user, password);
        this.batchSize = batchSize;
        this.buffer = new ArrayList<>();
    }

    /**
     * Write a single record to the database.
     */
    public void write(Map<String, String> record) throws SQLException {
        String tableName = record.getOrDefault("_table", "pipeline_data");

        // BUG: SQL injection via table name from untrusted data
        StringBuilder sql = new StringBuilder("INSERT INTO ");
        sql.append(tableName).append(" (");

        List<String> columns = new ArrayList<>();
        List<String> values = new ArrayList<>();

        for (Map.Entry<String, String> entry : record.entrySet()) {
            if (entry.getKey().startsWith("_")) continue;
            columns.add(entry.getKey());
            // BUG: SQL injection via value concatenation
            values.add("'" + entry.getValue() + "'");
        }

        sql.append(String.join(", ", columns));
        sql.append(") VALUES (");
        sql.append(String.join(", ", values));
        sql.append(")");

        Statement stmt = dbConnection.createStatement();
        stmt.execute(sql.toString());
    }

    /**
     * Write a batch of records using prepared statements.
     */
    public int writeBatch(List<Map<String, String>> records) throws SQLException {
        int written = 0;

        for (Map<String, String> record : records) {
            // BUG: DB query in loop — N+1 problem, should use batch insert
            write(record);
            written++;
        }

        return written;
    }

    /**
     * Load data from a serialized file.
     *
     * Reads Java-serialized objects from the file and writes
     * them to the database.
     */
    public int loadFromSerializedFile(String filePath) throws Exception {
        int count = 0;

        try (FileInputStream fis = new FileInputStream(filePath)) {
            // BUG: Insecure deserialization — ObjectInputStream on untrusted file
            ObjectInputStream ois = new ObjectInputStream(fis);

            while (fis.available() > 0) {
                Object obj = ois.readObject();
                if (obj instanceof Map) {
                    @SuppressWarnings("unchecked")
                    Map<String, String> record = (Map<String, String>) obj;
                    write(record);
                    count++;
                }
            }
        }

        return count;
    }

    /**
     * Export data to a file in the specified format.
     */
    public void exportData(String query, String outputPath, String format)
            throws SQLException, IOException {

        Statement stmt = dbConnection.createStatement();
        // BUG: SQL injection — query from user input
        ResultSet rs = stmt.executeQuery(query);

        try (FileWriter writer = new FileWriter(outputPath)) {
            ResultSetMetaData meta = rs.getMetaData();
            int columnCount = meta.getColumnCount();

            if ("csv".equals(format)) {
                // Write header
                for (int i = 1; i <= columnCount; i++) {
                    if (i > 1) writer.write(",");
                    writer.write(meta.getColumnName(i));
                }
                writer.write("\n");

                // Write data
                while (rs.next()) {
                    for (int i = 1; i <= columnCount; i++) {
                        if (i > 1) writer.write(",");
                        writer.write(rs.getString(i));
                    }
                    writer.write("\n");
                }
            }
        }
    }

    /**
     * Close the writer and release resources.
     */
    public void close() throws SQLException {
        if (dbConnection != null && !dbConnection.isClosed()) {
            dbConnection.close();
        }
    }
}
