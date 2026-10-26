"""Explicit capture of a currently visible bound control, never visual locating."""
import ctypes as C
from ctypes import wintypes as W
import math
import struct
import zlib

from object_bridge import Fault


def png_rgb(width,height,rgb):
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    rows=b''.join(b'\0'+rgb[y*width*3:(y+1)*width*3] for y in range(height))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')


def capture_control(gui,source,row,max_pixels):
    import win32gui
    from gui_api import checked
    window=source['target']['window']
    context=checked(gui.desktop.request('ping')).get('desktop',{})
    if not context.get('interactive_confirmed'):raise Fault('desktop_not_interactive','An unlocked interactive desktop is required')
    user=C.WinDLL('user32',use_last_error=True);gdi=C.WinDLL('gdi32',use_last_error=True)
    user.SetThreadDpiAwarenessContext.argtypes=[W.HANDLE];user.SetThreadDpiAwarenessContext.restype=W.HANDLE
    previous=user.SetThreadDpiAwarenessContext(W.HANDLE(-4))
    if not previous:raise Fault('resource_capture_unavailable','Physical coordinate context could not be established')
    dc=memory=bitmap=old=None
    try:
        def geometry(current):
            rect=current.get('rect',{})
            if current.get('offscreen') or current.get('password') or rect.get('width',0)<=0 or rect.get('height',0)<=0:
                raise Fault('resource_not_visible','Resource has no visible unprotected bounds')
            if win32gui.IsIconic(window) or not win32gui.IsWindowVisible(window) or win32gui.GetForegroundWindow()!=window:
                raise Fault('resource_not_visible','Capture requires the intended foreground window; no activation was attempted')
            x,y=int(math.ceil(rect['x'])),int(math.ceil(rect['y']))
            right,bottom=int(math.floor(rect['x']+rect['width'])),int(math.floor(rect['y']+rect['height']))
            client=win32gui.GetClientRect(window);left,top=win32gui.ClientToScreen(window,(client[0],client[1]))
            cr,cb=win32gui.ClientToScreen(window,(client[2],client[3]))
            x1,y1,x2,y2=max(x,left),max(y,top),min(right,cr),min(bottom,cb)
            if x2<=x1 or y2<=y1:raise Fault('resource_not_visible','Resource is outside the window client region')
            capture=(x1,y1,x2,y2)
            # Conservatively reject any higher visible window intersecting the
            # region. Transparent/tool windows are not assumed harmless.
            above=[]
            def visit(hwnd,_):above.append(hwnd)
            win32gui.EnumWindows(visit,None)
            if window not in above:raise Fault('resource_not_visible','Window could not be resolved in the desktop order')
            for other in above[:above.index(window)]:
                if not win32gui.IsWindowVisible(other) or win32gui.IsIconic(other):continue
                a,b,c,d=win32gui.GetWindowRect(other)
                if max(a,x1)<min(c,x2) and max(b,y1)<min(d,y2):raise Fault('target_occluded','Another window covers the requested resource')
            for px,py in ((x1,y1),(x2-1,y1),(x1,y2-1),(x2-1,y2-1),((x1+x2)//2,(y1+y2)//2)):
                hit=win32gui.WindowFromPoint((px,py))
                if win32gui.GetAncestor(hit,2)!=window:raise Fault('target_occluded','Resource hit-test did not resolve to its window')
            return capture,capture!=(x,y,right,bottom)
        rect,clipped=geometry(row)
        state=gui._event_state(source)
        x,y,right,bottom=rect;width,height=right-x,bottom-y
        scale=min(1,math.sqrt(max_pixels/(width*height)));dw,dh=max(1,int(width*scale)),max(1,int(height*scale))
        for name,args,restype in (
            ('GetDC',[W.HWND],W.HDC),('ReleaseDC',[W.HWND,W.HDC],C.c_int)):
            function=getattr(user,name);function.argtypes=args;function.restype=restype
        for name,args,restype in (
            ('CreateCompatibleDC',[W.HDC],W.HDC),('CreateCompatibleBitmap',[W.HDC,C.c_int,C.c_int],W.HBITMAP),
            ('SelectObject',[W.HDC,W.HANDLE],W.HANDLE),('DeleteObject',[W.HANDLE],W.BOOL),('DeleteDC',[W.HDC],W.BOOL),
            ('SetStretchBltMode',[W.HDC,C.c_int],C.c_int),
            ('StretchBlt',[W.HDC,C.c_int,C.c_int,C.c_int,C.c_int,W.HDC,C.c_int,C.c_int,C.c_int,C.c_int,W.DWORD],W.BOOL),
            ('GetDIBits',[W.HDC,W.HBITMAP,W.UINT,W.UINT,C.c_void_p,C.c_void_p,W.UINT],C.c_int)):
            function=getattr(gdi,name);function.argtypes=args;function.restype=restype
        dc=user.GetDC(None);memory=gdi.CreateCompatibleDC(dc);bitmap=gdi.CreateCompatibleBitmap(dc,dw,dh)
        if not dc or not memory or not bitmap:raise Fault('resource_capture_unavailable','Could not allocate a capture surface')
        old=gdi.SelectObject(memory,bitmap);gdi.SetStretchBltMode(memory,4)
        if not gdi.StretchBlt(memory,0,0,dw,dh,dc,x,y,width,height,0x00CC0020|0x40000000):
            raise Fault('resource_capture_unavailable','The desktop did not return resource pixels')
        gdi.SelectObject(memory,old);old=None
        header=struct.pack('<IiiHHIIiiII',40,dw,-dh,1,32,0,dw*dh*4,0,0,0,0)
        info=C.create_string_buffer(header+b'\0'*16);pixels=C.create_string_buffer(dw*dh*4)
        if gdi.GetDIBits(dc,bitmap,0,dh,pixels,info,0)!=dh:raise Fault('resource_capture_unavailable','Incomplete resource pixels')
        current=gui._observe_bound(row['ref'])
        if current is None or not gui._same_bound_identity(row,current) or geometry(current)[0]!=rect:
            raise Fault('resource_changed','The resource moved or changed during capture')
        after=gui._event_state(source)
        if state.get('revision') is not None and state.get('revision')!=after.get('revision'):
            raise Fault('resource_changed','The source changed during capture; no image was returned')
        raw=pixels.raw;rgb=bytearray(dw*dh*3)
        rgb[0::3]=raw[2::4];rgb[1::3]=raw[1::4];rgb[2::3]=raw[0::4]
        return png_rgb(dw,dh,rgb),{'representation':'rendered_region','visible_only':True,'clipped':clipped,
            'scale':scale,'capture_backend':'Win32.GDI.visible_region','content_identity_verified':False}
    finally:
        if old and memory:gdi.SelectObject(memory,old)
        if bitmap:gdi.DeleteObject(bitmap)
        if memory:gdi.DeleteDC(memory)
        if dc:user.ReleaseDC(None,dc)
        user.SetThreadDpiAwarenessContext(previous)
