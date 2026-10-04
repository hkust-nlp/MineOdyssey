package dev.mcbots.groundnav;

import com.mojang.logging.LogUtils;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;
import net.minecraft.core.BlockPos;
import org.slf4j.Logger;

/**
 * Reads the path from the obfuscated Baritone standalone jar without linking to
 * implementation class names at compile time.
 *
 * <p>Baritone's supported API normally exposes IPathingBehavior and IPath, but
 * the standalone 1.17.0 NeoForge artifact used by this instance obfuscates most
 * of those API names. The stable type shapes and generic signatures are enough
 * to locate current, next, and in-progress paths safely.</p>
 */
final class BaritonePathReader {
    private static final Logger LOGGER = LogUtils.getLogger();

    private Accessors accessors;
    private State initializationFailure;
    private boolean failureLogged;

    /**
     * Disable every user-facing Baritone command and overlay entry point.
     *
     * <p>Ground Navigation embeds Baritone only as a path planner. The
     * evaluated agent must never be able to turn that implementation detail
     * into a movement or coordinate oracle through chat.</p>
     */
    boolean lockDownAgentControls() {
        return executeCommand("set chatControl false")
                && executeCommand("set chatControlAnyway false")
                && executeCommand("set prefixControl false")
                && executeCommand("set echoCommands false")
                && executeCommand("set chatDebug false")
                && executeCommand("set renderPath false")
                && executeCommand("set renderGoal false")
                && executeCommand("set allowBreak false")
                && executeCommand("set allowPlace false")
                && executeCommand("set allowInventory false");
    }

    /** Keep Baritone's route alive while preventing it from pressing movement keys. */
    boolean requestGuidePause() {
        try {
            Accessors currentAccessors = getAccessors();
            // A newly calculated executor starts with safeToCancel=false. If we
            // only request a pause, Baritone may execute that path for one tick
            // before it reaches a safe movement boundary. Guide-only mode owns
            // no movement, so make the initial path boundary safe as well.
            currentAccessors.safeToCancel.setBoolean(currentAccessors.pathingBehavior, true);
            currentAccessors.pauseRequested.setBoolean(currentAccessors.pathingBehavior, true);
            return true;
        } catch (ReflectiveOperationException | RuntimeException error) {
            logFailureOnce("Unable to pause Baritone for guide-only mode", error);
            return false;
        }
    }

    /** Execute a Baritone command on the Minecraft client thread. */
    boolean executeCommand(String command) {
        try {
            Accessors currentAccessors = getAccessors();
            return Boolean.TRUE.equals(currentAccessors.executeCommand.invoke(
                    currentAccessors.commandManager,
                    command));
        } catch (ReflectiveOperationException | RuntimeException error) {
            logFailureOnce("Unable to execute Baritone command: " + command, error);
            return false;
        }
    }

    ReadResult read() {
        if (initializationFailure != null) {
            return new ReadResult(initializationFailure, List.of());
        }

        try {
            Accessors currentAccessors = getAccessors();

            List<BlockPos> current = positionsFromExecutor(
                    currentAccessors.currentExecutor.get(currentAccessors.pathingBehavior));
            List<BlockPos> next = positionsFromExecutor(
                    currentAccessors.nextExecutor.get(currentAccessors.pathingBehavior));

            if (!current.isEmpty()) {
                ArrayList<BlockPos> combined = new ArrayList<>(current.size() + next.size());
                combined.addAll(current);
                appendWithoutDuplicate(combined, next);
                return new ReadResult(State.ACTIVE, List.copyOf(combined));
            }

            List<BlockPos> preview = positionsFromInProgress();
            if (!preview.isEmpty()) {
                return new ReadResult(State.CALCULATING, preview);
            }
            if (currentAccessors.inProgress.get(currentAccessors.pathingBehavior) != null) {
                return new ReadResult(State.CALCULATING, List.of());
            }
            return new ReadResult(State.IDLE, List.of());
        } catch (ClassNotFoundException error) {
            initializationFailure = State.UNAVAILABLE;
            logFailureOnce("Baritone is not installed", error);
            return new ReadResult(State.UNAVAILABLE, List.of());
        } catch (ReflectiveOperationException | RuntimeException error) {
            logFailureOnce("Unable to read Baritone's planned path", error);
            return new ReadResult(State.ERROR, List.of());
        }
    }

    private Accessors getAccessors() throws ReflectiveOperationException {
        if (accessors == null) {
            accessors = discoverAccessors();
        }
        return accessors;
    }

