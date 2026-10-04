package dev.mcbots.groundnav;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.mojang.logging.LogUtils;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.world.phys.Vec3;
import org.slf4j.Logger;

/**
 * File-based bridge between an evaluation harness and the visual route guide.
 *
 * <p>The harness writes {@code <gameDir>/ground-navigation/request.json}. This
 * controller asks Baritone to calculate one segment at a time, continuously
 * requests Baritone's pause flag, and lets the evaluated agent provide all
 * actual movement input.</p>
 */
final class EvaluationRouteController {
    private static final Logger LOGGER = LogUtils.getLogger();
    private static final Gson GSON = new Gson();
    private static final int REQUEST_POLL_INTERVAL_TICKS = 5;
    private static final int REPLAN_COOLDOWN_TICKS = 30;
    private static final double OFF_ROUTE_DISTANCE = 4.5;
    private static final double SEGMENT_END_REPLAN_DISTANCE = 2.75;

    private String lastRequestId = "";
    private String requestId = "";
    private List<RoutePoint> points = List.of();
    private int pointIndex;
    private double arrivalRadius = 1.5;
    private boolean active;
    private boolean guideOnly;
    private int ticks;
    private int replanCooldown;
    private int ticksWithoutPath;
    private String lastStatusSignature = "";
    private boolean baritoneControlsLocked;

    void beforeClientTick(Minecraft minecraft, BaritonePathReader pathReader) {
        if (minecraft.level == null || minecraft.player == null) {
            return;
        }
        if (++ticks % REQUEST_POLL_INTERVAL_TICKS == 0) {
            pollRequest(minecraft, pathReader);
        }
        if (!baritoneControlsLocked || ticks % 20 == 0) {
            baritoneControlsLocked = pathReader.lockDownAgentControls();
        }
        if (active && !baritoneControlsLocked) {
            pathReader.executeCommand("stop");
            active = false;
            writeStatus(
                    minecraft,
                    "error",
                    "error",
                    "Baritone agent controls are not locked");
            return;
        }
        if (active && guideOnly) {
            pathReader.requestGuidePause();
        }
    }

    void afterClientTick(
            Minecraft minecraft,
            BaritonePathReader pathReader,
            BaritonePathReader.ReadResult pathResult) {
        if (!active || minecraft.player == null) {
            return;
        }
        if (guideOnly) {
            pathReader.requestGuidePause();
        }

        RoutePoint target = points.get(pointIndex);
        Vec3 playerPosition = minecraft.player.position();
        double targetDistance = target.distanceTo(playerPosition);
        if (targetDistance <= arrivalRadius) {
            advance(pathReader, pathResult.state());
            return;
        }

        if (replanCooldown > 0) {
            replanCooldown--;
        }

        List<BlockPos> route = pathResult.positions();
        if (route.isEmpty()) {
            ticksWithoutPath++;
            if (ticksWithoutPath >= REPLAN_COOLDOWN_TICKS && replanCooldown == 0) {
                planCurrentTarget(pathReader, "waiting_for_path");
            }
        } else {
            ticksWithoutPath = 0;
            double distanceToRoute = distanceToRoute(playerPosition, route);
            double distanceToSegmentEnd = distance(playerPosition, route.getLast());
            boolean leftRoute = distanceToRoute > OFF_ROUTE_DISTANCE;
            boolean reachedPartialSegment = distanceToSegmentEnd <= SEGMENT_END_REPLAN_DISTANCE
                    && targetDistance > arrivalRadius + SEGMENT_END_REPLAN_DISTANCE;
            if ((leftRoute || reachedPartialSegment) && replanCooldown == 0) {
                planCurrentTarget(pathReader, leftRoute ? "off_route" : "partial_segment_end");
            }
        }

        writeStatus(
                minecraft,
                "active",
                pathResult.state().name().toLowerCase(),
                null);
    }

    boolean isActive() {
        return active;
    }

    private void pollRequest(Minecraft minecraft, BaritonePathReader pathReader) {
        Path requestPath = controlDirectory(minecraft).resolve("request.json");
        if (!Files.isRegularFile(requestPath)) {
            return;
        }

        try {
            JsonObject request = JsonParser.parseString(Files.readString(requestPath, StandardCharsets.UTF_8))
                    .getAsJsonObject();
            String incomingId = requiredString(request, "id");
            if (incomingId.equals(lastRequestId)) {
                return;
            }
            lastRequestId = incomingId;

            boolean requestedEnabled = !request.has("enabled") || request.get("enabled").getAsBoolean();
            if (!requestedEnabled) {
                requestId = incomingId;
                active = false;
                guideOnly = false;
                points = List.of();
                pathReader.executeCommand("stop");
                writeStatus(minecraft, "disabled", "idle", null);
                return;
            }

            List<RoutePoint> requestedPoints = parsePoints(request.getAsJsonArray("points"));
            if (requestedPoints.isEmpty()) {
                throw new IllegalArgumentException("points must contain at least one target");
            }
            if (!pathReader.lockDownAgentControls()) {
                throw new IllegalStateException("unable to lock Baritone agent controls");
            }
            baritoneControlsLocked = true;

            requestId = incomingId;
            points = List.copyOf(requestedPoints);
            pointIndex = 0;
            arrivalRadius = request.has("arrival_radius")
                    ? Math.max(0.5, request.get("arrival_radius").getAsDouble())
                    : 1.5;
            guideOnly = !request.has("guide_only") || request.get("guide_only").getAsBoolean();
            active = true;
            replanCooldown = 0;
            ticksWithoutPath = 0;

            if (guideOnly) {
                pathReader.requestGuidePause();
            }
            planCurrentTarget(pathReader, "new_request");
            writeStatus(minecraft, "planning", "calculating", null);
        } catch (IOException | RuntimeException error) {
            LOGGER.error("[Ground Navigation] Invalid evaluation request {}", requestPath, error);
            active = false;
            guideOnly = false;
            writeStatus(minecraft, "error", "error", error.getMessage());
        }
    }

