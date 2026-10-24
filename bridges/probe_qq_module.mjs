// Load only an installed native module in an isolated process and inspect exports.
// No QQ session objects are constructed; no login or messaging method is called.
import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import {writeSync} from 'node:fs';
const require = createRequire(import.meta.url);
const path = resolve(process.argv[2]);
const result = {ok:false, node_version:process.version, module:path, explicit_business_calls:0, account_session_initialization_requested:false};
try {
  const native = require(path);
  const exports = [];
  for (const [name, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(native))) {
    const value = descriptor.value;
    const item = {name, type:typeof value, accessor:!!descriptor.get};
    if (value && ['object','function'].includes(typeof value)) {
      item.members = Object.entries(Object.getOwnPropertyDescriptors(value)).filter(([n]) => !['length','name','arguments','caller'].includes(n)).map(([n,d]) => ({name:n,type:typeof d.value,accessor:!!d.get}));
      const prototype = Object.getOwnPropertyDescriptor(value, 'prototype')?.value;
      if (prototype) item.instance_methods = Object.entries(Object.getOwnPropertyDescriptors(prototype)).filter(([n,d]) => n !== 'constructor' && typeof d.value === 'function').map(([n]) => n);
    }
    exports.push(item);
  }
  result.ok = true;
  result.exports = exports;
} catch (e) {
  result.error = {code:e.code ?? 'module_load_failed',message:String(e.message)};
}
writeSync(1, JSON.stringify(result) + '\n');
process.exit(0);
