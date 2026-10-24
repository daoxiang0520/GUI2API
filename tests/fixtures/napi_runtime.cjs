'use strict';
const addon = require(process.argv[2]);
globalThis.appctlOwnedCounter = { count: 0, add(n) { this.count += n; return this.count; } };
console.log(JSON.stringify({ ready: true, pid: process.pid, thread: addon.tick() }));
globalThis.appctlOwnedTimer = setInterval(() => addon.tick(), 30);
globalThis.appctlOwnedKeepAlive = setInterval(() => {}, 3600000);
