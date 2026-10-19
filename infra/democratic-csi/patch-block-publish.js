// Compatibility fix for democratic-csi v1.9.5 NodePublishVolume on Ubuntu.
// A block bind mount's devtmpfs source may be "udev", not hardcoded "dev".
// Both paths have already passed pathIsMounted. Validate actual device identity,
// not the devtmpfs name (which cannot distinguish two different block devices).
const fs = require("fs");
const old = 'normalized_staging_device = "dev"; // special syntax for single file bind mounts';
const replacement = `const stagedBlock = await fs.promises.stat(normalized_staging_path);
                  const publishedBlock = await fs.promises.stat(target_path);
                  if (!stagedBlock.isBlockDevice() ||
                      !publishedBlock.isBlockDevice() ||
                      stagedBlock.rdev !== publishedBlock.rdev) {
                    throw new GrpcError(
                      grpc.status.FAILED_PRECONDITION,
                      "published block device does not match staged block device"
                    );
                  }
                  return {};`;

function patch(source) {
  if (source.split(old).length !== 2) {
    throw new Error("Expected exactly one v1.9.5 block publish check; review upstream before deploying");
  }
  return source.replace(old, replacement);
}
if (require.main === module) {
  const source = fs.readFileSync("/home/csi/app/src/driver/index.js", "utf8");
  fs.writeFileSync("/driver-patch/index.js", patch(source));
}
module.exports = { patch, replacement };
