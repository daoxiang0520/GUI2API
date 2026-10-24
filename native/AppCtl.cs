using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Automation;

namespace AppCtl {
    public class Fault : Exception {
        public readonly string Code;
        public Fault(string code, string message) : base(message) { Code = code; }
    }
    class Reference {
        public AutomationElement Element;
        public long Window;
        public int Pid;
        public string Runtime;
        public DateTime Seen;
    }
    class Snapshot {
        public string Scope;
        public Dictionary<string, Dictionary<string, object>> Rows;
    }
    public static partial class Worker {
        delegate bool WindowCallback(IntPtr hwnd, IntPtr arg);
        [DllImport("user32.dll")] static extern bool EnumWindows(WindowCallback cb, IntPtr arg);
        [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
        [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
        [DllImport("user32.dll")] static extern bool ShowWindowAsync(IntPtr hwnd, int command);
        [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hwnd);
        [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out int pid);
        static readonly Dictionary<string, Reference> Refs = new Dictionary<string, Reference>();
        static readonly Dictionary<string, Snapshot> Snapshots = new Dictionary<string, Snapshot>();
        static readonly Queue<string> SnapshotOrder = new Queue<string>();
        static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 8 * 1024 * 1024, RecursionLimit = 100 };
        static readonly AutomationPattern[] Patterns = { InvokePattern.Pattern, ValuePattern.Pattern, TextPattern.Pattern, RangeValuePattern.Pattern, SelectionItemPattern.Pattern, TogglePattern.Pattern, ExpandCollapsePattern.Pattern, ScrollPattern.Pattern };
        static string S(Dictionary<string, object> a, string key, string fallback = "") { object v; return a.TryGetValue(key, out v) && v != null ? Convert.ToString(v) : fallback; }
        static int I(Dictionary<string, object> a, string key, int fallback) { object v; return a.TryGetValue(key, out v) ? Convert.ToInt32(v) : fallback; }
        static bool B(Dictionary<string, object> a, string key, bool fallback) { object v; return a.TryGetValue(key, out v) ? Convert.ToBoolean(v) : fallback; }
        static Dictionary<string, object> D(params object[] fields) {
            var d = new Dictionary<string, object>();
            for (int i=0; i<fields.Length; i+=2) d.Add((string)fields[i], fields[i+1]);
            return d;
        }
        static string Clip(string value, int limit) { return value == null ? "" : value.Length > limit ? value.Substring(0, limit) + "…" : value; }
        static string Runtime(AutomationElement e) { return string.Join(".", e.GetRuntimeId().Select(x => x.ToString()).ToArray()); }
        static long Hwnd(Dictionary<string, object> a) {
            long h;
            if (!long.TryParse(S(a,"window"), out h) || h <= 0 || !IsWindow(new IntPtr(h))) throw new Fault("window_not_found", "Specify a current window handle from apps.");
            return h;
        }
        static int Pid(long h) { int p; GetWindowThreadProcessId(new IntPtr(h), out p); return p; }
        static string SaveRef(AutomationElement e, long window) {
            string runtime = Runtime(e);
            string id = "w" + window + ":" + runtime;
            Refs[id] = new Reference { Element=e, Window=window, Pid=Pid(window), Runtime=runtime, Seen=DateTime.UtcNow };
            return id;
        }
        static CacheRequest Cache() {
            var c = new CacheRequest { TreeScope=TreeScope.Element };
            foreach (var p in new[] { AutomationElement.NameProperty, AutomationElement.AutomationIdProperty, AutomationElement.ControlTypeProperty, AutomationElement.IsEnabledProperty, AutomationElement.IsOffscreenProperty, AutomationElement.IsPasswordProperty, AutomationElement.BoundingRectangleProperty, AutomationElement.FrameworkIdProperty, AutomationElement.ClassNameProperty, AutomationElement.IsKeyboardFocusableProperty }) c.Add(p);
            foreach (var p in Patterns) c.Add(p);
            c.Add(ValuePattern.IsReadOnlyProperty);
            c.Add(RangeValuePattern.IsReadOnlyProperty);
            return c;
        }
        static object Pattern(AutomationElement e, AutomationPattern p, bool cached=false) {
            object value;
            bool found = cached ? e.TryGetCachedPattern(p, out value) : e.TryGetCurrentPattern(p, out value);
            return found ? value : null;
        }
        static AutomationElement Unique(AutomationElement root, string id, string name, string role) {
            var conditions = new List<Condition>();
            if (id.Length>0) conditions.Add(new PropertyCondition(AutomationElement.AutomationIdProperty,id));
            if (name.Length>0) conditions.Add(new PropertyCondition(AutomationElement.NameProperty,name));
            if (conditions.Count==0) throw new Fault("invalid_target", "Use ref, automation_id, or name; window alone cannot select an action target.");
            Condition condition = conditions.Count==1 ? conditions[0] : new AndCondition(conditions.ToArray());
            var found = root.FindAll(TreeScope.Descendants,condition);
            var matches = new List<AutomationElement>();
            foreach (AutomationElement e in found) if (role.Length==0 || string.Equals(e.Current.ControlType.ProgrammaticName.Substring(12),role,StringComparison.OrdinalIgnoreCase)) matches.Add(e);
            if (matches.Count==0) throw new Fault("element_not_found", "No matching control; inspect the current window.");
            if (matches.Count!=1) throw new Fault("ambiguous_target", "Multiple controls match. Add within, name, role, or use an inspected ref.");
            return matches[0];
        }
        static AutomationElement Resolve(Dictionary<string,object> a, out long window) {
            string id = S(a,"ref");
            if (id.Length>0) {
                Reference r;
                if (!Refs.TryGetValue(id,out r) || (DateTime.UtcNow-r.Seen).TotalMinutes>10) throw new Fault("stale_ref", "References belong to this session and expire after ten minutes. Inspect again.");
                window=r.Window;
                if (!IsWindow(new IntPtr(window)) || Pid(window)!=r.Pid || Runtime(r.Element)!=r.Runtime) throw new Fault("stale_ref", "The target has been replaced; inspect again.");
                return r.Element;
            }
            window=Hwnd(a);
            var root=AutomationElement.FromHandle(new IntPtr(window));
            string within=S(a,"within");
            if (within.Length>0) root=Unique(root,within,"","");
            return Unique(root,S(a,"automation_id"),S(a,"name"),S(a,"role"));
        }
        static object Windows() {
            var rows = new List<object>();
            EnumWindows(delegate(IntPtr h,IntPtr unused) {
                if (!IsWindowVisible(h)) return true;
                var text=new StringBuilder(2048); GetWindowText(h,text,text.Capacity);
                if (text.Length==0) return true;
                int pid=Pid(h.ToInt64()); string process="";
                try { using(var p=Process.GetProcessById(pid)) process=p.ProcessName; } catch { }
                rows.Add(D("window",h.ToInt64(),"pid",pid,"app",process,"title",text.ToString()));
                return true;
            },IntPtr.Zero);
            return D("windows",rows,"desktop",DesktopContext());
        }
        static object WindowState(Dictionary<string,object> a) {
            long h=Hwnd(a); int pid=Pid(h);
            if (a.ContainsKey("pid") && Convert.ToInt32(a["pid"])!=pid) throw new Fault("window_replaced","The window belongs to a different process.");
            string state=S(a,"state","get");
            if (state=="get") return D("window",h,"pid",pid,"minimized",IsIconic(new IntPtr(h)),"visible",IsWindowVisible(new IntPtr(h)),"foreground",GetForegroundWindow()==new IntPtr(h));
            if (state=="activate") {
                if(IsIconic(new IntPtr(h))) ShowWindowAsync(new IntPtr(h),9);
                else if(!IsWindowVisible(new IntPtr(h))) ShowWindowAsync(new IntPtr(h),5);
                for(int i=0;i<20 && IsIconic(new IntPtr(h));i++) System.Threading.Thread.Sleep(25);
                bool accepted=SetForegroundWindow(new IntPtr(h));
                if(accepted) for(int i=0;i<8 && GetForegroundWindow()!=new IntPtr(h);i++) System.Threading.Thread.Sleep(25);
                return D("window",h,"pid",pid,"minimized",IsIconic(new IntPtr(h)),"visible",IsWindowVisible(new IntPtr(h)),"foreground",GetForegroundWindow()==new IntPtr(h),"status",GetForegroundWindow()==new IntPtr(h) && IsWindowVisible(new IntPtr(h)) ? "verified" : "executed_unverified","activation_accepted",accepted);
            }
            if (state!="restore" && state!="minimize") throw new Fault("invalid_arguments","state must be get, restore, minimize, or activate.");
            ShowWindowAsync(new IntPtr(h),state=="restore" ? 9 : 6);
            bool desired=state=="minimize";
            for(int i=0;i<20 && IsIconic(new IntPtr(h))!=desired;i++) System.Threading.Thread.Sleep(25);
            bool actual=IsIconic(new IntPtr(h));
            return D("window",h,"pid",pid,"minimized",actual,"visible",IsWindowVisible(new IntPtr(h)),"status",actual==desired && (desired || IsWindowVisible(new IntPtr(h))) ? "verified" : "executed_unverified");
        }
        class Frame { public AutomationElement E; public string Parent; public int Depth; public bool Chrome; }
        static object Inspect(Dictionary<string,object> a) {
            long h=Hwnd(a); int depth=I(a,"depth",6), limit=I(a,"limit",300);
            if (depth<0 || depth>20 || limit<1 || limit>3000) throw new Fault("invalid_arguments","depth must be 0..20 and limit 1..3000.");
            bool includeValues=B(a,"values",false);
            string view=S(a,"view","control");
            if (view!="control" && view!="raw") throw new Fault("invalid_arguments","view must be control or raw.");
            string scope=h+"/"+S(a,"within")+"/"+depth+"/"+limit+"/"+includeValues+"/"+view;
            string since=S(a,"since"); Snapshot previous=null;
            if (since.Length>0 && (!Snapshots.TryGetValue(since,out previous) || previous.Scope!=scope)) throw new Fault("unknown_snapshot","Use a snapshot from this session with the same window, scope, depth, limit, and values option.");
            var cache=Cache(); var root=AutomationElement.FromHandle(new IntPtr(h));
            if(S(a,"root_ref").Length>0) { long bound; root=Resolve(D("ref",S(a,"root_ref")),out bound); if(bound!=h) throw new Fault("invalid_target","The scope belongs to a different window."); }
            if (S(a,"within").Length>0) root=Unique(root,S(a,"within"),"","");
            root=root.GetUpdatedCache(cache);
            var queue=new Queue<Frame>(); queue.Enqueue(new Frame { E=root,Depth=0,Parent=null });
            var rows=new List<Dictionary<string,object>>(); var all=new Dictionary<string,Dictionary<string,object>>();
            var walker=view=="raw" ? TreeWalker.RawViewWalker : TreeWalker.ControlViewWalker; bool actionable=B(a,"actionable",true); int skipped=0;
            while (queue.Count>0 && all.Count<limit) {
                Frame frame=queue.Dequeue(); var e=frame.E;
                try {
                    string id=SaveRef(e,h); var info=e.Cached; var actions=new List<string>();
                    if(info.ControlType==null) { skipped++; continue; }
                    bool chrome=frame.Chrome || info.ControlType==ControlType.TitleBar;
                    bool password=info.IsPassword;
                    if (!password) actions.Add("get");
                    if (info.IsKeyboardFocusable) actions.Add("focus");
                    if (Pattern(e,InvokePattern.Pattern,true)!=null) actions.Add("invoke");
                    var vp=Pattern(e,ValuePattern.Pattern,true) as ValuePattern;
                    var rp=Pattern(e,RangeValuePattern.Pattern,true) as RangeValuePattern;
                    if (!password && ((vp!=null && !vp.Cached.IsReadOnly) || (rp!=null && !rp.Cached.IsReadOnly))) actions.Add("set");
                    if (Pattern(e,SelectionItemPattern.Pattern,true)!=null) actions.Add("select");
                    if (Pattern(e,TogglePattern.Pattern,true)!=null) { actions.Add("toggle-on"); actions.Add("toggle-off"); }
                    if (Pattern(e,ExpandCollapsePattern.Pattern,true)!=null) { actions.Add("expand"); actions.Add("collapse"); }
                    if (Pattern(e,ScrollPattern.Pattern,true)!=null) actions.Add("scroll");
                    var rect=info.BoundingRectangle;
                    var row=D("ref",id,"parent",frame.Parent,"role",info.ControlType.ProgrammaticName.Substring(12),"name",Clip(info.Name,160),"automation_id",info.AutomationId,"enabled",info.IsEnabled,"offscreen",info.IsOffscreen,"password",password,"actions",actions,"framework",info.FrameworkId,"class_name",info.ClassName,"region",chrome ? "window_chrome" : "content","rect",D("x",Finite(rect.X),"y",Finite(rect.Y),"width",Finite(rect.Width),"height",Finite(rect.Height)));
                    if (includeValues && !password) {
                        if (vp!=null) {
                            string v=vp.Current.Value;
                            row["value"]=v.Length>160 ? v.Substring(0,160) : v;
                            row["value_truncated"]=v.Length>160;
                        } else if (rp!=null) row["value"]=rp.Current.Value;
                    }
                    all[id]=row;
                    if (!actionable || actions.Count>1 || frame.Depth==0) rows.Add(row);
                    if (frame.Depth<depth) {
                        var child=walker.GetFirstChild(e,cache);
                        while (child!=null && queue.Count+all.Count<limit) {
                            queue.Enqueue(new Frame { E=child,Parent=id,Depth=frame.Depth+1,Chrome=chrome });
                            child=walker.GetNextSibling(child,cache);
                        }
                        if (child!=null) skipped++;
                    }
                } catch (ElementNotAvailableException) { skipped++; }
                  catch (COMException) { skipped++; }
                  catch (NullReferenceException) { skipped++; }
            }
            string snapshot=Guid.NewGuid().ToString("N");
            Snapshots[snapshot]=new Snapshot { Scope=scope,Rows=all }; SnapshotOrder.Enqueue(snapshot);
            while (SnapshotOrder.Count>16) Snapshots.Remove(SnapshotOrder.Dequeue());
            foreach (string key in Refs.Where(x=>(DateTime.UtcNow-x.Value.Seen).TotalMinutes>10).Select(x=>x.Key).ToArray()) Refs.Remove(key);
            if (since.Length>0) {
                var old=previous.Rows;
                var added=all.Where(x=>!old.ContainsKey(x.Key)).Select(x=>x.Value).ToArray();
                var removed=old.Keys.Where(x=>!all.ContainsKey(x)).ToArray();
                var changed=all.Where(x=>old.ContainsKey(x.Key) && Json.Serialize(x.Value)!=Json.Serialize(old[x.Key])).Select(x=>x.Value).ToArray();
                return D("snapshot",snapshot,"window",h,"view",view,"added",added,"removed",removed,"changed",changed,"visited",all.Count,"truncated",skipped>0 || queue.Count>0);
            }
            return D("snapshot",snapshot,"window",h,"view",view,"elements",rows,"visited",all.Count,"truncated",skipped>0 || queue.Count>0,"depth_limit",depth);
        }
        static object Read(AutomationElement e,int limit) {
            if (e.Current.IsPassword) throw new Fault("protected_control","Password controls are not exposed by this interface.");
            var value=Pattern(e,ValuePattern.Pattern) as ValuePattern;
            if (value!=null) {
                string v=value.Current.Value;
                return D("value",v.Length>limit ? v.Substring(0,limit) : v,"source","ValuePattern","truncated",v.Length>limit);
            }
            var range=Pattern(e,RangeValuePattern.Pattern) as RangeValuePattern;
            if (range!=null) return D("value",range.Current.Value,"source","RangeValuePattern");
            var text=Pattern(e,TextPattern.Pattern) as TextPattern;
            if (text!=null) {
                string v=text.DocumentRange.GetText(limit+1);
                return D("value",v.Length>limit ? v.Substring(0,limit) : v,"source","TextPattern","truncated",v.Length>limit);
            }
            return D("value",Clip(e.Current.Name,limit),"source","Name","label_only",true);
        }
        static object Action(string op, Dictionary<string,object> a) {
            long h; var e=Resolve(a,out h);
            if(op=="input_target") {
                var c=e.Current; var r=c.BoundingRectangle;
                var v=Pattern(e,ValuePattern.Pattern) as ValuePattern;
                return InputTarget(h,D("x",r.X,"y",r.Y,"width",r.Width,"height",r.Height),c.ControlType.ProgrammaticName.Substring(12),c.IsEnabled,c.IsOffscreen,c.IsPassword,c.IsKeyboardFocusable,c.HasKeyboardFocus,v!=null && v.Current.IsReadOnly,c.Name);
            }
            if (op=="get") return Read(e,Math.Min(Math.Max(I(a,"max_length",4096),1),100000));
            if (!e.Current.IsEnabled) throw new Fault("disabled_control","The target control is disabled.");
            if (op=="focus") { e.SetFocus(); return D("status",e.Current.HasKeyboardFocus ? "verified" : "executed_unverified","backend","UIA.SetFocus"); }
            if (op=="set") {
                if (e.Current.IsPassword) throw new Fault("protected_control","Password controls are not exposed by this interface.");
                if (!a.ContainsKey("value")) throw new Fault("invalid_arguments","set requires value, including an explicit empty string when clearing.");
                var value=Pattern(e,ValuePattern.Pattern) as ValuePattern;
                if (value!=null && !value.Current.IsReadOnly) {
                    string desired=S(a,"value"); value.SetValue(desired);
                    string actual=value.Current.Value;
                    return D("status",actual==desired ? "verified" : "verification_failed","value",actual,"backend","ValuePattern");
                }
                var range=Pattern(e,RangeValuePattern.Pattern) as RangeValuePattern;
                if (range!=null && !range.Current.IsReadOnly) {
                    double desired=Convert.ToDouble(a["value"],System.Globalization.CultureInfo.InvariantCulture); range.SetValue(desired);
                    double actual=range.Current.Value;
                    return D("status",actual==desired ? "verified" : "verification_failed","value",actual,"backend","RangeValuePattern");
                }
            } else if (op=="invoke") {
                var p=Pattern(e,InvokePattern.Pattern) as InvokePattern;
                if (p!=null) { p.Invoke(); return D("status","executed_unverified","backend","InvokePattern"); }
            } else if (op=="select") {
                var p=Pattern(e,SelectionItemPattern.Pattern) as SelectionItemPattern;
                if (p!=null) { p.Select(); return D("status",p.Current.IsSelected ? "verified" : "verification_failed","backend","SelectionItemPattern"); }
            } else if (op=="toggle-on" || op=="toggle-off") {
                var p=Pattern(e,TogglePattern.Pattern) as TogglePattern;
                if (p!=null) {
                    ToggleState desired=op=="toggle-on" ? ToggleState.On : ToggleState.Off;
                    for (int i=0;i<2 && p.Current.ToggleState!=desired;i++) p.Toggle();
                    return D("status",p.Current.ToggleState==desired ? "verified" : "verification_failed","state",p.Current.ToggleState.ToString(),"backend","TogglePattern");
                }
            } else if (op=="expand" || op=="collapse") {
                var p=Pattern(e,ExpandCollapsePattern.Pattern) as ExpandCollapsePattern;
                if (p!=null) {
                    if(op=="expand") p.Expand(); else p.Collapse();
                    var desired=op=="expand" ? ExpandCollapseState.Expanded : ExpandCollapseState.Collapsed;
                    return D("status",p.Current.ExpandCollapseState==desired ? "verified" : "verification_failed","backend","ExpandCollapsePattern");
                }
            } else if (op=="scroll") {
                var p=Pattern(e,ScrollPattern.Pattern) as ScrollPattern;
                if (p!=null) {
                    string direction=S(a,"direction");
                    if (direction=="up") p.Scroll(ScrollAmount.NoAmount,ScrollAmount.SmallDecrement);
                    else if (direction=="down") p.Scroll(ScrollAmount.NoAmount,ScrollAmount.SmallIncrement);
                    else if (direction=="left") p.Scroll(ScrollAmount.SmallDecrement,ScrollAmount.NoAmount);
                    else if (direction=="right") p.Scroll(ScrollAmount.SmallIncrement,ScrollAmount.NoAmount);
                    else throw new Fault("invalid_arguments","direction must be up, down, left, or right.");
                    return D("status","executed_unverified","backend","ScrollPattern");
                }
            }
            throw new Fault("unsupported","This control does not expose the requested UIA pattern. No image or coordinate fallback was attempted.");
        }
        static object Dispatch(string op,Dictionary<string,object> a,string backend) {
            bool input=new[]{"set","invoke","focus","select","toggle-on","toggle-off","expand","collapse","scroll","dismiss_popup"}.Contains(op) || (op=="window" && S(a,"state","get")!="get");
            if(input) { var desktop=DesktopContext(); if(!Convert.ToBoolean(desktop["interactive_confirmed"])) throw new Fault("desktop_not_interactive","The unlocked interactive desktop was not confirmed. No app input was issued."); }
            if (op=="ping") return D("ready",true,"protocol","appctl/2","screenshots",false,"session",Process.GetCurrentProcess().Id,"desktop",DesktopContext());
            if (op=="apps") return Windows();
            if (op=="window") return WindowState(a);
            if (op=="request_accessibility" || op=="dismiss_popup") return Recovery(op,a);
            if (op=="describe") return Describe(a);
            if (op=="watch") return Watch(a,backend);
            if (op=="events") return Events(a);
            if (backend=="uia3") return Uia3Dispatch(op,a);
            if (backend=="msaa") return MsaaDispatch(op,a);
            if (backend=="win32") return Win32Dispatch(op,a);
            if (op=="inspect") return Inspect(a);
            if (new[] { "get","set","invoke","focus","select","toggle-on","toggle-off","expand","collapse","scroll","input_target" }.Contains(op)) return Action(op,a);
            throw new Fault("unknown_command","Unknown command: "+op);
        }
        public static void Run(string backend) {
            // Match accessibility bounds, Win32 geometry and the OS input driver.
            SetThreadDpiAwarenessContext(new IntPtr(-4));
            Console.InputEncoding=new UTF8Encoding(false); Console.OutputEncoding=new UTF8Encoding(false);
            string line;
            while ((line=Console.ReadLine())!=null) {
                var clock=Stopwatch.StartNew(); object id=null; Dictionary<string,object> response; string op="";
                try {
                    if(line.Length>1024*1024) throw new Fault("request_too_large","Requests must be smaller than 1 MiB.");
                    var request=Json.Deserialize<Dictionary<string,object>>(line);
                    object raw; if(request.TryGetValue("id",out raw)) id=raw;
                    var args=request.TryGetValue("args",out raw) ? raw as Dictionary<string,object> : new Dictionary<string,object>();
                    if(args==null) throw new Fault("invalid_arguments","args must be an object.");
                    op=S(request,"op"); object data=Dispatch(op,args,backend);
                    var state=data as Dictionary<string,object>;
                    bool verifiedFailed=state!=null && state.ContainsKey("status") && (string)state["status"]=="verification_failed";
                    response=D("id",id,"ok",!verifiedFailed,"data",data);
                    if (verifiedFailed) response["error"]=D("code","verification_failed","message","The action ran but the expected state was not observed. Do not blindly repeat it.");
                } catch (Exception ex) {
                    var fault=ex as Fault;
                    string code=fault!=null ? fault.Code : ex is ElementNotAvailableException ? "stale_ref" : ex is UnauthorizedAccessException ? "access_denied" : ex is ArgumentException || ex is FormatException || ex is InvalidCastException ? "invalid_arguments" : backend+"_error";
                    bool mutation=new[]{"set","invoke","focus","select","toggle-on","toggle-off","expand","collapse","scroll","window","dismiss_popup"}.Contains(op);
                    if(mutation && (fault==null || code=="provider_timeout")) code="outcome_unknown";
                    response=D("id",id,"ok",false,"error",D("code",code,"message",ex.Message,"type",ex.GetType().FullName,"location",ex.StackTrace==null ? "" : Clip(ex.StackTrace,1500)));
                }
                response["elapsed_ms"]=Math.Round(clock.Elapsed.TotalMilliseconds,2);
                response["backend"]="native-"+backend;
                Console.WriteLine(Json.Serialize(response)); Console.Out.Flush();
            }
        }
    }
}
