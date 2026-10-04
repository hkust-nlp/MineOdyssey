package com.mcagent.bridge;

import com.google.gson.Gson;
import com.mcagent.bridge.api.*;
import com.mcagent.bridge.util.BridgeLogger;
import com.mcagent.bridge.util.BaritoneHelper;
import fi.iki.elonen.NanoHTTPD;

import java.io.IOException;
import java.io.InputStream;
import java.util.HashMap;
import java.util.Map;
import java.util.Properties;

/**
 * HTTP Server for Agent Bridge
 *
 * Provides REST API endpoints for Python agents to control Minecraft
 */
public class BridgeServer extends NanoHTTPD {

    private final Gson gson;
    private static final Properties BUILD_PROPERTIES = loadBuildProperties();

    public BridgeServer(int port) throws IOException {
        super(port);
        this.gson = new Gson();
        BridgeLogger.info("BridgeServer initialized on port " + port);
    }

    @Override
    public Response serve(IHTTPSession session) {
        String uri = session.getUri();
        Method method = session.getMethod();
        Map<String, String> params = new HashMap<>();

        try {
            session.parseBody(params);
        } catch (Exception e) {
            // Ignore parse errors for GET requests
        }

        BridgeLogger.debug("Request: " + method + " " + uri);

        try {
            // Parse query parameters
            Map<String, String> queryParams = session.getParms();

            // Route to appropriate handler
            if (uri.equals("/api/health")) {
                return handleHealthCheck();
            }
            else if (uri.startsWith("/api/input/")) {
                return handleInput(uri, queryParams);
            }
            else if (uri.equals("/api/look")) {
                return handleLook(queryParams);
            }
            else if (uri.equals("/api/state")) {
                return handleState();
            }
            else if (uri.equals("/api/right_click_block")) {
                return handleRightClickBlock(queryParams);
            }
            else if (uri.equals("/api/right_click")) {
                return handleRightClick();
            }
            else if (uri.equals("/api/window_click")) {
                return handleWindowClick(queryParams);
            }
            else if (uri.equals("/api/close_gui")) {
                return handleCloseGui();
            }
            // Unknown endpoint
            return newErrorResponse(404, "Endpoint not found: " + uri);

        } catch (Exception e) {
            BridgeLogger.error("Error handling request: " + e.getMessage());
            e.printStackTrace();
            return newErrorResponse(500, "Internal server error: " + e.getMessage());
        }
    }

    /**
     * Health check endpoint
     * GET /api/health
     */
    private Response handleHealthCheck() {
        Map<String, Object> response = new HashMap<>();
        response.put("success", true);
        response.put("status", "healthy");
        response.put("mod_version", "1.0.0");
        response.put(
                "minecraft_version",
                BUILD_PROPERTIES.getProperty("minecraft_version", "unknown")
        );
        response.put(
                "neoforge_version",
                BUILD_PROPERTIES.getProperty("neoforge_version", "unknown")
        );
        response.put("baritone_available", BaritoneHelper.isAvailable());
        response.put("input_backend", "minecraft_key_mapping");
        response.put(
                "coordinate_lock_enabled",
                CoordinateExposureLock.isEnabled()
        );
        response.put(
                "coordinate_lock_policy_version",
                CoordinateExposureLock.getPolicyVersion()
        );
        response.put(
                "coordinate_lock_blocked_screen_count",
                CoordinateExposureLock.getBlockedScreenCount()
        );
        response.put(
                "coordinate_lock_blocked_chat_count",
                CoordinateExposureLock.getBlockedChatCount()
        );

        String json = gson.toJson(response);
        return newFixedLengthResponse(Response.Status.OK, "application/json", json);
    }

    private static Properties loadBuildProperties() {
        Properties properties = new Properties();
        try (InputStream stream = BridgeServer.class.getResourceAsStream(
                "/agentbridge-build.properties"
        )) {
            if (stream != null) {
                properties.load(stream);
            }
        } catch (IOException error) {
            BridgeLogger.warn("Cannot load AgentBridge build properties: " + error.getMessage());
        }
        return properties;
    }

