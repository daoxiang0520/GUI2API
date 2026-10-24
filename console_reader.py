"""Read-only legacy console fallback in an isolated helper process.

Never attaches the GUI bridge itself to a foreign console, never sends input,
and never creates a console. ConPTY/Terminal hosts may expose no legacy buffer.
"""
import ctypes
from ctypes import wintypes as w
import json
import os
import sys


class Coord(ctypes.Structure): _fields_=[('x',w.SHORT),('y',w.SHORT)]
class SmallRect(ctypes.Structure): _fields_=[('left',w.SHORT),('top',w.SHORT),('right',w.SHORT),('bottom',w.SHORT)]
class BufferInfo(ctypes.Structure):
    _fields_=[('size',Coord),('cursor',Coord),('attributes',w.WORD),('window',SmallRect),('maximum',Coord)]


def read(pid, created_at, limit=100000):
    import psutil
    if psutil.Process(pid).create_time()!=created_at: raise ValueError('Process identity changed')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    for name,args,result in (
        ('AttachConsole',[w.DWORD],w.BOOL),('FreeConsole',[],w.BOOL),
        ('CreateFileW',[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE],w.HANDLE),
        ('CloseHandle',[w.HANDLE],w.BOOL),('GetConsoleScreenBufferInfo',[w.HANDLE,ctypes.POINTER(BufferInfo)],w.BOOL),
        ('ReadConsoleOutputCharacterW',[w.HANDLE,w.LPWSTR,w.DWORD,Coord,ctypes.POINTER(w.DWORD)],w.BOOL)):
        fn=getattr(kernel,name); fn.argtypes=args; fn.restype=result
    # Inherited pipe handles are retained by Python's stdout object; this helper
    # has no interactive console because its parent uses CREATE_NO_WINDOW.
    kernel.FreeConsole()
    if not kernel.AttachConsole(pid):
        return {'ok':False,'error':{'code':'console_unavailable','message':'Selected process exposes no attachable legacy console buffer; use its UIA TextPattern or a terminal integration'},'win32_error':ctypes.get_last_error()}
    handle=None
    try:
        handle=kernel.CreateFileW('CONOUT$',0x80000000,3,None,3,0,None)
        if handle==ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
        info=BufferInfo()
        if not kernel.GetConsoleScreenBufferInfo(handle,ctypes.byref(info)): raise ctypes.WinError(ctypes.get_last_error())
        # Visible viewport only; no claim to capture all scrollback or alternate
        # buffers. Keep cells/newlines unambiguously bounded.
        lines=[]; remaining=limit; truncated=False
        for y in range(info.window.top,info.window.bottom+1):
            width=min(info.size.x,remaining)
            if width<=0: truncated=True; break
            text=ctypes.create_unicode_buffer(width+1); count=w.DWORD()
            if not kernel.ReadConsoleOutputCharacterW(handle,text,width,Coord(0,y),ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            lines.append(text[:count.value].rstrip()); remaining-=width+1
        return {'ok':True,'data':{'value':'\n'.join(lines)[:limit],'source':'ReadConsoleOutputCharacterW',
            'scope':'selected_process_visible_legacy_console_buffer','truncated':truncated,'read_only':True,
            'pid':pid,'text_input_supported':False,'coverage':'partial'}}
    finally:
        if handle and handle!=ctypes.c_void_p(-1).value: kernel.CloseHandle(handle)
        kernel.FreeConsole()


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try: result=read(int(sys.argv[1]),float(sys.argv[2]))
    except Exception as exc: result={'ok':False,'error':{'code':'console_unavailable','message':type(exc).__name__}}
    print(json.dumps(result,ensure_ascii=False))
