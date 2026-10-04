package com.mcagent.bridge.api;

import com.mcagent.bridge.util.BridgeLogger;
import com.mcagent.bridge.util.ClientThread;
import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;

import java.util.HashMap;
import java.util.Map;

/** Direct player rotation control on Minecraft's client thread. */
public final class LookHandler {
    private LookHandler() {
    }

    public static Map<String, Object> setLookDirection(
            float yaw,
            float pitch,
            boolean blockInteract
    ) {
        Map<String, Object> response = new HashMap<>();
        if (pitch < -90 || pitch > 90) {
            response.put("success", false);
            response.put("error", "Pitch must be between -90 and 90");
            return response;
        }
        try {
            ClientThread.call(() -> {
                LocalPlayer player = Minecraft.getInstance().player;
                if (player == null) {
                    throw new IllegalStateException("Player not available");
                }
                player.setYRot(yaw);
                player.setYHeadRot(yaw);
                player.setXRot(pitch);
                return null;
            });
            response.put("success", true);
            response.put("message", "Look direction updated");
            response.put("yaw", yaw);
            response.put("pitch", pitch);
            response.put("block_interact", blockInteract);
            response.put("backend", "minecraft_player_rotation");
        } catch (Exception error) {
            BridgeLogger.error("Error setting look direction: " + error.getMessage());
            response.put("success", false);
            response.put("error", error.getMessage());
        }
        return response;
    }
}
