using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace AppCtl {
    public static partial class Worker {
        [StructLayout(LayoutKind.Sequential)] struct GuiThreadState {
            public int Size,Flags; public IntPtr Active,Focus,Capture,MenuOwner,MoveSize,Caret;
            public Rect CaretRect;
        }
        [DllImport("user32.dll")] static extern bool GetGUIThreadInfo(uint thread,out GuiThreadState state);
        static object Recovery(string op,Dictionary<string,object> a) {
            long h=Hwnd(a); int pid=Pid(h);
            if(I(a,"pid",pid)!=pid) throw new Fault("window_replaced","Window process changed.");
            if(op=="request_accessibility") {
                // These documented client APIs request WM_GETOBJECT through the
                // accessibility infrastructure. They do not force app support.
                var timer=Stopwatch.StartNew(); int attempted=0,msaa=0,uia=0;
                foreach(long child in ChildHandles(h,16)) {
                    if(timer.ElapsedMilliseconds>=2000) break;
                    attempted++;
                    try { Accessibility.IAccessible obj; Guid iid=new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
                        if(AccessibleObjectFromWindow(new IntPtr(child),0xFFFFFFFC,ref iid,out obj)>=0 && obj!=null) msaa++;
                    } catch(COMException) { }
                    try { if(Engine3().ElementFromHandle(new IntPtr(child))!=null) uia++; } catch(COMException) { }
                }
                return D("status","requested_unverified","window",h,"pid",pid,"windows_requested",attempted,
                    "msaa_roots",msaa,"uia_roots",uia,"complete_tree_guaranteed",false,"global_settings_changed",false,
                    "application_restarted",false,"next_action","find_refresh",
                    "chromium_relaunch_plan",D("argument","--force-renderer-accessibility","requires_explicit_relaunch",true,"support_verified",false));
            }
            if(op=="dismiss_popup") {
                // Cancel only an active standard menu belonging to the bound
                // foreground window. No editor focus, guessed click or Enter.
                if(GetForegroundWindow()!=new IntPtr(h)) throw new Fault("focus_unverified","Bring this window to front before dismissing its popup.");
                int ignored; uint thread=GetWindowThreadProcessId(new IntPtr(h),out ignored);
                var state=new GuiThreadState(); state.Size=Marshal.SizeOf(typeof(GuiThreadState));
                if(!GetGUIThreadInfo(thread,out state)) throw new Fault("focus_unverified","Could not verify menu ownership.");
                if(state.MenuOwner==IntPtr.Zero || (state.Flags & 0x1C)==0)
                    return D("status","no_standard_menu_observed","window",h,"input_sent",false,
                        "next_action","find_popup_or_use_explicit_ESC","custom_popup_supported",false);
                if(!Within(h,state.MenuOwner.ToInt64()) || Pid(state.MenuOwner.ToInt64())!=pid)
                    throw new Fault("invalid_target","Menu owner is outside the selected window.");
                IntPtr result;
                if(SendTimeout(state.MenuOwner,0x001F,IntPtr.Zero,IntPtr.Zero,2,250,out result)==IntPtr.Zero)
                    throw new Fault("outcome_unknown","WM_CANCELMODE did not return; inspect the menu, no retry.");
                return D("status","executed_unverified","window",h,"backend","WM_CANCELMODE","input_sent",false,
                    "next_action","find_refresh","custom_popup_supported",false);
            }
            throw new Fault("unknown_operation","Unknown recovery operation.");
        }
    }
}
