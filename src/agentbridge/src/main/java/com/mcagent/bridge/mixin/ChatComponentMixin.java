package com.mcagent.bridge.mixin;

import com.mcagent.bridge.CoordinateExposureLock;
import net.minecraft.client.gui.components.ChatComponent;
import net.minecraft.network.chat.Component;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Catches Xaero's formatted waypoint message, which the mod inserts directly
 * into the chat component before NeoForge can publish a chat-received event.
 */
@Mixin(ChatComponent.class)
abstract class ChatComponentMixin {

    @Inject(
            method = "addMessage(Lnet/minecraft/network/chat/Component;)V",
            at = @At("HEAD"),
            cancellable = true
    )
    private void agentbridge$filterXaeroWaypointMessage(
            Component component,
            CallbackInfo callback
    ) {
        if (CoordinateExposureLock.blockXaeroCoordinateChat(
                component,
                "Minecraft chat component"
        )) {
            callback.cancel();
        }
    }
}
