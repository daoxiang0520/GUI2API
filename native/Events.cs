using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Automation;

namespace AppCtl {
    public static partial class Worker {
        static readonly object EventLock=new object();
        static readonly Queue<object> EventQueue=new Queue<object>();
        static readonly AutoResetEvent EventReady=new AutoResetEvent(false);
        static int DroppedEvents;
        static AutomationElement WatchRoot;
        static Thread HookThread;
        static uint HookThreadId;
        static long WatchWindow;
        delegate void WinEventCallback(IntPtr hook,uint eventId,IntPtr hwnd,int objectId,int childId,uint thread,uint time);
        static WinEventCallback WinCallback;
        [DllImport("user32.dll")] static extern IntPtr SetWinEventHook(uint first,uint last,IntPtr module,WinEventCallback cb,uint pid,uint tid,uint flags);
        [DllImport("user32.dll")] static extern bool UnhookWinEvent(IntPtr hook);
        [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
        [StructLayout(LayoutKind.Sequential)] struct NativeMessage { public IntPtr H; public uint Message; public UIntPtr W; public IntPtr L; public uint Time; public int X,Y; public uint Private; }
        [DllImport("user32.dll")] static extern int GetMessage(out NativeMessage msg,IntPtr hwnd,uint first,uint last);
        [DllImport("user32.dll")] static extern bool PeekMessage(out NativeMessage msg,IntPtr hwnd,uint first,uint last,uint remove);
        [DllImport("user32.dll")] static extern bool PostThreadMessage(uint tid,uint msg,UIntPtr w,IntPtr l);
        static void PushEvent(object data) {
            lock(EventLock) { if(EventQueue.Count>=1024) { EventQueue.Dequeue(); DroppedEvents++; } EventQueue.Enqueue(data); }
            EventReady.Set();
        }
        static object Watch(Dictionary<string,object> a,string backend) {
            if(WatchRoot!=null) { Automation.RemoveAllEventHandlers(); WatchRoot=null; }
            if(HookThread!=null) { PostThreadMessage(HookThreadId,18,UIntPtr.Zero,IntPtr.Zero); HookThread.Join(1000); HookThread=null; }
            lock(EventLock) { EventQueue.Clear(); DroppedEvents=0; }
            if(!B(a,"enabled",true)) return D("watching",false);
            long window=Hwnd(a); WatchWindow=window;
            if(backend=="uia") {
                WatchRoot=AutomationElement.FromHandle(new IntPtr(window));
                Automation.AddStructureChangedEventHandler(WatchRoot,TreeScope.Subtree,delegate(object sender,StructureChangedEventArgs e){ PushEvent(D("kind","structure","window",window,"change",e.StructureChangeType.ToString())); });
                Automation.AddAutomationPropertyChangedEventHandler(WatchRoot,TreeScope.Subtree,delegate(object sender,AutomationPropertyChangedEventArgs e){ PushEvent(D("kind","property","window",window,"property",e.Property.ProgrammaticName)); },AutomationElement.NameProperty,AutomationElement.IsEnabledProperty,ValuePattern.ValueProperty,TogglePattern.ToggleStateProperty,SelectionItemPattern.IsSelectedProperty);
                Automation.AddAutomationEventHandler(TextPattern.TextChangedEvent,WatchRoot,TreeScope.Subtree,delegate(object sender,AutomationEventArgs e){ PushEvent(D("kind","text","window",window)); });
                return D("watching",true,"window",window,"source","UIA events","contains_values",false);
            }
            int owner=Pid(window); var ready=new ManualResetEvent(false); string failure=null;
            HookThread=new Thread(delegate(){
                HookThreadId=GetCurrentThreadId(); NativeMessage msg; PeekMessage(out msg,IntPtr.Zero,0,0,0);
                WinCallback=delegate(IntPtr hook,uint ev,IntPtr h,int obj,int child,uint tid,uint time){ if(h!=IntPtr.Zero && Within(window,h.ToInt64())) PushEvent(D("kind","win_event","window",window,"hwnd",h.ToInt64(),"event_id",ev,"object_id",obj,"child_id",child)); };
                IntPtr token=SetWinEventHook(0x8000,0x8019,IntPtr.Zero,WinCallback,(uint)owner,0,0);
                if(token==IntPtr.Zero) failure="SetWinEventHook failed."; ready.Set();
                if(token==IntPtr.Zero) return;
                try { while(GetMessage(out msg,IntPtr.Zero,0,0)>0) {} } finally { UnhookWinEvent(token); }
            });
            HookThread.IsBackground=true; HookThread.SetApartmentState(ApartmentState.MTA); HookThread.Start();
            if(!ready.WaitOne(2000) || failure!=null) throw new Fault("events_unavailable",failure ?? "WinEvent subscription timed out.");
            return D("watching",true,"window",window,"source","WinEventHook","contains_values",false);
        }
        static object Events(Dictionary<string,object> a) {
            int wait=I(a,"timeout_ms",0),limit=I(a,"limit",100); if(wait<0 || wait>5000 || limit<1 || limit>1024) throw new Fault("invalid_arguments","timeout_ms must be 0..5000 and limit 1..1024.");
            bool empty; lock(EventLock) empty=EventQueue.Count==0;
            if(empty && wait>0) EventReady.WaitOne(wait);
            var rows=new List<object>(); int dropped;
            lock(EventLock) { while(EventQueue.Count>0 && rows.Count<limit) rows.Add(EventQueue.Dequeue()); dropped=DroppedEvents; DroppedEvents=0; }
            return D("events",rows,"dropped",dropped,"invalidated",rows.Count>0 || dropped>0,"window",WatchWindow);
        }
    }
}
