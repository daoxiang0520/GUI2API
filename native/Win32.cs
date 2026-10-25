using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;

namespace AppCtl {
    public static partial class Worker {
        [StructLayout(LayoutKind.Sequential)] struct Rect { public int Left,Top,Right,Bottom; }
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h,StringBuilder b,int n);
        [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr h,WindowCallback cb,IntPtr p);
        [DllImport("user32.dll")] static extern IntPtr GetParent(IntPtr h);
        [DllImport("user32.dll")] static extern bool IsChild(IntPtr parent,IntPtr child);
        [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr h);
        [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h,out Rect rect);
        [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr h,out Rect rect);
        [StructLayout(LayoutKind.Sequential)] struct Point { public int X,Y; }
        [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr h,ref Point point);
        [DllImport("user32.dll")] static extern int GetDlgCtrlID(IntPtr h);
        [DllImport("user32.dll",EntryPoint="GetWindowLongW")] static extern int GetWindowLong(IntPtr h,int index);
        [DllImport("user32.dll",EntryPoint="SendMessageTimeoutW",SetLastError=true)] static extern IntPtr SendTimeout(IntPtr h,uint m,IntPtr w,IntPtr l,uint flags,uint timeout,out IntPtr result);
        [DllImport("user32.dll",EntryPoint="SendMessageTimeoutW",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr SendTextTimeout(IntPtr h,uint m,IntPtr w,StringBuilder l,uint flags,uint timeout,out IntPtr result);
        [DllImport("iphlpapi.dll")] static extern uint GetExtendedTcpTable(IntPtr table,ref int size,bool order,int family,int kind,uint reserved);
        static double Finite(double x) { return double.IsNaN(x) || double.IsInfinity(x) ? 0 : x; }
        static string Class(long h) { var b=new StringBuilder(256); GetClassName(new IntPtr(h),b,b.Capacity); return b.ToString(); }
        static string Caption(long h) { var b=new StringBuilder(512); GetWindowText(new IntPtr(h),b,b.Capacity); return b.ToString(); }
        static bool Within(long root,long h) { return root==h || IsChild(new IntPtr(root),new IntPtr(h)); }
        static Dictionary<string,object> Bounds(long h) { Rect r; GetWindowRect(new IntPtr(h),out r); return D("x",r.Left,"y",r.Top,"width",r.Right-r.Left,"height",r.Bottom-r.Top); }
        static bool NonClient(long window,int x,int y,int width,int height) {
            if(width<=0 || height<=0) return false;
            Rect client; var origin=new Point();
            if(!GetClientRect(new IntPtr(window),out client) || !ClientToScreen(new IntPtr(window),ref origin)) return false;
            return x+width<=origin.X || y+height<=origin.Y || x>=origin.X+client.Right || y>=origin.Y+client.Bottom;
        }
        static List<long> ChildHandles(long root,int limit) {
            var rows=new List<long>{root};
            EnumChildWindows(new IntPtr(root),delegate(IntPtr h,IntPtr p){ if(rows.Count>=limit) return false; rows.Add(h.ToInt64()); return true; },IntPtr.Zero);
            return rows;
        }
        static int ChildDepth(long root,long h) { int n=0; while(h!=root && h!=0 && n<64) { h=GetParent(new IntPtr(h)).ToInt64(); n++; } return n; }
        static long Message(long h,uint message,long value=0) {
            IntPtr result;
            if(SendTimeout(new IntPtr(h),message,new IntPtr(value),IntPtr.Zero,2,250,out result)==IntPtr.Zero) throw new Fault("provider_timeout","The window did not answer within 250 ms. No retry was made.");
            return result.ToInt64();
        }
        static string WindowText(long h,int limit) {
            var b=new StringBuilder(limit+1); IntPtr result;
            if(SendTextTimeout(new IntPtr(h),13,new IntPtr(b.Capacity),b,2,250,out result)==IntPtr.Zero) throw new Fault("provider_timeout","WM_GETTEXT timed out. No retry was made.");
            return b.ToString();
        }
        static string StandardClass(string c) { return c.StartsWith("WindowsForms10.",StringComparison.OrdinalIgnoreCase) && c.Split('.').Length>1 ? c.Split('.')[1] : c; }
        static bool EditClass(string c) { c=StandardClass(c); return c.Equals("Edit",StringComparison.OrdinalIgnoreCase) || c.StartsWith("RichEdit",StringComparison.OrdinalIgnoreCase); }
        static string WinRole(string c,int style) {
            c=StandardClass(c);
            if(EditClass(c)) return "Edit";
            if(c.Equals("Button",StringComparison.OrdinalIgnoreCase)) { int kind=style&15; return kind==2 || kind==3 || kind==5 || kind==6 ? "CheckBox" : kind==4 || kind==9 ? "RadioButton" : kind==7 ? "Group" : "Button"; }
            if(c.Equals("Static",StringComparison.OrdinalIgnoreCase)) return "Text";
            if(c.Equals("ComboBox",StringComparison.OrdinalIgnoreCase)) return "ComboBox";
            if(c.Equals("ListBox",StringComparison.OrdinalIgnoreCase) || c=="SysListView32") return "List";
            if(c=="SysTreeView32") return "Tree";
            return "Pane";
        }
        class WinRef { public long Window,H; public int Pid,ControlId; public string Class; public DateTime Seen; }
        static readonly Dictionary<string,WinRef> WinRefs=new Dictionary<string,WinRef>();
        static WinRef ResolveWin(string id) {
            WinRef r;
            if(!WinRefs.TryGetValue(id,out r) || (DateTime.UtcNow-r.Seen).TotalMinutes>10 || !IsWindow(new IntPtr(r.H)) || Pid(r.H)!=r.Pid || !Within(r.Window,r.H) || Class(r.H)!=r.Class || GetDlgCtrlID(new IntPtr(r.H))!=r.ControlId) throw new Fault("stale_ref","Inspect again before using this Win32 reference.");
            return r;
        }
        static object WinInspect(Dictionary<string,object> a) {
            long window=Hwnd(a),root=window; int limit=I(a,"limit",300),depth=I(a,"depth",6);
            if(limit<1 || limit>3000 || depth<0 || depth>20) throw new Fault("invalid_arguments","depth must be 0..20 and limit 1..3000.");
            if(S(a,"root_ref").Length>0) {
                var bound=ResolveWin(S(a,"root_ref"));
                if(bound.Window!=window) throw new Fault("invalid_target","The scope belongs to a different window.");
                root=bound.H;
            }
            var handles=ChildHandles(root,limit+1); var rows=new List<object>();
            foreach(long h in handles.Take(limit)) {
                if(ChildDepth(root,h)>depth) continue;
                string c=Class(h), id="h"+h; int style=GetWindowLong(new IntPtr(h),-16); string role=WinRole(c,style);
                bool edit=EditClass(c), password=edit && (style&32)!=0, readOnly=edit && (style&2048)!=0;
                var actions=new List<string>(); if(!password) actions.Add("get");
                if(!password && (edit || role=="Button" || role=="CheckBox" || role=="ComboBox")) actions.Add("focus");
                if(edit && !password && !readOnly) actions.Add("set");
                if(role=="Button") actions.Add("invoke");
                if(role=="CheckBox") { actions.Add("toggle-on"); actions.Add("toggle-off"); }
                var row=D("ref",id,"parent",h==window ? null : "h"+GetParent(new IntPtr(h)).ToInt64(),"role",h==window ? "Window" : role,"name",password || edit ? "" : Clip(Caption(h),160),"class_name",c,"automation_id",GetDlgCtrlID(new IntPtr(h)).ToString(),"enabled",IsWindowEnabled(new IntPtr(h)),"offscreen",!IsWindowVisible(new IntPtr(h)),"password",password,"actions",actions,"region",h==window ? "unknown" : "content","rect",Bounds(h));
                row["hwnd"]=h;
                if(GetDlgCtrlID(new IntPtr(h))<=0) row["automation_id"]="";
                if(B(a,"values",false) && edit && !password) { try { row["value"]=WindowText(h,160); } catch(Fault) { row["value_unavailable"]=true; } }
                rows.Add(row); WinRefs[id]=new WinRef { Window=window,H=h,Pid=Pid(h),Class=c,ControlId=GetDlgCtrlID(new IntPtr(h)),Seen=DateTime.UtcNow };
            }
            return D("window",window,"elements",rows,"visited",rows.Count,"truncated",handles.Count>limit,"depth_limit",depth);
        }
        static object WinAction(string op,Dictionary<string,object> a) {
            WinRef r=ResolveWin(S(a,"ref"));
            long h=r.H; int style=GetWindowLong(new IntPtr(h),-16); bool edit=EditClass(r.Class);
            if(edit && (style&32)!=0) throw new Fault("protected_control","Password controls are not exposed.");
            if(op=="input_target" || op=="focus") {
                var e=System.Windows.Automation.AutomationElement.FromHandle(new IntPtr(h));
                if(op=="focus") { e.SetFocus(); return D("status",e.Current.HasKeyboardFocus ? "verified" : "executed_unverified","backend","UIA.SetFocus(HWND)"); }
                return InputTarget(r.Window,Bounds(h),WinRole(r.Class,style),IsWindowEnabled(new IntPtr(h)),!IsWindowVisible(new IntPtr(h)),false,e.Current.IsKeyboardFocusable,e.Current.HasKeyboardFocus,edit && (style&2048)!=0,e.Current.Name);
            }
            if(op=="get") return D("value",WindowText(h,Math.Min(100000,Math.Max(1,I(a,"max_length",4096)))),"source","WM_GETTEXT","label_only",!edit);
            if(!IsWindowEnabled(new IntPtr(h))) throw new Fault("disabled_control","The control is disabled.");
            if(op=="set" && edit && (style&2048)==0) {
                if(!a.ContainsKey("value")) throw new Fault("invalid_arguments","set requires an explicit value.");
                string wanted=S(a,"value"); if(wanted.Length>100000) throw new Fault("invalid_arguments","Value exceeds 100000 characters.");
                IntPtr result; var b=new StringBuilder(wanted);
                if(SendTextTimeout(new IntPtr(h),12,IntPtr.Zero,b,2,250,out result)==IntPtr.Zero) throw new Fault("outcome_unknown","WM_SETTEXT timed out; do not retry without observing.");
                string actual=WindowText(h,wanted.Length+1);
                return D("status",actual==wanted ? "verified" : "verification_failed","value",actual,"backend","WM_SETTEXT");
            }
            string role=WinRole(r.Class,style);
            if(op=="invoke" && role=="Button") { Message(h,245); return D("status","executed_unverified","backend","BM_CLICK"); }
            if((op=="toggle-on" || op=="toggle-off") && role=="CheckBox") {
                long wanted=op=="toggle-on" ? 1 : 0; if(Message(h,240)!=wanted) Message(h,245);
                long actual=Message(h,240); return D("status",actual==wanted ? "verified" : "verification_failed","state",actual,"backend","BM_CLICK");
            }
            throw new Fault("unsupported","The Win32 control does not support this semantic action.");
        }
        static List<object> Listeners(int pid) {
            var rows=new List<object>(); int size=0; GetExtendedTcpTable(IntPtr.Zero,ref size,false,2,3,0);
            if(size<=0) return rows; IntPtr buffer=Marshal.AllocHGlobal(size);
            try {
                if(GetExtendedTcpTable(buffer,ref size,false,2,3,0)!=0) return rows;
                int count=Marshal.ReadInt32(buffer);
                for(int i=0;i<count;i++) {
                    int p=4+i*24,owner=Marshal.ReadInt32(buffer,p+20); if(owner!=pid) continue;
                    int address=Marshal.ReadInt32(buffer,p+4); byte[] bytes=BitConverter.GetBytes(address);
                    if(address!=0 && bytes[0]!=127) continue;
                    int port=((Marshal.ReadInt32(buffer,p+8)&255)<<8)|((Marshal.ReadInt32(buffer,p+8)>>8)&255);
                    rows.Add(D("port",port,"pid",owner));
                }
            } finally { Marshal.FreeHGlobal(buffer); }
            return rows;
        }
        static object Describe(Dictionary<string,object> a) {
            long h=Hwnd(a); int pid=Pid(h); string exe=LimitedImageName(pid),app=""; var modules=new List<string>(); string moduleError="";
            try {
                using(var p=Process.GetProcessById(pid)) { app=p.ProcessName; exe=p.MainModule.FileName;
                    foreach(ProcessModule m in p.Modules) { string n=m.ModuleName; if(n.IndexOf("qt",StringComparison.OrdinalIgnoreCase)>=0 || n.IndexOf("chrome",StringComparison.OrdinalIgnoreCase)>=0 || n.IndexOf("cef",StringComparison.OrdinalIgnoreCase)>=0 || n.IndexOf("webview",StringComparison.OrdinalIgnoreCase)>=0 || n.IndexOf("jvm",StringComparison.OrdinalIgnoreCase)>=0 || n.IndexOf("accessbridge",StringComparison.OrdinalIgnoreCase)>=0 || n.IndexOf("presentationframework",StringComparison.OrdinalIgnoreCase)>=0 || n.IndexOf("clr",StringComparison.OrdinalIgnoreCase)>=0) modules.Add(n); }
                }
            } catch(Exception ex) { moduleError=ex.GetType().Name; }
            var classes=ChildHandles(h,256).Select(x=>Class(x)).Distinct().ToArray();
            return D("window",h,"pid",pid,"app",app,"exe",exe,"class_name",Class(h),"child_classes",classes,"framework_modules",modules.Distinct().ToArray(),"module_error",moduleError,"minimized",IsIconic(new IntPtr(h)),"foreground",GetForegroundWindow()==new IntPtr(h),"access_context",AccessContext(pid),"listeners",Listeners(pid));
        }
        static object Win32Dispatch(string op,Dictionary<string,object> a) {
            if(op=="inspect") return WinInspect(a);
            if(new[]{"get","set","invoke","focus","toggle-on","toggle-off","input_target"}.Contains(op)) return WinAction(op,a);
            throw new Fault("unsupported","This command is not supported by the Win32 driver.");
        }
    }
}
