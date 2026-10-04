package com.mcagent.bridge.api;

import com.mcagent.bridge.util.BridgeLogger;
import com.mcagent.bridge.util.ClientThread;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Client-thread-safe key control with no Baritone dependency. */
public final class InputHandler {
    private static final List<String> SUPPORTED_INPUTS = List.of(
            "MOVE_FORWARD",
            "MOVE_BACK",
            "MOVE_LEFT",
            "MOVE_RIGHT",
            "JUMP",
            "SNEAK",
            "SPRINT",
            "CLICK_LEFT",
            "CLICK_RIGHT"
    );

    private InputHandler() {
    }

    public static Map<String, Object> setInputState(String inputType, boolean state) {
        Map<String, Object> response = new LinkedHashMap<>();
        String input = parseInputType(inputType);
        if (input == null) {
            response.put("success", false);
            response.put("error", "Invalid input type: " + inputType);
            return response;
        }
        try {
            ClientThread.call(() -> {
                keyMapping(input).setDown(state);
                return null;
            });
            BridgeLogger.debug("Set input " + input + " to " + state);
            response.put("success", true);
            response.put("message", "Input state updated");
            response.put("input", input);
            response.put("state", state);
            response.put("backend", "minecraft_key_mapping");
            response.put("client_thread_safe", true);
        } catch (Exception error) {
            BridgeLogger.error("Error setting input state: " + error.getMessage());
            response.put("success", false);
            response.put("error", error.getMessage());
        }
        return response;
    }

    public static Map<String, Object> clearAllInputs() {
        Map<String, Object> response = new LinkedHashMap<>();
        try {
            ClientThread.call(() -> {
                for (String input : SUPPORTED_INPUTS) {
                    keyMapping(input).setDown(false);
                }
                return null;
            });
            response.put("success", true);
            response.put("message", "All inputs cleared");
        } catch (Exception error) {
            response.put("success", false);
            response.put("error", error.getMessage());
        }
        return response;
    }

    private static String parseInputType(String type) {
        if (type == null) {
            return null;
        }
        String normalized = type.trim().toUpperCase();
        return SUPPORTED_INPUTS.contains(normalized) ? normalized : null;
    }

    private static KeyMapping keyMapping(String input) {
        Minecraft minecraft = Minecraft.getInstance();
        if (minecraft.options == null) {
            throw new IllegalStateException("Minecraft options not available");
        }
        return switch (input) {
            case "MOVE_FORWARD" -> minecraft.options.keyUp;
            case "MOVE_BACK" -> minecraft.options.keyDown;
            case "MOVE_LEFT" -> minecraft.options.keyLeft;
            case "MOVE_RIGHT" -> minecraft.options.keyRight;
            case "JUMP" -> minecraft.options.keyJump;
            case "SNEAK" -> minecraft.options.keyShift;
            case "SPRINT" -> minecraft.options.keySprint;
            case "CLICK_LEFT" -> minecraft.options.keyAttack;
            case "CLICK_RIGHT" -> minecraft.options.keyUse;
            default -> throw new IllegalArgumentException("Unsupported input: " + input);
        };
    }
}
