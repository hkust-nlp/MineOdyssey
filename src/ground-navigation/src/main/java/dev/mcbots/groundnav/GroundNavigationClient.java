package dev.mcbots.groundnav;

import com.mojang.blaze3d.platform.InputConstants;
import java.util.List;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.minecraft.client.particle.Particle;
import net.minecraft.core.BlockPos;
import net.minecraft.core.particles.DustParticleOptions;
import net.minecraft.network.chat.Component;
import net.minecraft.util.Mth;
import net.minecraft.world.phys.Vec3;
import net.neoforged.neoforge.client.event.ClientTickEvent;
import net.neoforged.neoforge.client.event.RegisterKeyMappingsEvent;

final class GroundNavigationClient {
    private static final int UPDATE_INTERVAL_TICKS = 2;
    private static final int PARTICLE_LIFETIME_TICKS = 3;
    private static final int MAX_PARTICLES_PER_UPDATE = 260;
    private static final double MAX_TRAIL_DISTANCE = 96.0;
    private static final double TRAIL_SPACING = 0.8;
    private static final double ARROW_SPACING = 12.0;
    private static final double ARROW_LENGTH = 0.6;
    private static final double ARROW_HALF_WIDTH = 0.28;

    private static final DustParticleOptions TRAIL_PARTICLE = new DustParticleOptions(0x21E6A1, 0.22F);
    private static final DustParticleOptions DIRECTION_PARTICLE = new DustParticleOptions(0xFFD34E, 0.3F);
    private static final DustParticleOptions DESTINATION_PARTICLE = new DustParticleOptions(0xFF5A6E, 0.34F);

    private static final KeyMapping TOGGLE_KEY = new KeyMapping(
            "key.ground_navigation.toggle",
            InputConstants.Type.KEYSYM,
            InputConstants.KEY_N,
            KeyMapping.Category.MISC);
    private static final BaritonePathReader PATH_READER = new BaritonePathReader();
    private static final EvaluationRouteController EVALUATION_ROUTE = new EvaluationRouteController();

    private static boolean enabled = true;
    private static int ticks;
    private static BaritonePathReader.State lastReportedState;

    private GroundNavigationClient() {
    }

    static void registerKeyMappings(RegisterKeyMappingsEvent event) {
        event.register(TOGGLE_KEY);
    }

    static void onClientTickPre(ClientTickEvent.Pre event) {
        EVALUATION_ROUTE.beforeClientTick(Minecraft.getInstance(), PATH_READER);
    }

    static void onClientTick(ClientTickEvent.Post event) {
        Minecraft minecraft = Minecraft.getInstance();

        if (EVALUATION_ROUTE.isActive()) {
            while (TOGGLE_KEY.consumeClick()) {
                // The evaluator owns the guide during a task. Discard manual
                // toggle input so the agent cannot hide or alter it.
            }
            enabled = true;
        } else {
            while (TOGGLE_KEY.consumeClick()) {
                enabled = !enabled;
                lastReportedState = null;
                if (minecraft.player != null) {
                    minecraft.player.displayClientMessage(Component.translatable(
                            enabled
                                    ? "message.ground_navigation.enabled"
                                    : "message.ground_navigation.disabled"), true);
                }
            }
        }

        if (minecraft.level == null || minecraft.player == null) {
            return;
        }

        BaritonePathReader.ReadResult result = PATH_READER.read();
        boolean evaluationWasActive = EVALUATION_ROUTE.isActive();
        EVALUATION_ROUTE.afterClientTick(minecraft, PATH_READER, result);

        if (!evaluationWasActive) {
            reportStateChange(minecraft, result);
        }
        if (enabled
                && ++ticks % UPDATE_INTERVAL_TICKS == 0
                && !result.positions().isEmpty()) {
            renderTrail(minecraft, result.positions());
        }
    }

