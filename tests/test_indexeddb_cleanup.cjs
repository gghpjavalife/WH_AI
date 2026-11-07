const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(
  path.join(__dirname, "..", "src", "ui", "components", "indexeddb_storage", "index.html"),
  "utf8"
);
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const context = {
  window: { addEventListener() {}, parent: { postMessage() {} } },
  document: { referrer: "" },
  IDBKeyRange: { lowerBound: value => value },
};
vm.createContext(context);
vm.runInContext(script, context);

function database(records, abort = false) {
  return {
    close() {},
    transaction(name, mode) {
      assert.equal(name, "records");
      assert.equal(mode, "readwrite");
      const staged = new Map(records);
      const transaction = {};
      transaction.objectStore = () => ({
        openKeyCursor(lowerBound) {
          const request = {};
          const keys = [...staged.keys()].filter(key => key >= lowerBound).sort();
          let index = 0;
          function next() {
            setImmediate(() => {
              const key = keys[index++];
              request.result = key === undefined ? null : {
                key,
                primaryKey: key,
                continue: next,
              };
              request.onsuccess();
              if (!request.result || !request.result.key.startsWith(lowerBound)) {
                if (abort) {
                  transaction.error = new Error("aborted");
                  transaction.onabort();
                } else {
                  records.clear();
                  for (const [k, value] of staged) records.set(k, value);
                  transaction.oncomplete();
                }
              }
            });
          }
          next();
          return request;
        },
        delete(key) { staged.delete(key); },
      });
      return transaction;
    },
  };
}

async function main() {
  const records = new Map([
    ["portfolio_analysis:first", { analysis: 1 }],
    ["portfolio_analysis:second", { analysis: 2 }],
    ["portfolio_analysis:\uffffsuffix", { analysis: 3 }],
    ["portfolio_analysis_extra:first", { keep: true }],
    ["user_settings:owner", { encrypted_sensitive: "ciphertext" }],
    ["oauth_states:state", { keep: true }],
  ]);
  const original = new Map(records);
  await assert.rejects(context.deleteCollection(database(records, true), "portfolio_analysis"), /aborted/);
  assert.deepEqual(records, original);
  context.testDatabase = database(records);
  vm.runInContext("openDatabase = async () => testDatabase", context);
  assert.equal(await context.execute({
    action: "delete_collection", collection: "portfolio_analysis",
  }), true);
  assert.deepEqual([...records.keys()], [
    "portfolio_analysis_extra:first", "user_settings:owner", "oauth_states:state",
  ]);
  assert.deepEqual(records.get("user_settings:owner"), { encrypted_sensitive: "ciphertext" });
  await context.deleteCollection(database(records), "portfolio_analysis");
  assert.equal(records.size, 3);
  await assert.rejects(context.execute({
    action: "delete_collection", collection: "portfolio_analysis:other",
  }), /collection name/);
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
