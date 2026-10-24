using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows.Automation;
using U3=Interop.UIAutomationClient;

namespace AppCtl {
    public static partial class Worker {
        static U3.IUIAutomation NativeAutomation;
        static U3.IUIAutomation Engine3() {
            if(NativeAutomation==null) {
                var type=typeof(U3.IUIAutomation).Assembly.GetTypes().First(t=>t.IsClass && t.Name.StartsWith("CUIAutomation8"));
                NativeAutomation=(U3.IUIAutomation)Activator.CreateInstance(type);
                var config=(U3.IUIAutomation2)NativeAutomation;
                config.ConnectionTimeout=1000; config.TransactionTimeout=1200; config.AutoSetFocus=0;
            }
            return NativeAutomation;
        }
        class Ref3 { public U3.IUIAutomationElement E; public long Window; public int Pid; public string Runtime; public DateTime Seen; }
        class Frame3 { public U3.IUIAutomationElement E; public string Parent; public int Depth; public bool Chrome; }
        static readonly Dictionary<string,Ref3> Refs3=new Dictionary<string,Ref3>();
        static readonly int[] PatternIds3={10000,10002,10003,10004,10005,10010,10014,10015,10018};
        static string Runtime3(U3.IUIAutomationElement e) { var id=e.GetRuntimeId(); if(id==null) throw new Fault("stale_ref","The provider returned no runtime identity."); return string.Join(".",id.Cast<object>().Select(x=>Convert.ToString(x)).ToArray()); }
        static string CachedRuntime3(U3.IUIAutomationElement e) { try { var id=e.GetCachedPropertyValue(30000) as Array; if(id!=null) return string.Join(".",id.Cast<object>().Select(x=>Convert.ToString(x)).ToArray()); } catch(COMException) {} return Runtime3(e); }
        static string Role3(int id) { var type=ControlType.LookupById(id); return type==null ? "Custom" : type.ProgrammaticName.Substring(12); }
        static T Pattern3<T>(U3.IUIAutomationElement e,int id,bool cached=false) where T:class { try { return (cached ? e.GetCachedPattern(id) : e.GetCurrentPattern(id)) as T; } catch(COMException) { return null; } }
        static U3.IUIAutomationCacheRequest Cache3() {
            var c=Engine3().CreateCacheRequest(); c.TreeScope=(U3.TreeScope)1; c.AutomationElementMode=(U3.AutomationElementMode)1;
            c.AddProperty(30000);
            c.AddProperty(AutomationElement.NativeWindowHandleProperty.Id);
            foreach(var p in new[]{AutomationElement.NameProperty,AutomationElement.AutomationIdProperty,AutomationElement.ControlTypeProperty,AutomationElement.IsEnabledProperty,AutomationElement.IsOffscreenProperty,AutomationElement.IsPasswordProperty,AutomationElement.BoundingRectangleProperty,AutomationElement.FrameworkIdProperty,AutomationElement.ClassNameProperty,AutomationElement.IsKeyboardFocusableProperty}) c.AddProperty(p.Id);
            foreach(int id in PatternIds3) c.AddPattern(id);
            c.AddProperty(ValuePattern.IsReadOnlyProperty.Id); c.AddProperty(RangeValuePattern.IsReadOnlyProperty.Id);
            // Legacy properties are needed on providers that expose only MSAA.
            foreach(int id in new[]{30090,30091,30092,30093,30094,30095,30096,30097,30098,30100}) c.AddProperty(id);
            return c;
        }
        static U3.IUIAutomationElement Unique3(U3.IUIAutomationElement root,string id,string name,string role) {
            var engine=Engine3(); U3.IUIAutomationCondition condition=null;
            if(id.Length>0) condition=engine.CreatePropertyCondition(AutomationElement.AutomationIdProperty.Id,id);
            if(name.Length>0) { var n=engine.CreatePropertyCondition(AutomationElement.NameProperty.Id,name); condition=condition==null ? n : engine.CreateAndCondition(condition,n); }
            if(condition==null) throw new Fault("invalid_target","Use an inspected ref, automation_id, or name.");
            var found=root.FindAll((U3.TreeScope)4,condition); var matches=new List<U3.IUIAutomationElement>();
            for(int i=0;i<found.Length;i++) { var e=found.GetElement(i); if(role.Length==0 || string.Equals(Role3(e.CurrentControlType),role,StringComparison.OrdinalIgnoreCase)) matches.Add(e); }
            if(matches.Count==0) throw new Fault("element_not_found","No matching control.");
            if(matches.Count!=1) throw new Fault("ambiguous_target","Multiple controls match; use within, role, or a ref.");
            return matches[0];
        }
        static object Inspect3(Dictionary<string,object> a) {
            long window=Hwnd(a); int depth=I(a,"depth",6),limit=I(a,"limit",300); string view=S(a,"view","control");
            if(depth<0 || depth>20 || limit<1 || limit>3000 || (view!="control" && view!="raw")) throw new Fault("invalid_arguments","Invalid depth, limit, or view.");
            var engine=Engine3(); var cache=Cache3(); var root=S(a,"root_ref").Length>0 ? Resolve3(D("ref",S(a,"root_ref"))) : engine.ElementFromHandle(new IntPtr(window));
            if(S(a,"root_ref").Length>0 && Refs3[S(a,"root_ref")].Window!=window) throw new Fault("invalid_target","The scope belongs to a different window.");
            if(S(a,"within").Length>0) root=Unique3(root,S(a,"within"),"",""); root=root.BuildUpdatedCache(cache);
            var walker=view=="raw" ? engine.RawViewWalker : engine.ControlViewWalker;
            int budget=I(a,"time_budget_ms",2000); if(budget<100 || budget>15000) throw new Fault("invalid_arguments","time_budget_ms must be 100..15000.");
            var timer=Stopwatch.StartNew(); var queue=new Queue<Frame3>(); queue.Enqueue(new Frame3{E=root}); var rows=new List<object>(); var seen=new HashSet<string>(); int skipped=0;
            while(queue.Count>0 && rows.Count<limit && timer.ElapsedMilliseconds<budget) {
                var frame=queue.Dequeue(); var e=frame.E;
                try {
                    string runtime=CachedRuntime3(e),id="u3w"+window+":"+runtime; if(!seen.Add(id)) continue;
                    var legacy=Pattern3<U3.IUIAutomationLegacyIAccessiblePattern>(e,10018,true);
                    uint state=legacy==null ? 0 : legacy.CachedState,oldRole=legacy==null ? 0 : legacy.CachedRole;
                    bool password=e.CachedIsPassword!=0 || (state&0x20000000)!=0,chrome=frame.Chrome || e.CachedControlType==ControlType.TitleBar.Id;
                    var actions=new List<string>(); if(!password) actions.Add("get"); if(e.CachedIsKeyboardFocusable!=0) actions.Add("focus");
                    var invoke=Pattern3<U3.IUIAutomationInvokePattern>(e,10000,true);
                    var value=Pattern3<U3.IUIAutomationValuePattern>(e,10002,true); var range=Pattern3<U3.IUIAutomationRangeValuePattern>(e,10003,true);
                    if(invoke!=null || (legacy!=null && !string.IsNullOrEmpty(legacy.CachedDefaultAction))) actions.Add("invoke");
                    if(!password && ((value!=null && value.CachedIsReadOnly==0) || (range!=null && range.CachedIsReadOnly==0) || (legacy!=null && (oldRole==42 || oldRole==46) && (state&0x40)==0))) actions.Add("set");
                    if(Pattern3<U3.IUIAutomationSelectionItemPattern>(e,10010,true)!=null || (state&0x200000)!=0) actions.Add("select");
                    if(Pattern3<U3.IUIAutomationTogglePattern>(e,10015,true)!=null || (oldRole==44 && legacy!=null && !string.IsNullOrEmpty(legacy.CachedDefaultAction))) { actions.Add("toggle-on"); actions.Add("toggle-off"); }
                    if(Pattern3<U3.IUIAutomationExpandCollapsePattern>(e,10005,true)!=null) { actions.Add("expand"); actions.Add("collapse"); }
                    if(Pattern3<U3.IUIAutomationScrollPattern>(e,10004,true)!=null) actions.Add("scroll");
                    string name=e.CachedName ?? ""; if(name.Length==0 && legacy!=null) name=legacy.CachedName ?? "";
                    var rect=e.CachedBoundingRectangle;
                    var row=D("ref",id,"parent",frame.Parent,"role",Role3(e.CachedControlType),"name",password ? "" : Clip(name,160),"automation_id",e.CachedAutomationId,"enabled",e.CachedIsEnabled!=0,"offscreen",e.CachedIsOffscreen!=0,"password",password,"actions",actions,"framework",e.CachedFrameworkId,"class_name",e.CachedClassName,"region",chrome ? "window_chrome" : "content","legacy_pattern",legacy!=null,"rect",D("x",rect.left,"y",rect.top,"width",rect.right-rect.left,"height",rect.bottom-rect.top));
                    row["hwnd"]=e.CachedNativeWindowHandle;
                    row["text_pattern"]=Pattern3<U3.IUIAutomationTextPattern>(e,10014,true)!=null;
                    if(invoke!=null || actions.Contains("invoke")) row["invoke_reliability"]=invoke!=null ? "provider_contract_only" : "unreliable_legacy_default_action";
                    if(B(a,"values",false) && !password) {
                        string v=value!=null ? value.CurrentValue : legacy!=null ? legacy.CurrentValue : null;
                        if(v!=null) { row["value"]=Clip(v,160); row["value_truncated"]=v.Length>160; }
                        else if(range!=null) row["value"]=range.CurrentValue;
                    }
                    rows.Add(row); Refs3[id]=new Ref3{E=e,Window=window,Pid=Pid(window),Runtime=runtime,Seen=DateTime.UtcNow};
                    if(frame.Depth>=depth) continue;
                    var child=walker.GetFirstChildElementBuildCache(e,cache);
                    while(child!=null && rows.Count+queue.Count<limit && timer.ElapsedMilliseconds<budget) { queue.Enqueue(new Frame3{E=child,Parent=id,Depth=frame.Depth+1,Chrome=chrome}); child=walker.GetNextSiblingElementBuildCache(child,cache); }
                    if(child!=null) skipped++;
                } catch(COMException) { skipped++; }
            }
            foreach(string key in Refs3.Where(p=>(DateTime.UtcNow-p.Value.Seen).TotalMinutes>10).Select(p=>p.Key).ToArray()) Refs3.Remove(key);
            return D("window",window,"elements",rows,"visited",rows.Count,"truncated",skipped>0 || queue.Count>0,"time_budget_exhausted",timer.ElapsedMilliseconds>=budget,"provider_failures",skipped,"depth_limit",depth,"view",view,"connection_timeout_ms",1000,"transaction_timeout_ms",1200);
        }
        static U3.IUIAutomationElement Resolve3(Dictionary<string,object> a) {
            string id=S(a,"ref");
            if(id.Length>0) {
                Ref3 r;
                if(!Refs3.TryGetValue(id,out r) || (DateTime.UtcNow-r.Seen).TotalMinutes>10 || !IsWindow(new IntPtr(r.Window)) || Pid(r.Window)!=r.Pid || Runtime3(r.E)!=r.Runtime) throw new Fault("stale_ref","Inspect again before using this native UIA reference.");
                return r.E;
            }
            var root=Engine3().ElementFromHandle(new IntPtr(Hwnd(a)));
            if(S(a,"within").Length>0) root=Unique3(root,S(a,"within"),"","");
            return Unique3(root,S(a,"automation_id"),S(a,"name"),S(a,"role"));
        }
        static object Read3(U3.IUIAutomationElement e,int limit) {
            var value=Pattern3<U3.IUIAutomationValuePattern>(e,10002);
            if(value!=null) { string v=value.CurrentValue ?? ""; return D("value",Clip(v,limit),"truncated",v.Length>limit,"source","UIA3.ValuePattern"); }
            var range=Pattern3<U3.IUIAutomationRangeValuePattern>(e,10003); if(range!=null) return D("value",range.CurrentValue,"source","UIA3.RangeValuePattern");
            var text=Pattern3<U3.IUIAutomationTextPattern>(e,10014); if(text!=null) { string v=text.DocumentRange.GetText(limit+1); return D("value",Clip(v,limit),"truncated",v.Length>limit,"source","UIA3.TextPattern"); }
            var legacy=Pattern3<U3.IUIAutomationLegacyIAccessiblePattern>(e,10018);
            if(legacy!=null && legacy.CurrentValue!=null) { string v=legacy.CurrentValue; return D("value",Clip(v,limit),"truncated",v.Length>limit,"source","UIA3.LegacyIAccessiblePattern"); }
            return D("value",Clip(e.CurrentName,limit),"label_only",true,"source","UIA3.Name");
        }
        static object Action3(string op,Dictionary<string,object> a) {
            var e=Resolve3(a); var legacy=Pattern3<U3.IUIAutomationLegacyIAccessiblePattern>(e,10018);
            if(op=="input_target") {
                var rect=e.CurrentBoundingRectangle;
                var value=Pattern3<U3.IUIAutomationValuePattern>(e,10002);
                bool readOnly=value!=null ? value.CurrentIsReadOnly!=0 : legacy!=null && (legacy.CurrentState&0x40)!=0;
                return InputTarget(Refs3[S(a,"ref")].Window,D("x",rect.left,"y",rect.top,"width",rect.right-rect.left,"height",rect.bottom-rect.top),Role3(e.CurrentControlType),e.CurrentIsEnabled!=0,e.CurrentIsOffscreen!=0,e.CurrentIsPassword!=0,e.CurrentIsKeyboardFocusable!=0,e.CurrentHasKeyboardFocus!=0,readOnly,e.CurrentName);
            }
            if(e.CurrentIsPassword!=0 || (legacy!=null && (legacy.CurrentState&0x20000000)!=0)) throw new Fault("protected_control","Password controls are not exposed.");
            if(op=="get") return Read3(e,Math.Min(100000,Math.Max(1,I(a,"max_length",4096))));
            if(e.CurrentIsEnabled==0) throw new Fault("disabled_control","The control is disabled.");
            if(op=="focus") { e.SetFocus(); return D("status",e.CurrentHasKeyboardFocus!=0 ? "verified" : "executed_unverified","backend","UIA3.SetFocus"); }
            if(op=="set") {
                if(!a.ContainsKey("value")) throw new Fault("invalid_arguments","set requires an explicit value.");
                string wanted=S(a,"value");
                var p=Pattern3<U3.IUIAutomationValuePattern>(e,10002);
                if(p!=null && p.CurrentIsReadOnly==0) { p.SetValue(wanted); return D("status",p.CurrentValue==wanted ? "verified" : "verification_failed","value",p.CurrentValue,"backend","UIA3.ValuePattern"); }
                var r=Pattern3<U3.IUIAutomationRangeValuePattern>(e,10003);
                if(r!=null && r.CurrentIsReadOnly==0) { double n=Convert.ToDouble(a["value"],System.Globalization.CultureInfo.InvariantCulture); r.SetValue(n); return D("status",r.CurrentValue==n ? "verified" : "verification_failed","value",r.CurrentValue,"backend","UIA3.RangeValuePattern"); }
                if(legacy!=null && (legacy.CurrentRole==42 || legacy.CurrentRole==46) && (legacy.CurrentState&0x40)==0) { legacy.SetValue(wanted); return D("status",legacy.CurrentValue==wanted ? "verified" : "verification_failed","value",legacy.CurrentValue,"backend","UIA3.LegacyIAccessiblePattern"); }
            }
            if(op=="invoke") {
                var p=Pattern3<U3.IUIAutomationInvokePattern>(e,10000);
                if(p!=null) { p.Invoke(); return D("status","executed_unverified","backend","UIA3.InvokePattern"); }
                if(legacy!=null && !string.IsNullOrEmpty(legacy.CurrentDefaultAction)) { legacy.DoDefaultAction(); return D("status","executed_unverified","backend","UIA3.LegacyIAccessiblePattern"); }
            }
            if(op=="select") {
                var p=Pattern3<U3.IUIAutomationSelectionItemPattern>(e,10010);
                if(p!=null) { p.Select(); return D("status",p.CurrentIsSelected!=0 ? "verified" : "verification_failed","backend","UIA3.SelectionItemPattern"); }
                if(legacy!=null && (legacy.CurrentState&0x200000)!=0) { legacy.Select(2); return D("status",(legacy.CurrentState&2)!=0 ? "verified" : "verification_failed","backend","UIA3.LegacyIAccessiblePattern"); }
            }
            if(op=="toggle-on" || op=="toggle-off") {
                bool wanted=op=="toggle-on"; var p=Pattern3<U3.IUIAutomationTogglePattern>(e,10015);
                if(p!=null) { if(((int)p.CurrentToggleState==1)!=wanted) p.Toggle(); return D("status",((int)p.CurrentToggleState==1)==wanted ? "verified" : "verification_failed","backend","UIA3.TogglePattern"); }
                if(legacy!=null && legacy.CurrentRole==44 && !string.IsNullOrEmpty(legacy.CurrentDefaultAction)) { if(((legacy.CurrentState&16)!=0)!=wanted) legacy.DoDefaultAction(); return D("status",((legacy.CurrentState&16)!=0)==wanted ? "verified" : "verification_failed","backend","UIA3.LegacyIAccessiblePattern"); }
            }
            if(op=="expand" || op=="collapse") {
                var p=Pattern3<U3.IUIAutomationExpandCollapsePattern>(e,10005); if(p!=null) { if(op=="expand") p.Expand(); else p.Collapse(); return D("status",(int)p.CurrentExpandCollapseState==(op=="expand" ? 1 : 0) ? "verified" : "verification_failed","backend","UIA3.ExpandCollapsePattern"); }
            }
            if(op=="scroll") {
                var p=Pattern3<U3.IUIAutomationScrollPattern>(e,10004); string direction=S(a,"direction");
                if(p!=null) { int none=2,dec=1,inc=4; if(direction=="up") p.Scroll((U3.ScrollAmount)none,(U3.ScrollAmount)dec); else if(direction=="down") p.Scroll((U3.ScrollAmount)none,(U3.ScrollAmount)inc); else if(direction=="left") p.Scroll((U3.ScrollAmount)dec,(U3.ScrollAmount)none); else if(direction=="right") p.Scroll((U3.ScrollAmount)inc,(U3.ScrollAmount)none); else throw new Fault("invalid_arguments","Unknown scroll direction."); return D("status","executed_unverified","backend","UIA3.ScrollPattern"); }
            }
            throw new Fault("unsupported","The native UIA object does not support this action.");
        }
        static object Uia3Dispatch(string op,Dictionary<string,object> a) { if(op=="inspect") return Inspect3(a); return Action3(op,a); }
    }
}
