package com.mcagent.bridge;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CoordinateExposureLockTest {

    @Test
    void permitsTheMainXaeroWorldMap() {
        assertFalse(
                CoordinateExposureLock.isBlockedScreenClass("xaero.map.gui.GuiMap")
        );
    }

    @Test
    void blocksWaypointAndSettingsScreens() {
        assertTrue(
                CoordinateExposureLock.isBlockedScreenClass(
                        "xaero.common.gui.GuiAddWaypoint"
                )
        );
        assertTrue(
                CoordinateExposureLock.isBlockedScreenClass(
                        "xaero.common.gui.GuiWaypoints"
                )
        );
        assertTrue(
                CoordinateExposureLock.isBlockedScreenClass(
                        "xaero.map.gui.GuiWorldMapSettings"
                )
        );
    }

    @Test
    void permitsTheVanillaChatScreen() {
        assertFalse(
                CoordinateExposureLock.isBlockedScreenClass(
                        "net.minecraft.client.gui.screens.ChatScreen"
                )
        );
    }

    @Test
    void recognizesOnlyXaeroCoordinateChatPayloads() {
        assertTrue(
                CoordinateExposureLock.isXaeroCoordinatePayload(
                        "xaero-waypoint:Home:H:12:64:-35:0:false:0:Internal-overworld-waypoints"
                )
        );
        assertTrue(
                CoordinateExposureLock.isXaeroCoordinatePayload(
                        "/xaero_waypoint_add:Home:H:12:64:-35"
                )
        );
        assertFalse(
                CoordinateExposureLock.isXaeroCoordinatePayload(
                        "Meet me near the station at noon"
                )
        );
        assertFalse(CoordinateExposureLock.isXaeroCoordinatePayload(null));
    }

    @Test
    void recognizesXaeroSharedWaypointTranslationKeys() {
        assertTrue(
                CoordinateExposureLock.isXaeroCoordinateTranslationKey(
                        "gui.xaero_waypoint_shared2"
                )
        );
        assertTrue(
                CoordinateExposureLock.isXaeroCoordinateTranslationKey(
                        "gui.xaero_waypoint_shared_dimension2"
                )
        );
        assertFalse(
                CoordinateExposureLock.isXaeroCoordinateTranslationKey(
                        "chat.type.text"
                )
        );
    }

    @Test
    void ignoresScreensFromOtherMods() {
        assertFalse(
                CoordinateExposureLock.isBlockedScreenClass(
                        "net.minecraft.client.gui.screens.inventory.InventoryScreen"
                )
        );
        assertFalse(CoordinateExposureLock.isBlockedScreenClass(null));
    }
}
