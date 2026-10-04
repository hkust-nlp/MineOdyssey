package com.mcagent.bridge.util;

import net.minecraft.client.Minecraft;

import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** Runs short client operations on Minecraft's main thread. */
public final class ClientThread {
    private static final long TIMEOUT_MILLIS = 3_000L;

    private ClientThread() {
    }

    public static <T> T call(Callable<T> action) throws Exception {
        Minecraft minecraft = Minecraft.getInstance();
        if (minecraft == null) {
            throw new IllegalStateException("Minecraft instance not available");
        }
        if (minecraft.isSameThread()) {
            return action.call();
        }
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<T> result = new AtomicReference<>();
        AtomicReference<Exception> error = new AtomicReference<>();
        minecraft.execute(() -> {
            try {
                result.set(action.call());
            } catch (Exception exception) {
                error.set(exception);
            } finally {
                latch.countDown();
            }
        });
        if (!latch.await(TIMEOUT_MILLIS, TimeUnit.MILLISECONDS)) {
            throw new IllegalStateException("Timed out waiting for Minecraft main thread");
        }
        if (error.get() != null) {
            throw error.get();
        }
        return result.get();
    }
}
