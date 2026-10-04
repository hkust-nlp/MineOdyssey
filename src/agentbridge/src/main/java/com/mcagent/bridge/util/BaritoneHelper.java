package com.mcagent.bridge.util;

/** Optional Baritone capability detection without a compile/runtime API link. */
public final class BaritoneHelper {
    private BaritoneHelper() {
    }

    public static boolean isAvailable() {
        try {
            Class.forName(
                    "baritone.api.BaritoneAPI",
                    false,
                    BaritoneHelper.class.getClassLoader()
            );
            return true;
        } catch (ClassNotFoundException | LinkageError ignored) {
            return false;
        }
    }
}
