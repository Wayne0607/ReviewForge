package com.reviewforge;

import org.w3c.dom.*;
import javax.xml.parsers.*;
import javax.script.*;
import java.io.*;

/**
 * Parses XML configuration files and evaluates dynamic expressions.
 *
 * Used for loading service configuration and evaluating
 * template expressions in deployment configs.
 */
public class ConfigParser {

    private DocumentBuilderFactory factory;

    public ConfigParser() {
        // BUG: XXE — DocumentBuilderFactory not configured to disable external entities
        factory = DocumentBuilderFactory.newInstance();
        // Missing: factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        // Missing: factory.setFeature("http://xml.org/sax/features/external-general-entities", false);
        // Missing: factory.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
    }

    /**
     * Parse an XML configuration file.
     *
     * Supports external entity references for modular configuration.
     */
    public Document parseConfig(String xmlPath) throws Exception {
        DocumentBuilder builder = factory.newDocumentBuilder();
        // BUG: XXE — parsing untrusted XML with external entities enabled
        return builder.parse(new File(xmlPath));
    }

    /**
     * Parse XML configuration from a string.
     */
    public Document parseConfigString(String xml) throws Exception {
        DocumentBuilder builder = factory.newDocumentBuilder();
        return builder.parse(new java.io.ByteArrayInputStream(xml.getBytes()));
    }

    /**
     * Evaluate a template expression in the configuration context.
     *
     * Supports JavaScript expressions for dynamic configuration values.
     */
    public Object evaluateExpression(String expression) throws Exception {
        // BUG: ScriptEngine eval — arbitrary code execution via JavaScript
        ScriptEngineManager manager = new ScriptEngineManager();
        ScriptEngine engine = manager.getEngineByName("js");
        if (engine == null) {
            engine = manager.getEngineByName("javascript");
        }
        return engine.eval(expression);
    }

    /**
     * Evaluate a template with variable substitution.
     */
    public String evaluateTemplate(String template, java.util.Map<String, String> variables) throws Exception {
        String result = template;
        for (java.util.Map.Entry<String, String> entry : variables.entrySet()) {
            result = result.replace("${" + entry.getKey() + "}", entry.getValue());
        }

        // Check if there are remaining expressions to evaluate
        if (result.contains("${{")) {
            String expr = result.substring(result.indexOf("${{") + 3, result.indexOf("}}"));
            Object value = evaluateExpression(expr);
            result = result.replace("${{" + expr + "}}", String.valueOf(value));
        }

        return result;
    }

    /**
     * Get a configuration value by path (e.g., "server.port").
     */
    public String getValue(Document config, String path) {
        String[] parts = path.split("\\.");
        Node current = config.getDocumentElement();

        for (String part : parts) {
            NodeList children = current.getChildNodes();
            boolean found = false;
            for (int i = 0; i < children.getLength(); i++) {
                if (children.item(i).getNodeName().equals(part)) {
                    current = children.item(i);
                    found = true;
                    break;
                }
            }
            if (!found) return null;
        }

        return current.getTextContent();
    }
}
