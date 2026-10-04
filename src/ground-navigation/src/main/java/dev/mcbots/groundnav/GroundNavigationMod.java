package dev.mcbots.groundnav;

import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.common.NeoForge;

@Mod(value = GroundNavigationMod.MOD_ID, dist = Dist.CLIENT)
public final class GroundNavigationMod {
    public static final String MOD_ID = "ground_navigation";

    public GroundNavigationMod(IEventBus modBus) {
        modBus.addListener(GroundNavigationClient::registerKeyMappings);
        NeoForge.EVENT_BUS.addListener(GroundNavigationClient::onClientTickPre);
        NeoForge.EVENT_BUS.addListener(GroundNavigationClient::onClientTick);
    }
}
