package com.zachd.claudemod.write;

/**
 * Thrown by {@link TargetResolver#resolveTarget} when the live world doesn't
 * present a target the protocol can write to. The {@link #code} field is the
 * machine-readable error code surfaced to the bridge ({@code wrong_target_type},
 * {@code backpack_unequipped}, etc.).
 */
final class TargetException extends RuntimeException {
    final String code;

    TargetException(String code, String msg) {
        super(msg);
        this.code = code;
    }
}
