package com.mcagent.bridge.api;

import com.mcagent.bridge.util.BridgeLogger;
import com.mcagent.bridge.util.ClientThread;
import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

import java.util.HashMap;
import java.util.Map;

/**
 * Handler for block interaction API
 * Handles right-clicking blocks and item use
 */
public class BlockHandler {
    /**
     * Right-click a block at the given position
     * @param x Block X coordinate
     * @param y Block Y coordinate
     * @param z Block Z coordinate
     * @return Response map
     */
    public static Map<String, Object> rightClickBlock(int x, int y, int z) {
        Map<String, Object> response = new HashMap<>();

        try {
            BlockPos pos = new BlockPos(x, y, z);
            ClientThread.call(() -> {
                Minecraft minecraft = Minecraft.getInstance();
                LocalPlayer player = minecraft.player;
                if (player == null) {
                    throw new IllegalStateException("Player not available");
                }
                if (minecraft.gameMode == null) {
                    throw new IllegalStateException("Game mode not available");
                }

                Vec3 hitVec = new Vec3(pos.getX() + 0.5, pos.getY() + 0.5, pos.getZ() + 0.5);
                BlockHitResult hitResult = new BlockHitResult(
                    hitVec,
                    Direction.UP,
                    pos,
                    false
                );

                minecraft.gameMode.useItemOn(player, InteractionHand.MAIN_HAND, hitResult);
                return null;
            });

            BridgeLogger.debug("Right-clicked block at " + pos);

            response.put("success", true);
            response.put("message", "Right-clicked block");
            response.put("position", Map.of("x", x, "y", y, "z", z));

        } catch (Exception e) {
            BridgeLogger.error("Error right-clicking block: " + e.getMessage());
            e.printStackTrace();
            response.put("success", false);
            response.put("error", e.getMessage());
        }

        return response;
    }

    /**
     * Right-click in the air (use item)
     * @return Response map
     */
    public static Map<String, Object> rightClick() {
        Map<String, Object> response = new HashMap<>();

        try {
            ClientThread.call(() -> {
                Minecraft mc = Minecraft.getInstance();
                if (mc.player == null) {
                    throw new IllegalStateException("Player not available");
                }
                if (mc.gameMode == null) {
                    throw new IllegalStateException("Game mode not available");
                }
                LocalPlayer player = mc.player;
                InteractionHand hand = InteractionHand.MAIN_HAND;
                ItemStack itemInHand = player.getItemInHand(hand);
                HitResult hitResult = mc.hitResult;

                if (hitResult != null) {
                    if (hitResult.getType() == HitResult.Type.ENTITY) {
                        EntityHitResult entityHit = (EntityHitResult) hitResult;
                        Entity entity = entityHit.getEntity();
                        InteractionResult result = mc.gameMode.interactAt(player, entity, entityHit, hand);
                        if (!result.consumesAction()) {
                            result = mc.gameMode.interact(player, entity, hand);
                        }
                        if (result.consumesAction()) {
                            if (shouldSwing(result)) {
                                player.swing(hand);
                            }
                            return null;
                        }
                    } else if (hitResult.getType() == HitResult.Type.BLOCK) {
                        BlockHitResult blockHit = (BlockHitResult) hitResult;
                        InteractionResult result = mc.gameMode.useItemOn(player, hand, blockHit);
                        if (result.consumesAction()) {
                            if (shouldSwing(result)) {
                                player.swing(hand);
                            }
                            return null;
                        }
                        if (result == InteractionResult.FAIL) {
                            return null;
                        }
                    }
                }

                if (!itemInHand.isEmpty()) {
                    InteractionResult result = mc.gameMode.useItem(player, hand);
                    if (result.consumesAction() && shouldSwing(result)) {
                        player.swing(hand);
                    }
                }
                return null;
            });

            BridgeLogger.debug("Right-clicked (use item)");

            response.put("success", true);
            response.put("message", "Right-clicked");

        } catch (Exception e) {
            BridgeLogger.error("Error right-clicking: " + e.getMessage());
            response.put("success", false);
            response.put("error", e.getMessage());
        }

        return response;
    }

    private static boolean shouldSwing(InteractionResult result) {
        try {
            Object value = InteractionResult.class.getMethod("shouldSwing").invoke(result);
            return Boolean.TRUE.equals(value);
        } catch (ReflectiveOperationException ignored) {
            // Minecraft 1.21.11 moved this signal to Success.swingSource().
        }

        try {
            Object swingSource = result.getClass().getMethod("swingSource").invoke(result);
            return swingSource != null && !"NONE".equals(swingSource.toString());
        } catch (ReflectiveOperationException ignored) {
            return result.consumesAction();
        }
    }
}
