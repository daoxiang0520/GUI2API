/* Trusted bridge factory. Profiles supply selectors, never JavaScript code. */
(function createFields(nativeHwnd, profile) {
  'use strict';
  const electron = process.mainModule.require('electron');
  const windows = electron.BrowserWindow.getAllWindows().filter(w =>
    w.getNativeWindowHandle().readBigUInt64LE(0).toString() === nativeHwnd);
  if (windows.length !== 1) throw new Error('ambiguous_native_window');
  const window = windows[0];
  const contents = window.webContents;

  async function renderer(request, slots) {
    function field(name) {
      const spec = slots[name];
      if (!spec) throw new Error('unknown_field');
      const matches = Array.from(document.querySelectorAll(spec.css)).filter(e => {
        const box = e.getBoundingClientRect();
        const style = getComputedStyle(e);
        return e.isConnected && box.width > 0 && box.height > 0 && style.visibility !== 'hidden'
          && e.type !== 'password' && !e.disabled && !e.readOnly;
      });
      if (matches.length !== 1) throw new Error(matches.length ? 'ambiguous_field' : 'field_unavailable');
      const element = matches[0];
      if (spec.model === 'prosemirror') {
        const view = element.pmViewDesc && element.pmViewDesc.view;
        const documentNode = () => view && view.state ? view.state.doc : element.pmViewDesc && element.pmViewDesc.node;
        const node = documentNode();
        if (!node || typeof node.textBetween !== 'function' || typeof node.descendants !== 'function')
          throw new Error('editor_model_unavailable');
        const read = () => { const doc = documentNode(); return doc.textBetween(0, doc.content.size, '\n'); };
        return { element, view, documentNode, read, source: view ? 'ProseMirror.EditorView.state.doc' : 'ProseMirror.DocViewDesc.node' };
      }
      if (element.tagName !== 'INPUT' && element.tagName !== 'TEXTAREA') throw new Error('unsupported_field_type');
      return { element, read: () => element.value, source: 'DOM.nativeValue+inputEvent' };
    }
    if (request.op === 'status') {
      const result = {};
      for (const name of Object.keys(slots)) {
        try { result[name] = { available: true, source: field(name).source }; }
        catch (error) { result[name] = { available: false, reason: error.message }; }
      }
      return { fields: result, backend: 'electron-node-api', send_supported: false };
    }
    const item = field(request.slot);
    const before = item.read();
    if (request.op === 'read') return { value: before, source: item.source, truncated: false };
    if (request.op !== 'replace') throw new Error('unsupported_operation');
    if (before !== request.expected) throw new Error('value_conflict');
    if (typeof request.text !== 'string' || request.text.length > 4096 || /[\r\n\u0000]/.test(request.text))
      throw new Error('only_bounded_single_line_text_is_verified');
    if (item.documentNode) {
      // Do not erase images, mentions, marks or other rich draft content.
      let rich = false;
      item.documentNode().descendants(node => {
        if (node.isText && node.marks.length || !node.isText && !node.isTextblock) rich = true;
      });
      if (rich) throw new Error('rich_draft_replacement_not_supported');
      if (item.view && typeof item.view.dispatch === 'function') {
        const state = item.view.state;
        item.view.dispatch(state.tr.insertText(request.text, 0, state.doc.content.size));
      } else {
        // Chromium's native editing pipeline emits input and lets ProseMirror's
        // DOM observer create its transaction. The postcondition reads the
        // immutable ProseMirror document, not just visible DOM text.
        item.element.focus({preventScroll: true});
        if (document.activeElement !== item.element || item.read() !== request.expected)
          throw new Error('editor_focus_or_value_changed');
        const selection = document.getSelection();
        const range = document.createRange();
        range.selectNodeContents(item.element);
        selection.removeAllRanges(); selection.addRange(range);
        if (!item.element.contains(selection.anchorNode) || !item.element.contains(selection.focusNode))
          throw new Error('selection_outside_editor');
        const accepted = request.text === '' ? document.execCommand('delete', false, null)
          : document.execCommand('insertText', false, request.text);
        if (!accepted) throw new Error('editing_command_not_accepted_no_retry');
      }
      const deadline = Date.now() + 500;
      while (item.element.isConnected && item.read() !== request.text && Date.now() < deadline)
        await new Promise(resolve => setTimeout(resolve, 20));
      if (!item.element.isConnected) throw new Error('editor_replaced_after_write_no_retry');
    } else {
      const proto = item.element.tagName === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(item.element, request.text);
      item.element.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: request.text}));
    }
    const actual = item.read();
    if (actual !== request.text) throw new Error('postcondition_failed_no_retry');
    return { value: actual, source: item.source, status: item.documentNode ? 'editor_model_verified' : 'field_value_verified',
      message_sent: false, business_completion_proven: false };
  }

  return {
    async dispatch(request) {
      if (window.isDestroyed() || contents.isDestroyed() || window.webContents !== contents)
        throw new Error('window_binding_changed');
      const code = '(' + renderer.toString() + ')(' + JSON.stringify(request) + ',' + JSON.stringify(profile.slots) + ')';
      return await contents.executeJavaScript(code, false);
    }
  };
})
