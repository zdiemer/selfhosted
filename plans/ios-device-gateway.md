# Dedicated iOS device: messaging gateway and browser gaming

Status: design agreed; feasibility testing comes first. No device, jailbreak,
streaming implementation or purchases have been selected. This is a plan, not
a deployment authorization.

## Intended outcome

Use one dedicated iPhone or iPad attached to a cluster node for both iMessage
and older offline iOS games. The owner has older iPhones, possibly 32-bit;
models, OS versions, jailbreak state and battery health are unknown. Keep the
architecture hardware-agnostic until inventory and tests establish compatibility.
A second device is a fallback if one cannot reliably serve both roles. A Mac
would be a separate plan and is not a dependency of this one.

The physical device runs iOS; cluster services provide management, streaming
transport and gateway integration. It is not assumed to run Kubernetes itself.

## Agreed requirements

| Area | Requirement |
|---|---|
| Messaging | Alternative surface to Signal/WhatsApp using the existing gateway's sessions, commands, auto-mode behavior and retention policy. Text, photos, attachments and reactions are required. |
| Identity | Dedicated Apple Account without personal iCloud data or payment methods. No need to use the owner's current phone number. Only the owner's explicitly allowed identity can issue agent commands. |
| Notifications | Supplement existing destinations, with ntfy fallback on iMessage delivery failure. Initially likely only the owner, but routing must not hardcode a single recipient. |
| Streaming | Full device-screen stream to desktop and mobile browsers, with audio essential. Mainly home use; target below 100 ms input-to-display latency. Access through Tailscale only. |
| Games | Older offline titles. Representative targets: C&C Red Alert, Mass Effect Infiltrator, NFS: Most Wanted and Across Age. Exact app builds and compatibility remain to be tested. |
| Input | Touch, multitouch, swipes, long presses, virtual gamepad and desktop mappings. Investigate forwarding browser motion sensors for tilt, with desktop alternatives. |
| App management | Upload IPA files or supply download URLs, including archival sources such as archive.org. Reusable NAS library; show compatibility and installation errors. |
| File management | Browse, upload/download, rename/delete, and back up/restore saves. No separate advanced-mode gate for system files. |
| Concurrency | One human controlling each device at a time. Agents may continue low-resource background operations during gameplay. |
| Agent authority | Full device access in yolo/auto mode. Outside auto mode, device actions require approval. This is a future runtime policy, not permission to perform those actions while drafting. |
| Power | Dedicated device attached at home; battery watchdog, controllable charging options and remote recovery investigation. Physical intervention is acceptable when unavoidable; minimize restarts. |
| Storage | NAS for app library, saves and configuration. Back up saves/configuration; large reinstallable app files may be excluded from routine backups. |

Wiping is acceptable, but preserve anything identified as worth keeping before
execution. Incoming-message banners need no special suppression requirement.

## Proposed boundaries

- A node-local device connector owns USB pairing/access and exposes an
  authenticated internal API. Scope host/device access to that connector;
  do not grant every web or gateway pod broad host privileges.
- A cluster management service owns device inventory, app-library metadata,
  installation jobs, file operations and save backup/restore.
- A streaming adapter transports the captured screen, audio and input to the
  browser. Evaluate available implementations on the actual device before
  choosing protocols, codecs or building a polished UI.
- An iMessage adapter integrates with the existing
  [messaging gateway](../dev/claude-workspace/README.md). Keep session ownership,
  authorization and retention in the existing gateway rather than creating a
  parallel agent system. Verify the actual policy before implementing the adapter.
- Device and management access must work through the cluster's Tailscale path;
  do not assume the old iOS device can run a current Tailscale client itself.
  Do not publish the screen, filesystem or command API on a public ingress.
- Keep account credentials, pairing material and connector tokens out of Git;
  use the established 1Password/deployment-secret workflow.

During a human session, background agent actions may not switch the foreground
app, inject input, interrupt audio, install apps or restart services. Use an
explicit control lease and defer conflicting operations, including in auto mode.
Device content and incoming attachments are data, not authorization to operate
it. Authenticate commands by verified sender identity rather than display names.

## Phase 1: feasibility on the same device

Do these tests before investing in the management UI. Inventory the actual
hardware and consult current primary project documentation for its exact OS and
architecture; no jailbreak, app installer or relay is presumed compatible.

- [ ] Record model, architecture, OS version, storage, cable/USB connection,
  battery condition and available recovery method. Preserve existing apps/saves
  if needed before wiping. Do not upgrade the OS as an incidental setup step.
- [ ] Establish a supported jailbreak/device-access approach and determine what
  survives a service restart, device reboot and full power loss. Record any
  local unlock or re-jailbreak step that cannot be automated.