    private static void reportStateChange(Minecraft minecraft, BaritonePathReader.ReadResult result) {
        if (result.state() == lastReportedState) {
            return;
        }
        lastReportedState = result.state();

        Component message = switch (result.state()) {
            case ACTIVE -> Component.translatable("message.ground_navigation.active", result.positions().size());
            case CALCULATING -> Component.translatable("message.ground_navigation.calculating");
            case IDLE -> Component.translatable("message.ground_navigation.idle");
            case UNAVAILABLE -> Component.translatable("message.ground_navigation.unavailable");
            case ERROR -> Component.translatable("message.ground_navigation.error");
        };
        minecraft.player.displayClientMessage(message, true);
    }

    private static void renderTrail(Minecraft minecraft, List<BlockPos> route) {
        int nearest = findNearestNode(route, minecraft.player.position());
        if (nearest < 0) {
            return;
        }

        ParticleBudget budget = new ParticleBudget(MAX_PARTICLES_PER_UPDATE);
        double traveled = 0.0;
        double nextArrowAt = 3.0;
        int lastVisibleIndex = nearest;

        BlockPos first = route.get(nearest);
        spawn(minecraft, TRAIL_PARTICLE, center(first), budget);

        for (int index = nearest + 1; index < route.size() && traveled < MAX_TRAIL_DISTANCE; index++) {
            BlockPos fromBlock = route.get(index - 1);
            BlockPos toBlock = route.get(index);
            Vec3 from = center(fromBlock);
            Vec3 to = center(toBlock);
            Vec3 delta = to.subtract(from);
            double length = delta.length();
            if (length < 1.0E-5) {
                continue;
            }

            double visibleLength = Math.min(length, MAX_TRAIL_DISTANCE - traveled);
            Vec3 direction = delta.scale(1.0 / length);
            spawnSegment(minecraft, from, direction, visibleLength, TRAIL_PARTICLE, TRAIL_SPACING, budget);

            while (nextArrowAt <= traveled + visibleLength) {
                double along = nextArrowAt - traveled;
                Vec3 arrowPosition = from.add(direction.scale(along));
                spawnArrow(minecraft, arrowPosition, direction, budget);
                nextArrowAt += ARROW_SPACING;
            }

            traveled += visibleLength;
            lastVisibleIndex = index;
            if (visibleLength < length || budget.exhausted()) {
                break;
            }

            if (index + 1 < route.size() && isTurn(fromBlock, toBlock, route.get(index + 1))) {
                spawnTurnMarker(minecraft, center(toBlock), budget);
            }
        }

        if (lastVisibleIndex == route.size() - 1 && traveled <= MAX_TRAIL_DISTANCE) {
            spawnDestination(minecraft, center(route.getLast()), budget);
        }
    }

    private static int findNearestNode(List<BlockPos> route, Vec3 player) {
        int nearest = -1;
        double nearestDistance = Double.POSITIVE_INFINITY;
        for (int index = 0; index < route.size(); index++) {
            BlockPos node = route.get(index);
            double dx = node.getX() + 0.5 - player.x;
            double dy = node.getY() - player.y;
            double dz = node.getZ() + 0.5 - player.z;
            double distance = dx * dx + dy * dy + dz * dz;
            if (distance < nearestDistance) {
                nearestDistance = distance;
                nearest = index;
            }
        }
        return nearestDistance <= 24.0 * 24.0 ? nearest : -1;
    }

    private static void spawnSegment(
            Minecraft minecraft,
            Vec3 start,
            Vec3 direction,
            double length,
            DustParticleOptions particle,
            double spacing,
            ParticleBudget budget) {
        int samples = Math.max(1, Mth.ceil(length / spacing));
        for (int sample = 1; sample <= samples && !budget.exhausted(); sample++) {
            double distance = length * sample / samples;
            spawn(minecraft, particle, start.add(direction.scale(distance)), budget);
        }
    }

