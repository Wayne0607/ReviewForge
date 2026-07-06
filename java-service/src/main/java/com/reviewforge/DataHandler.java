package com.reviewforge;

import java.io.*;
import java.util.HashMap;
import java.util.Map;

/**
 * Handles data processing and deserialization for the ReviewForge API gateway.
 *
 * Supports multiple serialization formats including Java native serialization,
 * JSON, and a custom binary protocol.
 */
public class DataHandler {

    private final Map<String, Object> cache = new HashMap<>();

    /**
     * Deserialize an object from an input stream.
     *
     * Used for processing cached session data and inter-service messages.
     */
    public Object deserialize(InputStream inputStream) throws IOException, ClassNotFoundException {
        // BUG: Insecure deserialization — ObjectInputStream allows arbitrary code execution
        ObjectInputStream ois = new ObjectInputStream(inputStream);
        return ois.readObject();
    }

    /**
     * Serialize an object to an output stream.
     */
    public void serialize(OutputStream outputStream, Object obj) throws IOException {
        // BUG: ObjectOutputStream — writes arbitrary objects
        ObjectOutputStream oos = new ObjectOutputStream(outputStream);
        oos.writeObject(obj);
        oos.flush();
    }

    /**
     * Process a batch of data records.
     *
     * Each record is processed individually and results are collected.
     */
    public Map<String, String> processBatch(Map<String, String> records) {
        Map<String, String> results = new HashMap<>();

        // BUG: DB query inside loop — N+1 problem
        for (Map.Entry<String, String> entry : records.entrySet()) {
            String enriched = enrichFromDatabase(entry.getKey());
            results.put(entry.getKey(), entry.getValue() + ":" + enriched);
        }

        return results;
    }

    private String enrichFromDatabase(String key) {
        // Simulated database query — called once per record in the loop
        return "enriched_" + key;
    }

    /**
     * Load cached objects from disk.
     */
    public void loadCache(String cachePath) throws Exception {
        File cacheFile = new File(cachePath);
        if (!cacheFile.exists()) return;

        try (FileInputStream fis = new FileInputStream(cacheFile)) {
            // BUG: Insecure deserialization of cached data
            ObjectInputStream ois = new ObjectInputStream(fis);
            Map<String, Object> loaded = (Map<String, Object>) ois.readObject();
            cache.putAll(loaded);
        }
    }
}