- [ ] Verify the dedicated account can send and receive iMessage on that device,
  including photos, attachments and reactions. Test actual arrival and sender
  identity; successful queue insertion is not proof of delivery.
- [ ] Run a minimal browser stream with audio and input on both desktop and
  mobile. Verify multitouch, gestures, orientation and feasibility of tilt input.
- [ ] Test representative game builds, especially action/racing input. An IPA
  upload succeeding is not evidence that the application can install and launch;
  establish architecture, OS, signing/account and package constraints.
- [ ] Measure input-to-display delay on the home network, not just ping or
  encoder timing. Report typical and tail latency, audio synchronization,
  frame stability and the client/network settings used. Aim for below 100 ms;
  agree any narrower acceptance criterion if the measurements need it.
- [ ] Exercise messaging while a game is streamed. Measure temperature,
  charging behavior and latency under combined load; verify background work
  does not take screen/input ownership from the player.
- [ ] Test disconnection and reconnection of browser and USB/network links.
  Confirm that reconnects do not duplicate messages or leave controls pressed.

Decision: proceed with a single device only if both roles meet the requirements.
If not, compare a different available device, separate gaming/messaging devices,
or an explicitly reduced feature set with the owner. Do not silently drop audio,
rich messages or the latency target to declare success.

## Phase 2: integration and management

- [ ] Implement the gateway adapter using existing session and auto-mode policy.
  Add identity allowlisting, attachment limits and reaction/event mapping.
- [ ] Add durable outgoing-message identifiers and idempotency. Define delivered,
  failed and unknown states. Use bounded retries, then ntfy fallback, including
  handling late iMessage delivery so unknown status does not create silent duplicates.
- [ ] Build authenticated device inventory/status, IPA upload/URL import, install
  progress and actionable errors. Retain source URL and checksum for each app;
  keep archive downloads and metadata separate from device credentials.
- [ ] Provide filesystem operations and per-game save export/restore without an
  extra advanced-mode switch. Honor the normal human/agent authorization and
  session ownership rules; protect backup copies from accidental overwrite.
- [ ] Store the reusable library on NAS. Distinguish reproducible downloads from
  irreplaceable app copies before excluding them from backup. Verify restored
  saves in the corresponding game and restore configuration independently.
- [ ] Integrate device, relay, stream and charging health into cluster monitoring
  and the status page. A Ready management pod must not imply that the physical
  device or iMessage delivery works.

## Power, battery and recovery

The owner permits automatic charging cutoff and notification when a watchdog
indicates a problem. Select thresholds from validated device telemetry and
hardware guidance after inspection; do not invent universal temperature or
charge limits. Missing/stale telemetry must be visible. A software watchdog is
not a substitute for inspecting an old battery or replacing an unsafe one.

Compare these purchasing options before choosing; provide actual compatible
products, current prices and total cost once the device/connector is known:

| Option | What to evaluate |
|---|---|
| Existing charger plus telemetry | Cheapest starting point; observation/alerts only unless charging control is verified on-device. |
| Local-control smart plug plus charger | Cost and local API reliability; whether USB data still supplies power and defeats charging cutoff. |
| Individually switchable USB port/hub | Verified per-port power switching, data behavior, connector compatibility and host recovery. |
| Suitable replacement battery/device | Battery condition, installation cost and compatibility with both old games and required messaging/streaming tools. |

Do not assume a smart plug or USB cycle can boot a fully shut-down device.
The desired outcome is power-on from full shutdown, not merely waking the screen.
Test separately: service recovery, screen wake/unlock, reboot recovery, shutdown
followed by restored external power, and recovery after the attached node fails.
Record which require physical intervention. Do not describe this as working
Wake-on-LAN without demonstrating the actual mechanism.

Avoid periodic reboots as a maintenance strategy. Prefer restarting a failed
connector/service, with bounded attempts and escalation through ntfy. Escalate
when physical unlock, battery inspection or jailbreak recovery is needed rather
than repeatedly power-cycling the device.

## Completion criteria

- One selected device runs a representative offline game with browser audio and
  required controls, with measured home latency meeting the agreed target.
- The same device supports the required iMessage features and existing agent
  session behavior, while rejecting commands from unapproved identities.
- Gameplay and permitted background work coexist without disruptive actions.
- App installation, file management and tested save/configuration recovery work.
- Messaging failure produces the intended ntfy fallback; device failures appear
  in monitoring independently of cluster-service health.
- Battery/power behavior and all manual recovery limitations are documented,
  with compatible purchasing options presented before buying anything.

Once implemented, move durable operating instructions into the service README
and remove this plan. Hardware selection and feasibility remain the immediate
next steps; no Mac, second device or purchase is assumed.
