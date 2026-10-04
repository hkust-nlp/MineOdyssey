package com.mcagent.bridge;

import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.components.toasts.SystemToast;

final class ToastSuppressor {
    private ToastSuppressor() {}

    static void suppressUnsecureServerWarning(Minecraft minecraft) {
        SystemToast.forceHide(
            minecraft.getToastManager(),
            SystemToast.SystemToastId.UNSECURE_SERVER_WARNING
        );
    }
}
