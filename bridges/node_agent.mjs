// Framework-neutral provider for explicitly registered JavaScript objects.
// No eval, property-path evaluation, debugger attach, or global-object scanning.
import readline from 'node:readline';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {EventEmitter} from 'node:events';
import {Console} from 'node:console';

// Reserve stdout for protocol frames, including during provider initialization.
globalThis.console = new Console({stdout:process.stderr, stderr:process.stderr});

const moduleIndex = process.argv.indexOf('--module');
if (moduleIndex < 0) throw new Error('--module is required');
const providerModule = await import(pathToFileURL(resolve(process.argv[moduleIndex + 1])).href);
const roots = await providerModule.createRoots();
const eventDefinitions = providerModule.events ?? {};
const instance = randomUUID(), objects = new Map(), identities = new WeakMap();
const rootIds = new Set(), subscriptions = new Map(), queue = [];
let dropped = 0, sequence = 0;

class Fault extends Error {
  constructor(code, message) {super(message); this.code = code;}
}

function objectType(value) {
  const proto = Object.getPrototypeOf(value);
  const constructor = proto && Object.getOwnPropertyDescriptor(proto, 'constructor')?.value;
  return typeof constructor === 'function' ? Object.getOwnPropertyDescriptor(constructor, 'name')?.value ?? 'Object' : 'Object';
}

function isJson(value, seen = new Set(), depth = 0) {
  if (depth > 20) return false;
  if (value === null || ['string', 'boolean'].includes(typeof value)) return true;
  if (typeof value === 'number') return Number.isFinite(value);
  if (typeof value !== 'object' || seen.has(value)) return false;
  if (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) return false;
  seen.add(value);
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const result = Object.entries(descriptors).every(([name, descriptor]) =>
    Array.isArray(value) && name === 'length' ||
    !descriptor.get && !descriptor.set && isJson(descriptor.value, seen, depth + 1));
  seen.delete(value);
  return result;
}

function box(value, force = false) {
  if (value === undefined) return {kind:'value', value:null, js_undefined:true};
  if (!force && isJson(value)) return {kind:'value', value};
  if ((typeof value !== 'object' || value === null) && typeof value !== 'function')
    throw new Fault('unsupported_value', 'Value is neither JSON nor a retainable object');
  let ref = identities.get(value);
  if (!ref || !objects.has(ref)) {
    if (objects.size >= 4096) throw new Fault('object_limit', 'Release unused references');
    ref = instance + ':' + randomUUID();
    identities.set(value, ref); objects.set(ref, value);
  }
  return {kind:'object', ref, type:objectType(value)};
}

for (const value of Object.values(roots)) rootIds.add(box(value, true).ref);

function object(ref) {
  if (!objects.has(ref)) throw new Fault('stale_ref', 'Object reference is no longer available');
  return objects.get(ref);
}

function unbox(value) {
  if (value?.kind === 'object') return object(value.ref);
  if (value?.kind === 'value' && isJson(value.value)) return value.value;
  throw new Fault('invalid_arguments', 'Arguments require value/object envelopes');
}

function descriptors(obj) {
  const members = new Map();
  for (let p = obj; p && p !== Object.prototype && p !== EventEmitter.prototype; p = Object.getPrototypeOf(p)) {
    for (const [name, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(p))) {
      if (!name.startsWith('_') && name !== 'constructor' && !members.has(name)) members.set(name, descriptor);
    }
  }
  return members;
}

function describe(obj) {
  const properties = [], methods = [];
  for (const [name, descriptor] of descriptors(obj)) {
    if (typeof descriptor.value === 'function') {
      const fn = descriptor.value;
      methods.push({name, parameters:Array.from({length:fn.length}, (_, i) => ({name:'arg' + i, type:'unknown', required:true})), returns:'unknown', effect:Object.getOwnPropertyDescriptor(fn, 'appctlReadOnly')?.value ? 'read' : 'unknown', types_available:false});
    } else properties.push({name, type:descriptor.get ? 'unknown' : typeof descriptor.value, readable:!!descriptor.get || 'value' in descriptor, writable:!!descriptor.set || descriptor.writable === true, getter_executed:false});
  }
  const names = new Set();
  for (const [name, value] of Object.entries(roots)) if (value === obj) for (const event of eventDefinitions[name] ?? []) names.add(event);
  if (obj instanceof EventEmitter) for (const name of obj.eventNames()) if (typeof name === 'string') names.add(name);
  return {type:objectType(obj), properties, methods, events:[...names].map(name => ({name}))};
}

