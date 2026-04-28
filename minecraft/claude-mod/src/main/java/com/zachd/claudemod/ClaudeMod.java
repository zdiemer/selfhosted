package com.zachd.claudemod;

import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class ClaudeMod implements ModInitializer {
    public static final Logger LOG = LoggerFactory.getLogger("claudemod");

    // Rolling tick-time history (nanoseconds), used by /claudemod query perf.
    // Yarn 1.20.1 doesn't expose MinecraftServer.lastTickLengths publicly and
    // reflection breaks across intermediary mappings, so we maintain our own.
    public static final long[] TICK_HISTORY_NS = new long[100];
    public static volatile int tickHistoryFill = 0;
    private static int tickHistoryIdx = 0;
    private static long tickStartNs = 0;

    @Override
    public void onInitialize() {
        CommandRegistrationCallback.EVENT.register(
            (dispatcher, registryAccess, env) -> {
                ClaudeCommand.register(dispatcher);
                ClaudeQueryCommand.register(dispatcher);
                ClaudeMarkerCommand.register(dispatcher);
            }
        );
        // After server start, load the markers file and hook BlueMap.
        ServerLifecycleEvents.SERVER_STARTED.register(ClaudeMarkerCommand::onServerStarted);

        // Track tick durations for the perf query — START fires just before
        // the server's tick logic, END just after, so the delta is the tick
        // body length.
        ServerTickEvents.START_SERVER_TICK.register(s -> tickStartNs = System.nanoTime());
        ServerTickEvents.END_SERVER_TICK.register(s -> {
            long delta = System.nanoTime() - tickStartNs;
            TICK_HISTORY_NS[tickHistoryIdx] = delta;
            tickHistoryIdx = (tickHistoryIdx + 1) % TICK_HISTORY_NS.length;
            if (tickHistoryFill < TICK_HISTORY_NS.length) tickHistoryFill++;
        });

        LOG.info("claude-mod initialized; /claude + /claudemod are registered");
    }
}
