package com.mcagent.bridge.api;

import com.mcagent.bridge.util.BridgeLogger;
import com.mcagent.bridge.util.ClientThread;
import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.world.inventory.ClickType;

import java.util.HashMap;
import java.util.Map;

/** Direct inventory interaction on Minecraft's client thread. */
public final class GUIHandler {
    private GUIHandler() {
    }

    public static Map<String, Object> windowClick(
            int windowId,
            int slotId,
            int button,
            String clickTypeText
    ) {
        Map<String, Object> response = new HashMap<>();
        final ClickType clickType;
        try {
            clickType = ClickType.valueOf(
                    clickTypeText == null ? "PICKUP" : clickTypeText.toUpperCase()
            );
        } catch (IllegalArgumentException error) {
            response.put("success", false);
            response.put("error", "Invalid click type: " + clickTypeText);
            return response;
        }
        try {
            ClientThread.call(() -> {
                Minecraft minecraft = Minecraft.getInstance();
                LocalPlayer player = minecraft.player;
                if (player == null || minecraft.gameMode == null) {
                    throw new IllegalStateException("Player or game mode not available");
                }
                minecraft.gameMode.handleInventoryMouseClick(
                        windowId,
                        slotId,
                        button,
                        clickType,
                        player
                );
                return null;
            });
            response.put("success", true);
            response.put("message", "Window clicked");
            response.put("window", windowId);
            response.put("slot", slotId);
            response.put("button", button);
            response.put("click_type", clickType.name());
        } catch (Exception error) {
            BridgeLogger.error("Error clicking window: " + error.getMessage());
            response.put("success", false);
            response.put("error", error.getMessage());
        }
        return response;
    }

    public static Map<String, Object> closeGui() {
        Map<String, Object> response = new HashMap<>();
        try {
            ClientThread.call(() -> {
                LocalPlayer player = Minecraft.getInstance().player;
                if (player == null) {
                    throw new IllegalStateException("Player not available");
                }
                player.closeContainer();
                return null;
            });
            response.put("success", true);
            response.put("message", "Container closed");
        } catch (Exception error) {
            BridgeLogger.error("Error closing container: " + error.getMessage());
            response.put("success", false);
            response.put("error", error.getMessage());
        }
        return response;
    }
}