    /**
     * Input control endpoint
     * GET /api/input/{type}/{state}
     */
    private Response handleInput(String uri, Map<String, String> params) {
        // Parse URI: /api/input/{type}/{state}
        String[] parts = uri.split("/");
        if (parts.length < 5) {
            return newErrorResponse(400, "Invalid input URI format. Expected: /api/input/{type}/{state}");
        }

        String inputType = parts[3];
        String stateStr = parts[4];
        boolean state = "true".equalsIgnoreCase(stateStr);

        Map<String, Object> response = InputHandler.setInputState(inputType, state);
        String json = gson.toJson(response);
        return newFixedLengthResponse(Response.Status.OK, "application/json", json);
    }

    /**
     * Look control endpoint
     * GET /api/look?yaw={yaw}&pitch={pitch}[&interact={true|false}]
     */
    private Response handleLook(Map<String, String> params) {
        try {
            String yawStr = params.get("yaw");
            String pitchStr = params.get("pitch");

            if (yawStr == null || pitchStr == null) {
                return newErrorResponse(400, "Missing required parameters: yaw and pitch");
            }

            float yaw = Float.parseFloat(yawStr);
            float pitch = Float.parseFloat(pitchStr);
            boolean interact = "true".equalsIgnoreCase(params.getOrDefault("interact", "false"));

            Map<String, Object> response = LookHandler.setLookDirection(yaw, pitch, interact);
            String json = gson.toJson(response);
            return newFixedLengthResponse(Response.Status.OK, "application/json", json);

        } catch (NumberFormatException e) {
            return newErrorResponse(400, "Invalid number format for yaw or pitch");
        }
    }

    /**
     * State query endpoint
     * GET /api/state
     */
    private Response handleState() {
        Map<String, Object> response = StateHandler.getState();
        String json = gson.toJson(response);
        return newFixedLengthResponse(Response.Status.OK, "application/json", json);
    }

    /**
     * Right-click block endpoint
     * GET /api/right_click_block?x={x}&y={y}&z={z}
     */
    private Response handleRightClickBlock(Map<String, String> params) {
        try {
            String xStr = params.get("x");
            String yStr = params.get("y");
            String zStr = params.get("z");

            if (xStr == null || yStr == null || zStr == null) {
                return newErrorResponse(400, "Missing required parameters: x, y, z");
            }

            int x = Integer.parseInt(xStr);
            int y = Integer.parseInt(yStr);
            int z = Integer.parseInt(zStr);

            Map<String, Object> response = BlockHandler.rightClickBlock(x, y, z);
            String json = gson.toJson(response);
            return newFixedLengthResponse(Response.Status.OK, "application/json", json);

        } catch (NumberFormatException e) {
            return newErrorResponse(400, "Invalid number format for coordinates");
        }
    }

    /**
     * Right-click (use item) endpoint
     * GET /api/right_click
     */
    private Response handleRightClick() {
        Map<String, Object> response = BlockHandler.rightClick();
        String json = gson.toJson(response);
        return newFixedLengthResponse(Response.Status.OK, "application/json", json);
    }

    /**
     * Window click endpoint
     * GET /api/window_click?window={id}&slot={slot}&button={btn}&type={type}
     */
    private Response handleWindowClick(Map<String, String> params) {
        try {
            String windowStr = params.get("window");
            String slotStr = params.get("slot");
            String buttonStr = params.get("button");
            String clickType = params.getOrDefault("type", "PICKUP");

            if (windowStr == null || slotStr == null || buttonStr == null) {
                return newErrorResponse(400, "Missing required parameters: window, slot, button");
            }

            int window = Integer.parseInt(windowStr);
            int slot = Integer.parseInt(slotStr);
            int button = Integer.parseInt(buttonStr);

            Map<String, Object> response = GUIHandler.windowClick(window, slot, button, clickType);
            String json = gson.toJson(response);
            return newFixedLengthResponse(Response.Status.OK, "application/json", json);

        } catch (NumberFormatException e) {
            return newErrorResponse(400, "Invalid number format for window, slot, or button");
        }
    }

    /**
     * Close GUI endpoint
     * GET /api/close_gui
     */
    private Response handleCloseGui() {
        Map<String, Object> response = GUIHandler.closeGui();
        String json = gson.toJson(response);
        return newFixedLengthResponse(Response.Status.OK, "application/json", json);
    }

    /**
     * Helper: Create error response
     */
    private Response newErrorResponse(int statusCode, String message) {
        Map<String, Object> response = new HashMap<>();
        response.put("success", false);
        response.put("error", message);

        String json = gson.toJson(response);
        Response.Status status = Response.Status.lookup(statusCode);
        return newFixedLengthResponse(status, "application/json", json);
    }
}
