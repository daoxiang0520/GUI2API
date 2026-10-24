/* A thread-bound Node-API transport. No application JS runs on Frida's thread. */
'use strict';
const pending = new Map();
let busy = false;
let calls = 0;
let environment = null;
let ownerThread = null;
let listeners = [];
let api = null;

function init(threadId, moduleName) {
  if (Process.arch !== 'x64') throw new Error('Only the verified Windows x64 ABI is supported');
  const main = moduleName ? Process.getModuleByName(moduleName) : Process.mainModule;
  ownerThread = threadId || null;
  function address(name) {
    let p = main.getExportByName(name);
    for (let i = 0; i < 4; i++) {
      const ins = Instruction.parse(p);
      if (ins.mnemonic !== 'jmp') break;
      const arg = ins.operands[0];
      if (arg.type === 'imm') p = ptr(arg.value);
      else if (arg.type === 'mem' && arg.value.base === 'rip')
        p = ins.next.add(arg.value.disp).readPointer();
      else break;
    }
    return p;
  }
  const fn = (name, parameters) => new NativeFunction(address(name), 'int', parameters,
    { scheduling: 'exclusive' });
  api = {
    open: fn('napi_open_handle_scope', ['pointer', 'pointer']),
    close: fn('napi_close_handle_scope', ['pointer', 'pointer']),
    string: fn('napi_create_string_utf8', ['pointer', 'pointer', 'size_t', 'pointer']),
    run: fn('napi_run_script', ['pointer', 'pointer', 'pointer']),
    read: fn('napi_get_value_string_utf8', ['pointer', 'pointer', 'pointer', 'size_t', 'pointer']),
    exceptionPending: fn('napi_is_exception_pending', ['pointer', 'pointer']),
    clearException: fn('napi_get_and_clear_last_exception', ['pointer', 'pointer']),
  };
  const callbacks = {
    onEnter(args) { this.env = args[0]; this.tid = this.threadId; },
    onLeave(ret) {
      if (busy || ret.toInt32() !== 0) return;
      calls++;
      // Capture only an environment observed in an actual JS -> addon callback.
      if (ownerThread === null || ownerThread === this.tid) {
        environment = this.env;
        ownerThread = this.tid;
      }
      if (this.tid !== ownerThread || !pending.size) return;
      const flag = Memory.alloc(1);
      if (api.exceptionPending(environment, flag) !== 0 || flag.readU8()) return;
      const [id, task] = pending.entries().next().value;
      pending.delete(id);
      if (Date.now() >= task.deadline) { task.reject(new Error('expired_before_execution')); return; }
      busy = true;
      const scope = Memory.alloc(Process.pointerSize);
      let opened = false;
      try {
        if (api.open(environment, scope) !== 0) throw new Error('handle_scope_failed');
        opened = true;
        const value = Memory.alloc(Process.pointerSize);
        const output = Memory.alloc(Process.pointerSize);
        const source = Memory.allocUtf8String(task.source);
        if (api.string(environment, source, uint64('0xffffffffffffffff'), value) !== 0) throw new Error('source_string_failed');
        const status = api.run(environment, value.readPointer(), output);
        if (status !== 0) {
          // This invocation owns the new exception; do not consume an existing one.
          api.clearException(environment, output);
          throw new Error('runtime_script_failed:' + status);
        }
        const size = Memory.alloc(Process.pointerSize);
        if (api.read(environment, output.readPointer(), ptr(0), 0, size) !== 0)
          throw new Error('runtime_result_must_be_string');
        const length = size.readU64().toNumber();
        if (length > 2 * 1024 * 1024) throw new Error('runtime_result_too_large');
        const buffer = Memory.alloc(length + 1);
        if (api.read(environment, output.readPointer(), buffer, length + 1, size) !== 0)
          throw new Error('runtime_result_read_failed');
        task.resolve(buffer.readUtf8String(length));
      } catch (error) { task.reject(error); }
      finally { if (opened) api.close(environment, scope.readPointer()); busy = false; }
    }
  };
  const names = ['napi_get_cb_info', 'napi_create_string_utf8', 'napi_call_function',
    'napi_make_callback', 'napi_get_named_property'];
  const addresses = new Set();
  for (const name of names) {
    const p = address(name);
    if (!addresses.has(p.toString())) { listeners.push(Interceptor.attach(p, callbacks)); addresses.add(p.toString()); }
  }
  return { pid: Process.id, architecture: Process.arch, transport: 'observed-node-api-callback',
    api_module: main.name, target_code_injected: true, application_restart: false };
}

rpc.exports = {
  init,
  status() { return { calls, environmentObserved: environment !== null, ownerThread, pending: pending.size }; },
  execute(id, source, timeoutMs) {
    if (typeof source !== 'string' || source.length > 262144) throw new Error('invalid_source');
    if (pending.size) throw new Error('one_request_at_a_time');
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (pending.delete(id)) reject(new Error('expired_before_execution'));
      }, timeoutMs);
      pending.set(id, { source, deadline: Date.now() + timeoutMs,
        resolve: value => { clearTimeout(timer); resolve(value); },
        reject: error => { clearTimeout(timer); reject(error); } });
    });
  },
  close() {
    for (const task of pending.values()) task.reject(new Error('bridge_closed'));
    pending.clear();
    for (const listener of listeners) listener.detach();
    listeners = [];
  }
};
