package com.mcagent.bridge;

import com.mcagent.bridge.util.BridgeLogger;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.network.chat.ClickEvent;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.HoverEvent;
import net.minecraft.network.chat.Style;
import net.minecraft.network.chat.contents.TranslatableContents;
import net.neoforged.neoforge.client.event.ClientChatReceivedEvent;
import net.neoforged.neoforge.client.event.ScreenEvent;

import java.util.Locale;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Evaluation-only guard against Xaero screens that reveal or can re-enable
 * exact waypoint coordinates, including the Xaero chat-sharing path.
 *
 * <p>The main world map remains available. All other Xaero screens are denied
 * while the guard is enabled, including waypoint editors and settings pages.
 * Ordinary chat remains available, but Xaero's share confirmation is denied
 * and Xaero waypoint payloads are discarded before they enter chat. The
 * implementation deliberately uses class names and translation keys so
 * AgentBridge does not need a compile-time dependency on either Xaero mod.</p>
 */
public final class CoordinateExposureLock {

    private static final String POLICY_VERSION = "xaero-coordinate-filter-v2";
    private static final String PROPERTY_NAME = "agentbridge.coordinateLock";
    private static final String ENVIRONMENT_NAME = "AGENTBRIDGE_COORDINATE_LOCK";
    private static final String XAERO_PACKAGE_PREFIX = "xaero.";
    private static final String ALLOWED_WORLD_MAP_SCREEN = "xaero.map.gui.GuiMap";
    private static final String XAERO_SHARE_CONFIRMATION_KEY =
            "gui.xaero_share_msg1";
    private static final String XAERO_SHARED_WAYPOINT_KEY_PREFIX =
            "gui.xaero_waypoint_shared";

    private static final boolean ENABLED = resolveEnabled();
    private static final AtomicLong BLOCKED_SCREEN_COUNT = new AtomicLong();
    private static final AtomicLong BLOCKED_CHAT_COUNT = new AtomicLong();

    private CoordinateExposureLock() {
    }

    static void onScreenOpening(ScreenEvent.Opening event) {
        if (!ENABLED) {
            return;
        }

        Screen newScreen = event.getNewScreen();
        if (!isBlockedScreen(newScreen)) {
            return;
        }

        event.setCanceled(true);
        long blockedCount = BLOCKED_SCREEN_COUNT.incrementAndGet();
        BridgeLogger.warn(
                "Coordinate exposure lock blocked screen "
                        + newScreen.getClass().getName()
                        + " (count="
                        + blockedCount
                        + ")"
        );
    }

    static void onChatReceived(ClientChatReceivedEvent event) {
        if (!blockXaeroCoordinateChat(event.getMessage(), "NeoForge chat event")) {
            return;
        }

        event.setCanceled(true);
    }

    public static boolean blockXaeroCoordinateChat(Component component, String source) {
        if (!ENABLED || !containsXaeroCoordinatePayload(component)) {
            return false;
        }

        long blockedCount = BLOCKED_CHAT_COUNT.incrementAndGet();
        BridgeLogger.warn(
                "Coordinate exposure lock discarded Xaero waypoint chat payload"
                        + " from "
                        + source
                        + " (count="
                        + blockedCount
                        + ")"
        );
        return true;
    }

    static boolean isEnabled() {
        return ENABLED;
    }

    static String getPolicyVersion() {
        return POLICY_VERSION;
    }

    static long getBlockedScreenCount() {
        return BLOCKED_SCREEN_COUNT.get();
    }

    static long getBlockedChatCount() {
        return BLOCKED_CHAT_COUNT.get();
    }

    static boolean isBlockedScreen(Screen screen) {
        if (screen == null) {
            return false;
        }
        return isBlockedScreenClass(screen.getClass().getName())
                || containsTranslationKey(screen.getTitle(), XAERO_SHARE_CONFIRMATION_KEY);
    }

    static boolean isBlockedScreenClass(String screenClass) {
        if (screenClass == null) {
            return false;
        }
        return screenClass.startsWith(XAERO_PACKAGE_PREFIX)
                && !ALLOWED_WORLD_MAP_SCREEN.equals(screenClass);
    }

    static boolean isXaeroCoordinatePayload(String text) {
        if (text == null) {
            return false;
        }
        String normalized = text.toLowerCase(Locale.ROOT);
        return normalized.contains("xaero-waypoint:")
                || normalized.contains("xaero_waypoint:")
                || normalized.contains("xaero_waypoint_add:");
    }

    static boolean isXaeroCoordinateTranslationKey(String key) {
        return key != null && key.startsWith(XAERO_SHARED_WAYPOINT_KEY_PREFIX);
    }

    static boolean containsXaeroCoordinatePayload(Component component) {
        return containsXaeroCoordinatePayload(component, 0);
    }

    private static boolean containsXaeroCoordinatePayload(Component component, int depth) {
        if (component == null || depth > 16) {
            return false;
        }
        if (isXaeroCoordinatePayload(component.getString())) {
            return true;
        }
        if (component.getContents() instanceof TranslatableContents translatable) {
            if (isXaeroCoordinateTranslationKey(translatable.getKey())) {
                return true;
            }
            for (Object argument : translatable.getArgs()) {
                if (argument instanceof Component nested
                        && containsXaeroCoordinatePayload(nested, depth + 1)) {
                    return true;
                }
                if (argument instanceof String text && isXaeroCoordinatePayload(text)) {
                    return true;
                }
            }
        }
        if (styleContainsXaeroCoordinatePayload(component.getStyle(), depth)) {
            return true;
        }
        for (Component sibling : component.getSiblings()) {
            if (containsXaeroCoordinatePayload(sibling, depth + 1)) {
                return true;
            }
        }
        return false;
    }

    private static boolean styleContainsXaeroCoordinatePayload(Style style, int depth) {
        if (style == null) {
            return false;
        }
        ClickEvent clickEvent = style.getClickEvent();
        if (clickEvent instanceof ClickEvent.RunCommand command
                && isXaeroCoordinatePayload(command.command())) {
            return true;
        }
        if (clickEvent instanceof ClickEvent.SuggestCommand command
                && isXaeroCoordinatePayload(command.command())) {
            return true;
        }
        if (isXaeroCoordinatePayload(style.getInsertion())) {
            return true;
        }
        HoverEvent hoverEvent = style.getHoverEvent();
        return hoverEvent instanceof HoverEvent.ShowText showText
                && containsXaeroCoordinatePayload(showText.value(), depth + 1);
    }

    private static boolean containsTranslationKey(Component component, String key) {
        if (component == null) {
            return false;
        }
        if (component.getContents() instanceof TranslatableContents translatable
                && key.equals(translatable.getKey())) {
            return true;
        }
        for (Component sibling : component.getSiblings()) {
            if (containsTranslationKey(sibling, key)) {
                return true;
            }
        }
        return false;
    }

    private static boolean resolveEnabled() {
        String property = System.getProperty(PROPERTY_NAME);
        if (property != null && !property.isBlank()) {
            return Boolean.parseBoolean(property.trim());
        }

        String environment = System.getenv(ENVIRONMENT_NAME);
        return environment != null
                && Boolean.parseBoolean(environment.trim());
    }
}
