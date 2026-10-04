package com.mcagent.bridge.util;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Simple logger wrapper for Agent Bridge
 */
public class BridgeLogger {

    private static final Logger LOGGER = LoggerFactory.getLogger("AgentBridge");

    public static void info(String message) {
        LOGGER.info(message);
    }

    public static void warn(String message) {
        LOGGER.warn(message);
    }

    public static void error(String message) {
        LOGGER.error(message);
    }

    public static void debug(String message) {
        LOGGER.debug(message);
    }
}
