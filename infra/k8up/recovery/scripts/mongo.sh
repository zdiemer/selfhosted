#!/bin/sh
set -eu
mkdir -p /work/data
mongod --dbpath=/work/data --bind_ip=127.0.0.1 --port=27017 --logpath=/tmp/mongo.log --pidfilepath=/tmp/mongo.pid --fork
trap 'mongod --dbpath=/work/data --shutdown >/dev/null 2>&1' EXIT
mongorestore --host=127.0.0.1 --port=27017 --gzip --archive="$ARCHIVE" --stopOnError > /tmp/restore.log 2>&1 || { echo RESTORE_FAILED; exit 1; }
# Only counts/validation results leave the pod, never document contents.
mongosh --quiet --host=127.0.0.1 --port=27017 --eval '
let collections = 0, documents = 0;
for (const item of db.adminCommand({listDatabases: 1}).databases) {
  const database = db.getSiblingDB(item.name);
  for (const coll of database.getCollectionInfos({type: "collection"})) {
    const result = database.runCommand({validate: coll.name, full: true});
    if (!result.ok || !result.valid) throw new Error("Restored collection validation failed");
    if (!["admin", "local", "config"].includes(item.name)) {
      collections++;
      documents += database.getCollection(coll.name).countDocuments({});
    }
  }
}
if (collections < 1 || documents < 1) throw new Error("No application data restored");
print(JSON.stringify({restored_collections: collections, restored_documents: documents}));
'
echo RESTORE_VERIFIED
