package gauntlet_services;

import gauntlet_fullstack.SeedJava;
import java.io.ByteArrayInputStream;
import java.io.ObjectInputStream;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

public class CrossPrConsumer {
    private final SeedJava seed = new SeedJava();

    public ResultSet lookupTenant(Connection conn, String tenant) throws Exception {
        return seed.runTenantQuery(conn, tenant);
    }

    public Object restore(byte[] body) throws Exception {
        return seed.restoreJob(body);
    }

    public Process startTool(String tool) throws Exception {
        return seed.launchTool(tool);
    }

    public ResultSet directSearch(Connection conn, String email) throws Exception {
        Statement stmt = conn.createStatement();
        return stmt.executeQuery("SELECT * FROM users WHERE email = '" + email + "'");
    }

    public Object directRestore(byte[] payload) throws Exception {
        ObjectInputStream input = new ObjectInputStream(new ByteArrayInputStream(payload));
        return input.readObject();
    }
}
