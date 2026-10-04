package com.mcagent.bridge.api;

import com.mcagent.bridge.util.BridgeLogger;
import com.mcagent.bridge.util.ClientThread;
import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.world.phys.Vec3;

import java.util.HashMap;
import java.util.Map;

/** Direct client state query with precise and block positions. */
public final class StateHandler {
    private StateHandler() {
    }

    public static Map<String, Object> getState() {
        Map<String, Object> response = new HashMap<>();
        try {
            Map<String, Object> data = ClientThread.call(() -> {
                Minecraft minecraft = Minecraft.getInstance();
                LocalPlayer player = minecraft.player;
                if (player == null || minecraft.gameMode == null) {
                    throw new IllegalStateException("Player or game mode not available");
                }
                Map<String, Object> state = new HashMap<>();
                Vec3 precise = player.position();
                state.put("position", Map.of(
                        "x", precise.x,
                        "y", precise.y,
                        "z", precise.z
                ));
                BlockPos block = player.blockPosition();
                state.put("block_position", Map.of(
                        "x", block.getX(),
                        "y", block.getY(),
                        "z", block.getZ()
                ));
                state.put("rotation", Map.of(
                        "yaw", player.getYRot(),
                        "pitch", player.getXRot()
                ));
                state.put("health", player.getHealth());
                state.put("max_health", player.getMaxHealth());
                state.put("food", player.getFoodData().getFoodLevel());
                state.put("gamemode", minecraft.gameMode.getPlayerMode().name());
                Vec3 motion = player.getDeltaMovement();
                state.put("motion", Map.of(
                        "x", motion.x,
                        "y", motion.y,
                        "z", motion.z
                ));
                state.put("on_ground", player.onGround());
                state.put(
                        "container_id",
                        player.containerMenu == null ? 0 : player.containerMenu.containerId
                );
                state.put("gui_open", minecraft.screen != null);
                return state;
            });
            response.put("success", true);
            response.put("data", data);
        } catch (Exception error) {
            BridgeLogger.error("Error getting state: " + error.getMessage());
            response.put("success", false);
            response.put("error", error.getMessage());
        }
        return response;
    }
}