    private void advance(BaritonePathReader pathReader, BaritonePathReader.State pathState) {
        pointIndex++;
        if (pointIndex >= points.size()) {
            pathReader.executeCommand("stop");
            active = false;
            guideOnly = false;
            Minecraft minecraft = Minecraft.getInstance();
            writeStatus(minecraft, "completed", pathState.name().toLowerCase(), null);
            return;
        }
        planCurrentTarget(pathReader, "waypoint_reached");
    }

    private void planCurrentTarget(BaritonePathReader pathReader, String reason) {
        if (!active || pointIndex >= points.size()) {
            return;
        }
        if (guideOnly) {
            pathReader.requestGuidePause();
        }
        pathReader.executeCommand("stop");
        RoutePoint target = points.get(pointIndex);
        boolean accepted = pathReader.executeCommand(
                "goto " + target.x + " " + target.y + " " + target.z);
        replanCooldown = REPLAN_COOLDOWN_TICKS;
        ticksWithoutPath = 0;
        if (!accepted) {
            LOGGER.error("[Ground Navigation] Baritone rejected target {} ({})", target, reason);
        }
    }

    private void writeStatus(
            Minecraft minecraft,
            String state,
            String baritoneState,
            String error) {
        if (requestId.isEmpty()) {
            return;
        }
        String signature = requestId + '|' + state + '|' + baritoneState + '|'
                + pointIndex + '|' + baritoneControlsLocked + '|'
                + (error == null ? "" : error);
        if (signature.equals(lastStatusSignature)) {
            return;
        }
        lastStatusSignature = signature;

        JsonObject status = new JsonObject();
        status.addProperty("request_id", requestId);
        status.addProperty("state", state);
        status.addProperty("baritone_state", baritoneState);
        status.addProperty("guide_only", guideOnly);
        status.addProperty("baritone_controls_locked", baritoneControlsLocked);
        status.addProperty("point_index", Math.min(pointIndex, points.size()));
        status.addProperty("point_count", points.size());
        status.addProperty("updated_at", Instant.now().toString());
        if (active && pointIndex < points.size()) {
            status.add("target", GSON.toJsonTree(points.get(pointIndex)));
        }
        if (error != null) {
            status.addProperty("error", error);
        }

        Path directory = controlDirectory(minecraft);
        Path destination = directory.resolve("status.json");
        Path temporary = directory.resolve("status.json.tmp");
        try {
            Files.createDirectories(directory);
            Files.writeString(temporary, GSON.toJson(status) + System.lineSeparator(), StandardCharsets.UTF_8);
            try {
                Files.move(
                        temporary,
                        destination,
                        StandardCopyOption.REPLACE_EXISTING,
                        StandardCopyOption.ATOMIC_MOVE);
            } catch (AtomicMoveNotSupportedException ignored) {
                Files.move(temporary, destination, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException ioError) {
            LOGGER.error("[Ground Navigation] Unable to write evaluation status", ioError);
        }
    }

    private static List<RoutePoint> parsePoints(JsonArray values) {
        if (values == null) {
            return List.of();
        }
        ArrayList<RoutePoint> result = new ArrayList<>(values.size());
        for (JsonElement value : values) {
            JsonObject point = value.getAsJsonObject();
            result.add(new RoutePoint(
                    point.get("x").getAsInt(),
                    point.get("y").getAsInt(),
                    point.get("z").getAsInt(),
                    point.has("id") ? point.get("id").getAsString() : ""));
        }
        return result;
    }

    private static String requiredString(JsonObject object, String key) {
        if (!object.has(key) || object.get(key).getAsString().isBlank()) {
            throw new IllegalArgumentException(key + " must be a non-empty string");
        }
        return object.get(key).getAsString();
    }

    private static Path controlDirectory(Minecraft minecraft) {
        return minecraft.gameDirectory.toPath().resolve("ground-navigation");
    }

    private static double distanceToRoute(Vec3 player, List<BlockPos> route) {
        double minimumSquared = Double.POSITIVE_INFINITY;
        for (BlockPos point : route) {
            double dx = point.getX() + 0.5 - player.x;
            double dy = point.getY() - player.y;
            double dz = point.getZ() + 0.5 - player.z;
            minimumSquared = Math.min(minimumSquared, dx * dx + dy * dy + dz * dz);
        }
        return Math.sqrt(minimumSquared);
    }

    private static double distance(Vec3 player, BlockPos point) {
        return player.distanceTo(new Vec3(point.getX() + 0.5, point.getY(), point.getZ() + 0.5));
    }

    private record RoutePoint(int x, int y, int z, String id) {
        double distanceTo(Vec3 position) {
            return position.distanceTo(new Vec3(x + 0.5, y, z + 0.5));
        }
    }
}
