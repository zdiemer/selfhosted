package com.zachd.claudemod;

import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class ClaudeMod implements ModInitializer {
    public static final Logger LOG = LoggerFactory.getLogger("claudemod");

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
        LOG.info("claude-mod initialized; /claude + /claudemod are registered");
    }
}