function member(obj, name, category) {
  const rows = describe(obj)[category].filter(row => row.name === name);
  if (rows.length !== 1) throw new Fault('unknown_member', 'Choose one advertised member');
  return rows[0];
}

function removeSubscription(ref, name) {
  const key = ref + '\n' + name;
  const current = subscriptions.get(key);
  if (current) {current.obj.off(name, current.listener); subscriptions.delete(key);}
}

async function request(op, args) {
  if (op === 'ping') return {protocol:'appctl.objects/1', instance, runtime:'javascript', access:'registered_roots', operations:['list','describe','get','set','call','watch','events','release'], arbitrary_process_attach:false};
  if (op === 'list') return {objects:Object.entries(roots).map(([name, obj]) => ({name, ...box(obj, true)}))};
  if (op === 'events') {
    const timeout = args.timeout_ms ?? 0, limit = args.limit ?? 100;
    if (!Number.isInteger(timeout) || timeout < 0 || timeout > 5000 || !Number.isInteger(limit) || limit < 1 || limit > 1000) throw new Fault('invalid_arguments', 'Invalid event bounds');
    const end = Date.now() + timeout;
    while (!queue.length && Date.now() < end) await new Promise(r => setTimeout(r, 5));
    const data = {events:queue.splice(0, limit), dropped, values_in_events:false};
    dropped = 0; return data;
  }
  const ref = args.ref, obj = object(ref);
  if (op === 'describe') return {object:box(obj, true), ...describe(obj)};
  if (op === 'get') {
    const prop = member(obj, args.member, 'properties');
    if (!prop.readable) throw new Fault('unsupported', 'Property is not readable');
    return {result:box(Reflect.get(obj, args.member))};
  }
  if (op === 'set') {
    const prop = member(obj, args.member, 'properties');
    if (!prop.writable) throw new Fault('unsupported', 'Property is not writable');
    const value = unbox(args.value);
    if (!Reflect.set(obj, args.member, value)) throw new Fault('write_failed', 'Property write was rejected');
    if (!prop.readable) return {status:'executed_unverified'};
    const actual = Reflect.get(obj, args.member);
    return {status:JSON.stringify(actual) === JSON.stringify(value) ? 'verified' : 'verification_failed', result:box(actual)};
  }
  if (op === 'call') {
    const method = member(obj, args.member, 'methods');
    const values = (args.arguments ?? []).map(unbox);
    if (values.length < method.parameters.length) throw new Fault('invalid_arguments', 'Missing required arguments');
    const fn = descriptors(obj).get(args.member).value;
    return {status:'returned', effect:method.effect, result:box(await Reflect.apply(fn, obj, values))};
  }
  if (op === 'watch') {
    const names = args.names ?? describe(obj).events.map(event => event.name);
    if (!Array.isArray(names)) throw new Fault('invalid_arguments', 'names must be an array');
    if (!(obj instanceof EventEmitter)) throw new Fault('unsupported', 'This object has no EventEmitter adapter');
    for (const name of names) {
      member(obj, name, 'events'); removeSubscription(ref, name);
      if (args.enabled !== false) {
        const listener = () => {
          if (queue.length >= 1024) {queue.shift(); dropped++;}
          queue.push({sequence:++sequence, object:box(obj, true), name});
        };
        obj.on(name, listener); subscriptions.set(ref + '\n' + name, {obj, listener});
      }
    }
    return {watching:[...subscriptions.keys()].filter(key => key.startsWith(ref + '\n')).map(key => key.slice(ref.length + 1)), values_in_events:false};
  }
  if (op === 'release') {
    if (rootIds.has(ref)) throw new Fault('root_reference', 'Root objects live for the provider session');
    for (const event of describe(obj).events) removeSubscription(ref, event.name);
    objects.delete(ref); identities.delete(obj); return {released:true};
  }
  throw new Fault('unknown_operation', 'Unknown object operation');
}

const lines = readline.createInterface({input:process.stdin, crlfDelay:Infinity});
for await (const line of lines) {
  let message;
  try {
    message = JSON.parse(line);
    if (!message || typeof message !== 'object' || Array.isArray(message) || !message.args || typeof message.args !== 'object' || Array.isArray(message.args)) throw new Fault('invalid_request', 'Expected request and object args');
    process.stdout.write(JSON.stringify({id:message.id, ok:true, data:await request(message.op, message.args)}) + '\n');
  } catch (e) {
    process.stdout.write(JSON.stringify({id:message?.id ?? null, ok:false, error:{code:e.code ?? 'application_exception', message:String(e.message), operation_may_have_executed:!e.code}}) + '\n');
  }
}
