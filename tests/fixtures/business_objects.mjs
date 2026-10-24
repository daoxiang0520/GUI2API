import {EventEmitter} from 'node:events';

class Child {
  label = '子对象';
  echo(text) {return text;}
}

class Counter extends EventEmitter {
  count = 0;
  label = 'JavaScript 业务对象';
  #child = new Child();
  #getterCalls = 0;
  get probe() {return ++this.#getterCalls;}
  add(amount) {this.count += amount; this.emit('changed', this.count); return this.count;}
  child() {return this.#child;}
  same_child(other) {return other === this.#child;}
  async echo(text) {console.log('fixture application log'); return text;}
  fail_after_add() {this.count++; throw new Error('fixture failure after a side effect');}
}

export const events = {counter:['changed']};
export function createRoots() {
  const counter = new Counter();
  return {counter, same_counter:counter};
}
