/* Exercise the production renderer guard before any editable surface mutation. */
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');

async function rejectsRichNode(node) {
  let edits = 0;
  const doc = {content: {size: 1}, textBetween: () => '', descendants: fn => fn(node)};
  const element = {isConnected: true, disabled: false, readOnly: false,
    getBoundingClientRect: () => ({width: 100, height: 50}),
    pmViewDesc: {node: doc}, focus: () => {edits++;}};
  const rendererContext = {document: {querySelectorAll: () => [element]},
    getComputedStyle: () => ({visibility: 'visible'})};
  const contents = {isDestroyed: () => false, executeJavaScript: code => vm.runInNewContext(code, rendererContext)};
  const window = {isDestroyed: () => false, webContents: contents,
    getNativeWindowHandle: () => ({readBigUInt64LE: () => 1n})};
  const factory = vm.runInNewContext(source, {process: {mainModule: {require: () => ({BrowserWindow: {getAllWindows: () => [window]}})}}});
  const bridge = factory('1', {slots: {draft: {css: '.editor', model: 'prosemirror'}}});
  await assert.rejects(bridge.dispatch({op: 'replace', slot: 'draft', text: 'probe', expected: ''}),
    /rich_draft_replacement_not_supported/);
  assert.strictEqual(edits, 0);
}

(async () => {
  await rejectsRichNode({isText: false, isTextblock: false, isBlock: true}); // block image
  await rejectsRichNode({isText: false, isTextblock: false, isBlock: false}); // mention/inline image
  await rejectsRichNode({isText: true, marks: [{type: 'link'}]});
  console.log(JSON.stringify({ok: true, protected_cases: 3, editable_surface_mutations: 0}));
})().catch(error => {console.error(error); process.exitCode = 1;});
