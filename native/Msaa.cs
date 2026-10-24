using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using Accessibility;

namespace AppCtl {
    public static partial class Worker {
        [DllImport("oleacc.dll")] static extern int AccessibleObjectFromWindow(IntPtr window,uint objectId,ref Guid iid,[MarshalAs(UnmanagedType.Interface)] out IAccessible accessible);
        [DllImport("oleacc.dll")] static extern int WindowFromAccessibleObject(IAccessible accessible,out IntPtr window);
        [DllImport("oleacc.dll")] static extern int AccessibleChildren(IAccessible accessible,int start,int count,[Out,MarshalAs(UnmanagedType.LPArray,SizeParamIndex=2)] object[] children,out int obtained);
        class AccRef { public IAccessible Object; public int Child,Pid,Role; public long Window; public string Name; public DateTime Seen; }
        class AccFrame { public IAccessible Object; public int Child,Depth; public string Parent; public bool Chrome; }
        static readonly Dictionary<string,AccRef> AccRefs=new Dictionary<string,AccRef>();
        static T AccTry<T>(Func<T> f,T fallback) { try { return f(); } catch(COMException) { return fallback; } catch(ArgumentException) { return fallback; } catch(OutOfMemoryException) { return fallback; } }
        static int AccInt(object value) { try { return Convert.ToInt32(value); } catch { return 0; } }
        static string AccName(IAccessible a,int child) { return AccTry(()=>a.get_accName(child),"") ?? ""; }
        static int AccRole(IAccessible a,int child) { return AccInt(AccTry(()=>a.get_accRole(child),null)); }
        static int AccState(IAccessible a,int child) { return AccInt(AccTry(()=>a.get_accState(child),null)); }
        static string AccKey(IAccessible a,int child) {
            IntPtr pointer=Marshal.GetIUnknownForObject(a);
            try { return "a"+pointer.ToInt64()+"."+child; } finally { Marshal.Release(pointer); }
        }
        static string AccRoleName(int role) {
            switch(role) {
                case 1:return "TitleBar"; case 2:return "MenuBar"; case 3:return "ScrollBar"; case 9:return "Window";
                case 10:return "Pane"; case 11:return "Menu"; case 12:return "MenuItem"; case 13:return "ToolTip"; case 14:return "Window";
                case 15:return "Document"; case 16:return "Pane"; case 18:return "Window"; case 20:return "Group"; case 21:return "Separator";
                case 22:return "ToolBar"; case 23:return "StatusBar"; case 24:return "Table"; case 25:return "HeaderItem"; case 29:return "DataItem";
                case 30:return "Hyperlink"; case 33:return "List"; case 34:return "ListItem"; case 35:return "Tree"; case 36:return "TreeItem";
                case 37:return "TabItem"; case 38:return "Pane"; case 40:return "Image"; case 41:return "Text"; case 42:return "Edit";
                case 43:return "Button"; case 44:return "CheckBox"; case 45:return "RadioButton"; case 46:return "ComboBox";
                case 48:return "ProgressBar"; case 51:return "Slider"; case 52:return "Spinner"; case 56:case 57:case 58:case 62:return "Button";
                case 60:return "Tab"; default:return "Custom";
            }
        }
        static object MsaaInspect(Dictionary<string,object> a) {
            long window=Hwnd(a); int depth=I(a,"depth",6),limit=I(a,"limit",300);
            if(depth<0 || depth>100 || limit<1 || limit>3000) throw new Fault("invalid_arguments","depth must be 0..100 and limit 1..3000.");
            int budget=I(a,"time_budget_ms",5000);
            if(budget<100 || budget>15000) throw new Fault("invalid_arguments","time_budget_ms must be 100..15000.");
            var timer=Stopwatch.StartNew();
            if(S(a,"within").Length>0) throw new Fault("unsupported_scope","MSAA does not expose UIA AutomationId scopes. Use the UIA driver for within.");
            var queue=new Queue<AccFrame>(); var seen=new HashSet<string>(); var covered=new HashSet<long>(); int failures=0,roots=0;
            // Some frameworks expose their accessible tree only on a child HWND.
            var candidates=new Queue<long>();
            if(S(a,"root_ref").Length>0) {
                AccRef scoped;
                if(!AccRefs.TryGetValue(S(a,"root_ref"),out scoped) || scoped.Window!=window || scoped.Pid!=Pid(window) || (DateTime.UtcNow-scoped.Seen).TotalMinutes>10) throw new Fault("stale_ref","Inspect again before using this subtree reference.");
                queue.Enqueue(new AccFrame{Object=scoped.Object,Child=scoped.Child,Depth=0});
            } else foreach(long candidate in ChildHandles(window,48)) candidates.Enqueue(candidate);
            var rows=new List<object>(); bool truncated=false;
            while((queue.Count>0 || candidates.Count>0) && rows.Count<limit && timer.ElapsedMilliseconds<budget) {
                if(queue.Count==0) {
                    long candidate=candidates.Dequeue(); if(covered.Contains(candidate)) continue;
                    IAccessible acc; Guid iid=new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
                    int hr; try { hr=AccessibleObjectFromWindow(new IntPtr(candidate),0xFFFFFFFC,ref iid,out acc); } catch(OutOfMemoryException) { failures++; continue; }
                    if(hr<0 || acc==null) continue;
                    queue.Enqueue(new AccFrame{Object=acc,Child=0,Depth=0,Parent=null}); roots++; covered.Add(candidate);
                }
                var f=queue.Dequeue(); string key=AccKey(f.Object,f.Child); if(!seen.Add(key)) continue;
                IntPtr source=IntPtr.Zero; try { if(WindowFromAccessibleObject(f.Object,out source)>=0 && source!=IntPtr.Zero) covered.Add(source.ToInt64()); } catch(COMException) { }
                int role,state;
                try { role=AccRole(f.Object,f.Child); state=AccState(f.Object,f.Child); }
                catch(InvalidCastException) { failures++; continue; }
                bool password=(state&0x20000000)!=0;
                bool chrome=f.Chrome || role==1;
                string name=password ? "" : AccName(f.Object,f.Child);
                string action=AccTry(()=>f.Object.get_accDefaultAction(f.Child),"") ?? "";
                var actions=new List<string>(); if(!password) actions.Add("get");
                if(action.Length>0) actions.Add("invoke");
                if(!password && (role==42 || role==46) && (state&0x40)==0) actions.Add("set");
                if((state&0x100000)!=0) actions.Add("focus");
                if((state&0x200000)!=0) actions.Add("select");
                if(role==44 && action.Length>0) { actions.Add("toggle-on"); actions.Add("toggle-off"); }
                int x=0,y=0,w=0,h=0; try { f.Object.accLocation(out x,out y,out w,out h,f.Child); } catch(COMException) { }
                string region=chrome || NonClient(window,x,y,w,h) ? "window_chrome" : w>0 && h>0 ? "content" : "unknown";
                var row=D("ref",key,"parent",f.Parent,"role",AccRoleName(role),"name",Clip(name,160),"automation_id","","enabled",(state&1)==0,"offscreen",(state&0x18000)!=0,"password",password,"actions",actions,"default_action",action,"msaa_role",role,"msaa_state",state,"region",region,"hwnd",source.ToInt64(),"rect",D("x",x,"y",y,"width",w,"height",h));
                row["invoke_reliability"]="unreliable_legacy_default_action";
                if(B(a,"values",false) && !password) { string v=AccTry(()=>f.Object.get_accValue(f.Child),null); if(v!=null) { row["value"]=Clip(v,160); row["value_truncated"]=v.Length>160; } }
                rows.Add(row); AccRefs[key]=new AccRef{Object=f.Object,Child=f.Child,Window=window,Pid=Pid(window),Role=role,Name=name,Seen=DateTime.UtcNow};
                if(f.Child!=0) continue;
                if(f.Depth>=depth) { if(AccTry(()=>f.Object.accChildCount,0)>0) truncated=true; continue; }
                int count=Math.Min(Math.Max(0,AccTry(()=>f.Object.accChildCount,0)),limit-rows.Count);
                if(count==0) continue; var children=new object[count]; int got;
                int result; try { result=AccessibleChildren(f.Object,0,count,children,out got); } catch(OutOfMemoryException) { failures++; continue; } catch(COMException) { failures++; continue; } if(result<0) { failures++; continue; }
                if(got<count) truncated=true;
                for(int i=0;i<got;i++) {
                    var obj=children[i] as IAccessible;
                    if(obj!=null) queue.Enqueue(new AccFrame{Object=obj,Child=0,Depth=f.Depth+1,Parent=key,Chrome=chrome});
                    else if(children[i] is int) {
                        int child=(int)children[i]; if(child==0) continue;
                        var full=AccTry(()=>f.Object.get_accChild(child),null) as IAccessible;
                        queue.Enqueue(new AccFrame{Object=full ?? f.Object,Child=full==null ? child : 0,Depth=f.Depth+1,Parent=key,Chrome=chrome});
                    }
                }
            }
            foreach(string key in AccRefs.Where(p=>(DateTime.UtcNow-p.Value.Seen).TotalMinutes>10).Select(p=>p.Key).ToArray()) AccRefs.Remove(key);
            if(AccRefs.Count>20000) foreach(string key in AccRefs.OrderBy(p=>p.Value.Seen).Take(AccRefs.Count-20000).Select(p=>p.Key).ToArray()) AccRefs.Remove(key);
            return D("window",window,"elements",rows,"visited",rows.Count,"truncated",truncated || failures>0 || queue.Count>0 || candidates.Count>0,"roots",roots,"provider_failures",failures,"depth_limit",depth,"time_budget_exhausted",timer.ElapsedMilliseconds>=budget);
        }
        static object AccRead(AccRef r,int limit) {
            string v=AccTry(()=>r.Object.get_accValue(r.Child),null);
            if(v!=null) return D("value",Clip(v,limit),"source","IAccessible.accValue","truncated",v.Length>limit);
            return D("value",Clip(AccName(r.Object,r.Child),limit),"source","IAccessible.accName","label_only",true);
        }
        static object MsaaAction(string op,Dictionary<string,object> a) {
            AccRef r;
            if(!AccRefs.TryGetValue(S(a,"ref"),out r) || (DateTime.UtcNow-r.Seen).TotalMinutes>10 || !IsWindow(new IntPtr(r.Window)) || Pid(r.Window)!=r.Pid) throw new Fault("stale_ref","Inspect again before using this MSAA reference.");
            int state=AccState(r.Object,r.Child),role=AccRole(r.Object,r.Child);
            if((state&0x20000000)!=0) throw new Fault("protected_control","Password controls are not exposed.");
            // A retained COM object is the identity; placeholders/names can
            // legitimately change on focus or text input. Integer child ids are
            // weaker, so keep the conservative name check for those references.
            if(role!=r.Role || r.Child!=0 && AccName(r.Object,r.Child)!=r.Name) throw new Fault("stale_ref","MSAA child identity changed; inspect again.");
            if(op=="input_target") {
                int x,y,w,h; r.Object.accLocation(out x,out y,out w,out h,r.Child);
                return InputTarget(r.Window,D("x",x,"y",y,"width",w,"height",h),AccRoleName(role),(state&1)==0,(state&0x18000)!=0,false,(state&0x100000)!=0,(state&4)!=0,(state&0x40)!=0,AccName(r.Object,r.Child));
            }
            if(op=="get") return AccRead(r,Math.Min(100000,Math.Max(1,I(a,"max_length",4096))));
            if((state&1)!=0) throw new Fault("disabled_control","The control is disabled.");
            if(op=="set" && (role==42 || role==46) && (state&0x40)==0) {
                if(!a.ContainsKey("value")) throw new Fault("invalid_arguments","set requires an explicit value.");
                string desired=S(a,"value"); r.Object.set_accValue(r.Child,desired); string actual=AccTry(()=>r.Object.get_accValue(r.Child),null);
                return D("status",actual==desired ? "verified" : actual==null ? "executed_unverified" : "verification_failed","value",actual,"backend","IAccessible.accValue");
            }
            if(op=="focus" && (state&0x100000)!=0) { r.Object.accSelect(1,r.Child); return D("status",(AccState(r.Object,r.Child)&4)!=0 ? "verified" : "executed_unverified","backend","IAccessible.accSelect"); }
            if(op=="select" && (state&0x200000)!=0) { r.Object.accSelect(2,r.Child); return D("status",(AccState(r.Object,r.Child)&2)!=0 ? "verified" : "executed_unverified","backend","IAccessible.accSelect"); }
            string defaultAction=AccTry(()=>r.Object.get_accDefaultAction(r.Child),"");
            if(op=="invoke" && !string.IsNullOrEmpty(defaultAction)) {
                r.Object.accDoDefaultAction(r.Child);
                return D("status","executed_unverified","backend","IAccessible.accDoDefaultAction",
                    "action_target",D("kind",r.Child==0 ? "accessible_self" : "accessible_child","child_id",r.Child,
                        "requested_native_ref",S(a,"ref"),"application_internal_recipient","unknown"),
                    "invoke_reliability","unreliable_legacy_default_action");
            }
            if((op=="toggle-on" || op=="toggle-off") && role==44 && !string.IsNullOrEmpty(defaultAction)) {
                bool desired=op=="toggle-on"; if(((state&16)!=0)!=desired) r.Object.accDoDefaultAction(r.Child);
                return D("status",((AccState(r.Object,r.Child)&16)!=0)==desired ? "verified" : "verification_failed","backend","IAccessible.accDoDefaultAction");
            }
            throw new Fault("unsupported","The MSAA object does not expose this action.");
        }
        static object MsaaDispatch(string op,Dictionary<string,object> a) {
            if(op=="inspect") return MsaaInspect(a);
            if(new[]{"get","set","invoke","focus","select","toggle-on","toggle-off","input_target"}.Contains(op)) return MsaaAction(op,a);
            throw new Fault("unsupported","This command is not supported by the MSAA driver.");
        }
    }
}
