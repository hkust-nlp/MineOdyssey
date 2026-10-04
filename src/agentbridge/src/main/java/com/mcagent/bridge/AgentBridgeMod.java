package com.mcagent.bridge;

import com.mcagent.bridge.util.BridgeLogger;
import net.minecraft.client.Minecraft;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.event.lifecycle.FMLClientSetupEvent;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.client.event.ClientTickEvent;

/**
 * Agent Bridge Mod - HTTP API bridge for AI agents
 *
 * Exposes direct Minecraft client input and state APIs over HTTP/JSON for
 * Python agents. Baritone is not required.
 */
@Mod("agentbridge")
public class AgentBridgeMod {

    private static final int DEFAULT_PORT = 8080;
    private static BridgeServer server;
    private static boolean initialized = false;

    public AgentBridgeMod(IEventBus modEventBus) {
        BridgeLogger.info("Agent Bridge Mod loading...");

        // Register client setup event
        modEventBus.addListener(this::onClientSetup);

        // Register tick event
        NeoForge.EVENT_BUS.addListener(this::onClientTick);
        NeoForge.EVENT_BUS.addListener(CoordinateExposureLock::onScreenOpening);
        NeoForge.EVENT_BUS.addListener(CoordinateExposureLock::onChatReceived);

        BridgeLogger.info("Agent Bridge Mod registered");
    }

    private void onClientSetup(FMLClientSetupEvent event) {
        BridgeLogger.info("Client setup event fired");
    }

    private void onClientTick(ClientTickEvent.Post event) {
        suppressUnsecureServerToast();

        // Initialize server on first tick (when game is fully loaded)
        if (!initialized) {
            initialized = true;
            startServer();
        }
    }

    private void suppressUnsecureServerToast() {
        String enabled = System.getenv("AGENTBRIDGE_SUPPRESS_UNSECURE_SERVER_TOAST");
        if (!"true".equalsIgnoreCase(enabled)) {
            return;
        }
        ToastSuppressor.suppressUnsecureServerWarning(Minecraft.getInstance());
    }

    private void startServer() {
        try {
            BridgeLogger.info("Starting HTTP server...");

            int port = resolvePort();
            server = new BridgeServer(port);
            server.start();

            BridgeLogger.info("✅ HTTP server started on port " + port);
            BridgeLogger.info("Agent Bridge is ready!");

        } catch (Exception e) {
            BridgeLogger.error("Failed to start HTTP server: " + e.getMessage());
            e.printStackTrace();
        }
    }

    private int resolvePort() {
        String[] rawCandidates = new String[] {
            System.getProperty("agentbridge.port"),
            System.getenv("AGENTBRIDGE_PORT")
        };

        for (String raw : rawCandidates) {
            if (raw == null) {
                continue;
            }
            String text = raw.trim();
            if (text.isEmpty()) {
                continue;
            }
            try {
                int parsed = Integer.parseInt(text);
                if (parsed >= 1 && parsed <= 65535) {
                    return parsed;
                }
                BridgeLogger.warn("Invalid agentbridge port out of range: " + text);
            } catch (NumberFormatException e) {
                BridgeLogger.warn("Invalid agentbridge port value: " + text);
            }
        }

        return DEFAULT_PORT;
    }

    public static void shutdown() {
        if (server != null) {
            BridgeLogger.info("Stopping HTTP server...");
            server.stop();
            server = null;
        }
    }
}