    private static void spawnArrow(Minecraft minecraft, Vec3 tip, Vec3 pathDirection, ParticleBudget budget) {
        double horizontalLength = Math.hypot(pathDirection.x, pathDirection.z);
        if (horizontalLength < 1.0E-5) {
            return;
        }
        Vec3 forward = new Vec3(
                pathDirection.x / horizontalLength,
                0.0,
                pathDirection.z / horizontalLength);
        Vec3 side = new Vec3(-forward.z, 0.0, forward.x);
        Vec3 base = tip.subtract(forward.scale(ARROW_LENGTH));
        Vec3 left = base.add(side.scale(ARROW_HALF_WIDTH));
        Vec3 right = base.subtract(side.scale(ARROW_HALF_WIDTH));

        spawnLine(minecraft, tip, left, DIRECTION_PARTICLE, 0.18, budget);
        spawnLine(minecraft, tip, right, DIRECTION_PARTICLE, 0.18, budget);
    }

    private static void spawnTurnMarker(Minecraft minecraft, Vec3 center, ParticleBudget budget) {
        for (int index = 0; index < 4 && !budget.exhausted(); index++) {
            double angle = Math.PI * 2.0 * index / 4.0;
            spawn(minecraft, DIRECTION_PARTICLE, center.add(
                    Math.cos(angle) * 0.12,
                    0.015,
                    Math.sin(angle) * 0.12), budget);
        }
    }

    private static void spawnDestination(Minecraft minecraft, Vec3 center, ParticleBudget budget) {
        for (int index = 0; index < 12 && !budget.exhausted(); index++) {
            double angle = Math.PI * 2.0 * index / 12.0;
            spawn(minecraft, DESTINATION_PARTICLE, center.add(
                    Math.cos(angle) * 0.36,
                    0.025,
                    Math.sin(angle) * 0.36), budget);
        }
    }

    private static void spawnLine(
            Minecraft minecraft,
            Vec3 from,
            Vec3 to,
            DustParticleOptions particle,
            double spacing,
            ParticleBudget budget) {
        Vec3 delta = to.subtract(from);
        double length = delta.length();
        if (length < 1.0E-5) {
            return;
        }
        spawnSegment(minecraft, from, delta.scale(1.0 / length), length, particle, spacing, budget);
    }

    private static boolean isTurn(BlockPos previous, BlockPos current, BlockPos next) {
        double firstX = current.getX() - previous.getX();
        double firstZ = current.getZ() - previous.getZ();
        double secondX = next.getX() - current.getX();
        double secondZ = next.getZ() - current.getZ();
        double firstLength = Math.hypot(firstX, firstZ);
        double secondLength = Math.hypot(secondX, secondZ);
        if (firstLength < 1.0E-5 || secondLength < 1.0E-5) {
            return false;
        }
        double cosine = (firstX * secondX + firstZ * secondZ) / (firstLength * secondLength);
        return cosine < 0.72;
    }

    private static Vec3 center(BlockPos pos) {
        return new Vec3(pos.getX() + 0.5, pos.getY() + 0.055, pos.getZ() + 0.5);
    }

    private static void spawn(
            Minecraft minecraft,
            DustParticleOptions particleOptions,
            Vec3 position,
            ParticleBudget budget) {
        if (!budget.take()) {
            return;
        }
        Particle particle = minecraft.particleEngine.createParticle(
                particleOptions,
                position.x,
                position.y,
                position.z,
                0.0,
                0.0,
                0.0);
        if (particle != null) {
            particle.setLifetime(PARTICLE_LIFETIME_TICKS);
        }
    }

    private static final class ParticleBudget {
        private int remaining;

        private ParticleBudget(int remaining) {
            this.remaining = remaining;
        }

        private boolean take() {
            if (remaining <= 0) {
                return false;
            }
            remaining--;
            return true;
        }

        private boolean exhausted() {
            return remaining <= 0;
        }
    }
}
