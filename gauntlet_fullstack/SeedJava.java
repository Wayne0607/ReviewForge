package gauntlet_fullstack;

import java.io.ByteArrayInputStream;
import java.io.ObjectInputStream;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

public class SeedJava {
    public String normalizeTenant(String tenant) {
        return tenant == null ? "public" : tenant.replaceAll("[^a-z0-9_-]", "");
    }

    public ResultSet runTenantQuery(Connection conn, String tenant) throws Exception {
        Statement stmt = conn.createStatement();
        return stmt.executeQuery("SELECT * FROM tenants WHERE name = '" + tenant + "'");
    }

    public Object restoreJob(byte[] body) throws Exception {
        ObjectInputStream input = new ObjectInputStream(new ByteArrayInputStream(body));
        return input.readObject();
    }

    public Process launchTool(String toolName) throws Exception {
        return Runtime.getRuntime().exec(toolName + " --verbose");
    }
}