    private Accessors discoverAccessors() throws ReflectiveOperationException {
        ClassLoader loader = BaritonePathReader.class.getClassLoader();
        Class<?> bootstrapType = Class.forName("baritone.c", true, loader);

        Method providerGetter = findMethod(
                bootstrapType,
                method -> Modifier.isStatic(method.getModifiers())
                        && method.getParameterCount() == 0
                        && method.getReturnType().getName().equals("baritone.api.IBaritoneProvider"));
        Object provider = providerGetter.invoke(null);

        Method primaryGetter = findMethod(
                provider.getClass(),
                method -> method.getParameterCount() == 0
                        && method.getReturnType().getName().equals("baritone.d"));
        Object primaryBaritone = primaryGetter.invoke(provider);

        Method commandManagerGetter = findMethod(
                primaryBaritone.getClass(),
                method -> method.getParameterCount() == 0
                        && method.getReturnType().getName().equals("baritone.hm"));
        Object commandManager = commandManagerGetter.invoke(primaryBaritone);
        Method executeCommand = findMethod(
                commandManager.getClass(),
                method -> method.getParameterCount() == 1
                        && method.getParameterTypes()[0] == String.class
                        && method.getReturnType() == boolean.class);

        Method pathingGetter = findMethod(
                primaryBaritone.getClass(),
                method -> method.getParameterCount() == 0
                        && method.getReturnType().getName().equals("baritone.fc"));
        Object pathingBehavior = pathingGetter.invoke(primaryBaritone);

        Class<?> executorType = Class.forName("baritone.jf", false, loader);
        List<Field> executorFields = Arrays.stream(pathingBehavior.getClass().getDeclaredFields())
                .filter(field -> field.getType() == executorType)
                .sorted(Comparator.comparing(Field::getName))
                .toList();
        if (executorFields.size() < 2) {
            throw new NoSuchFieldException("Expected current and next Baritone path executors");
        }

        Field currentExecutor = makeAccessible(executorFields.get(0));
        Field nextExecutor = makeAccessible(executorFields.get(1));
        Field path = makeAccessible(findField(
                executorType,
                field -> field.getType().getName().equals("baritone.bw")));
        Method positions = findMethod(
                path.getType(),
                method -> method.getParameterCount() == 0
                        && method.getReturnType() == List.class
                        && method.getGenericReturnType().getTypeName().contains("baritone.dy"));

        Field inProgress = makeAccessible(findField(
                pathingBehavior.getClass(),
                field -> field.getType().getName().equals("baritone.ho")));
        Field pauseRequested = makeAccessible(findField(
                pathingBehavior.getClass(),
                field -> field.getName().equals("a")
                        && field.getType() == boolean.class));
        Field safeToCancel = makeAccessible(findField(
                pathingBehavior.getClass(),
                field -> field.getName().equals("c")
                        && field.getType() == boolean.class));
        Method bestPath = findMethod(
                inProgress.getType(),
                method -> method.getName().equals("b")
                        && method.getParameterCount() == 0
                        && method.getReturnType() == Optional.class);

        return new Accessors(
                pathingBehavior,
                currentExecutor,
                nextExecutor,
                path,
                positions,
                inProgress,
                bestPath,
                pauseRequested,
                safeToCancel,
                commandManager,
                executeCommand);
    }

    private List<BlockPos> positionsFromExecutor(Object executor) throws ReflectiveOperationException {
        if (executor == null) {
            return List.of();
        }
        return positionsFromPath(accessors.path.get(executor));
    }

    private List<BlockPos> positionsFromInProgress() throws ReflectiveOperationException {
        Object calculation = accessors.inProgress.get(accessors.pathingBehavior);
        if (calculation == null) {
            return List.of();
        }
        Object optional = accessors.bestPath.invoke(calculation);
        if (!(optional instanceof Optional<?> best) || best.isEmpty()) {
            return List.of();
        }
        return positionsFromPath(best.get());
    }

    private List<BlockPos> positionsFromPath(Object path) throws ReflectiveOperationException {
        if (path == null) {
            return List.of();
        }
        Object rawPositions = accessors.positions.invoke(path);
        if (!(rawPositions instanceof List<?> values) || values.isEmpty()) {
            return List.of();
        }

        ArrayList<BlockPos> result = new ArrayList<>(values.size());
        for (Object value : values) {
            if (!(value instanceof BlockPos pos)) {
                throw new IllegalStateException("Baritone path contained a non-BlockPos node");
            }
            result.add(new BlockPos(pos.getX(), pos.getY(), pos.getZ()));
        }
        return List.copyOf(result);
    }

    private static void appendWithoutDuplicate(List<BlockPos> destination, List<BlockPos> addition) {
        if (addition.isEmpty()) {
            return;
        }
        int start = !destination.isEmpty() && destination.getLast().equals(addition.getFirst()) ? 1 : 0;
        destination.addAll(addition.subList(start, addition.size()));
    }

    private void logFailureOnce(String message, Throwable error) {
        if (!failureLogged) {
            failureLogged = true;
            LOGGER.error("[Ground Navigation] {}", message, error);
        }
    }

    private static Method findMethod(Class<?> type, java.util.function.Predicate<Method> predicate)
            throws NoSuchMethodException {
        return Arrays.stream(type.getMethods())
                .filter(predicate)
                .findFirst()
                .orElseThrow(() -> new NoSuchMethodException("No matching method on " + type.getName()));
    }

    private static Field findField(Class<?> type, java.util.function.Predicate<Field> predicate)
            throws NoSuchFieldException {
        return Arrays.stream(type.getDeclaredFields())
                .filter(predicate)
                .findFirst()
                .orElseThrow(() -> new NoSuchFieldException("No matching field on " + type.getName()));
    }

    private static Field makeAccessible(Field field) {
        if (!field.trySetAccessible()) {
            throw new IllegalStateException("Cannot access " + field);
        }
        return field;
    }

    enum State {
        ACTIVE,
        CALCULATING,
        IDLE,
        UNAVAILABLE,
        ERROR
    }

    record ReadResult(State state, List<BlockPos> positions) {
    }

    private record Accessors(
            Object pathingBehavior,
            Field currentExecutor,
            Field nextExecutor,
            Field path,
            Method positions,
            Field inProgress,
            Method bestPath,
            Field pauseRequested,
            Field safeToCancel,
            Object commandManager,
            Method executeCommand) {
    }
}
