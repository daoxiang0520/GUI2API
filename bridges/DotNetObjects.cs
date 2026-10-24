using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Linq.Expressions;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace AppCtlObjects {
    public class ObjectFault : Exception {
        public string Code;
        public ObjectFault(string code, string message) : base(message) { Code = code; }
    }

    public class Provider {
        readonly string instance = Guid.NewGuid().ToString("N");
        readonly Dictionary<string, object> roots;
        readonly Dictionary<string, object> objects = new Dictionary<string, object>();
        readonly HashSet<string> rootIds = new HashSet<string>();
        readonly Dictionary<string, Tuple<object, EventInfo, Delegate>> subscriptions = new Dictionary<string, Tuple<object, EventInfo, Delegate>>();
        readonly Queue<object> events = new Queue<object>();
        readonly object eventLock = new object();
        readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024 };
        int dropped, sequence;

        public Provider(IDictionary<string, object> exposedRoots) {
            roots = new Dictionary<string, object>(exposedRoots);
            foreach (var value in roots.Values) rootIds.Add((string)Box(value, true)["ref"]);
        }
        static Dictionary<string, object> D(params object[] values) {
            var data = new Dictionary<string, object>();
            for (int i = 0; i < values.Length; i += 2) data[(string)values[i]] = values[i + 1];
            return data;
        }
        static object Arg(Dictionary<string, object> args, string key, object fallback = null) {
            object value; return args.TryGetValue(key, out value) ? value : fallback;
        }
        static bool IsJson(object value, int depth = 0) {
            if (depth > 20) return false;
            if (value == null || value is string || value is bool || value is decimal) return true;
            if (value is double) return !Double.IsNaN((double)value) && !Double.IsInfinity((double)value);
            if (value is float) return !Single.IsNaN((float)value) && !Single.IsInfinity((float)value);
            if (value.GetType().IsPrimitive && !(value is IntPtr) && !(value is UIntPtr)) return true;
            if (value is IDictionary) {
                foreach (DictionaryEntry row in (IDictionary)value) if (!(row.Key is string) || !IsJson(row.Value, depth + 1)) return false;
                return true;
            }
            if (value is Array || value is ArrayList || value.GetType().IsGenericType && value.GetType().GetGenericTypeDefinition() == typeof(List<>)) {
                foreach (var row in (IEnumerable)value) if (!IsJson(row, depth + 1)) return false;
                return true;
            }
            return false;
        }
        Dictionary<string, object> Box(object value, bool force = false) {
            if (!force && IsJson(value)) return D("kind", "value", "value", value);
            foreach (var row in objects) if (Object.ReferenceEquals(row.Value, value)) return D("kind", "object", "ref", row.Key, "type", value.GetType().FullName);
            if (objects.Count >= 4096) throw new ObjectFault("object_limit", "Release unused object references");
            string id = instance + ":" + Guid.NewGuid().ToString("N"); objects[id] = value;
            return D("kind", "object", "ref", id, "type", value.GetType().FullName);
        }
        object Resolve(string id) {
            object value;
            if (id == null || !objects.TryGetValue(id, out value)) throw new ObjectFault("stale_ref", "Object reference is no longer available");
            return value;
        }
        object Unbox(object input) {
            var value = input as Dictionary<string, object>;
            if (value == null) throw new ObjectFault("invalid_arguments", "Expected a value/object envelope");
            if ((string)Arg(value, "kind") == "object") return Resolve((string)Arg(value, "ref"));
            if ((string)Arg(value, "kind") == "value") return Arg(value, "value");
            throw new ObjectFault("invalid_arguments", "Unknown value envelope");
        }
        static string Signature(MethodInfo method) {
            return method.Name + "(" + String.Join(",", method.GetParameters().Select(p => p.ParameterType.FullName)) + ")";
        }
        static MethodInfo[] Methods(object obj) {
            return obj.GetType().GetMethods(BindingFlags.Instance | BindingFlags.Public).Where(m => m.DeclaringType != typeof(object) && !m.IsSpecialName && !m.ContainsGenericParameters && !m.GetParameters().Any(p => p.ParameterType.IsByRef)).ToArray();
        }
        Dictionary<string, object> Describe(object obj) {
            var properties = new List<object>();
            foreach (var prop in obj.GetType().GetProperties(BindingFlags.Instance | BindingFlags.Public).Where(p => p.GetIndexParameters().Length == 0))
                properties.Add(D("name", prop.Name, "type", prop.PropertyType.FullName, "readable", prop.GetGetMethod() != null, "writable", prop.GetSetMethod() != null, "getter_executed", false));
            foreach (var field in obj.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public))
                properties.Add(D("name", field.Name, "type", field.FieldType.FullName, "readable", true, "writable", !field.IsInitOnly && !field.IsLiteral, "getter_executed", false));
            var methods = Methods(obj).Select(m => D("name", Signature(m), "member", m.Name, "parameters", m.GetParameters().Select(p => D("name", p.Name, "type", p.ParameterType.FullName, "required", !p.IsOptional)).ToArray(), "returns", m.ReturnType.FullName, "effect", "unknown")).ToArray();
            return D("type", obj.GetType().FullName, "properties", properties, "methods", methods, "events", obj.GetType().GetEvents(BindingFlags.Instance | BindingFlags.Public).Select(e => D("name", e.Name)).ToArray());
        }
        PropertyInfo Property(object obj, string name) { return obj.GetType().GetProperty(name, BindingFlags.Instance | BindingFlags.Public); }
        FieldInfo Field(object obj, string name) { return obj.GetType().GetField(name, BindingFlags.Instance | BindingFlags.Public); }
        object Read(object obj, string name) {
            var prop = Property(obj, name);
            if (prop != null && prop.GetIndexParameters().Length == 0 && prop.GetGetMethod() != null) return prop.GetValue(obj, null);
            var field = Field(obj, name); if (field != null) return field.GetValue(obj);
            throw new ObjectFault("unknown_member", "Property is not readable");
        }
        object ConvertArgument(object value, Type type) {
            if (value == null && type.IsValueType && Nullable.GetUnderlyingType(type) == null) throw new ObjectFault("invalid_arguments", "Null cannot be passed to this value type");
            if (value == null || type.IsInstanceOfType(value)) return value;
            try { return json.ConvertToType(value, type); }
            catch { throw new ObjectFault("invalid_arguments", "Argument does not match " + type.FullName); }
        }
        public void OnEvent(string id, string name) {
            lock (eventLock) {
                if (events.Count >= 1024) { events.Dequeue(); dropped++; }
                events.Enqueue(D("sequence", ++sequence, "object", D("kind", "object", "ref", id, "type", "Object"), "name", name));
            }
        }
        void Unsubscribe(string key) {
            Tuple<object, EventInfo, Delegate> current;
            if (subscriptions.TryGetValue(key, out current)) { current.Item2.RemoveEventHandler(current.Item1, current.Item3); subscriptions.Remove(key); }
        }
        object Request(string op, Dictionary<string, object> args) {
            if (op == "ping") return D("protocol", "appctl.objects/1", "instance", instance, "runtime", "dotnet", "access", "registered_roots", "arbitrary_process_attach", false, "operations", new[] { "list", "describe", "get", "set", "call", "watch", "events", "release" });
            if (op == "list") return D("objects", roots.Select(r => { var value = Box(r.Value, true); value["name"] = r.Key; return value; }).ToArray());
            if (op == "events") {
                int timeout = Convert.ToInt32(Arg(args, "timeout_ms", 0)), limit = Convert.ToInt32(Arg(args, "limit", 100));
                if (timeout < 0 || timeout > 5000 || limit < 1 || limit > 1000) throw new ObjectFault("invalid_arguments", "Invalid event bounds");
                var end = DateTime.UtcNow.AddMilliseconds(timeout);
                while (true) { lock (eventLock) { if (events.Count > 0) break; } if (DateTime.UtcNow >= end) break; Thread.Sleep(5); }
                lock (eventLock) {
                    var rows = new List<object>(); while (events.Count > 0 && rows.Count < limit) rows.Add(events.Dequeue());
                    var result = D("events", rows, "dropped", dropped, "values_in_events", false); dropped = 0; return result;
                }
            }
            string id = (string)Arg(args, "ref"), name = (string)Arg(args, "member"); object obj = Resolve(id);
            if (op == "describe") { var data = Describe(obj); data["object"] = Box(obj, true); return data; }
            if (op == "get") return D("result", Box(Read(obj, name)));
            if (op == "set") {
                object value = Unbox(Arg(args, "value")); var prop = Property(obj, name); var field = Field(obj, name);
                if (prop != null && prop.GetIndexParameters().Length == 0 && prop.GetSetMethod() != null) prop.SetValue(obj, ConvertArgument(value, prop.PropertyType), null);
                else if (field != null && !field.IsInitOnly && !field.IsLiteral) field.SetValue(obj, ConvertArgument(value, field.FieldType));
                else throw new ObjectFault("unsupported", "Property is not writable");
                if (prop != null && prop.GetGetMethod() == null) return D("status", "executed_unverified");
                var actual = Read(obj, name); return D("status", json.Serialize(actual) == json.Serialize(value) ? "verified" : "verification_failed", "result", Box(actual));
            }
            if (op == "call") {
                var candidates = Methods(obj).Where(m => Signature(m) == name).ToArray();
                if (candidates.Length == 0) candidates = Methods(obj).Where(m => m.Name == name).ToArray();
                if (candidates.Length != 1) throw new ObjectFault(candidates.Length > 1 ? "ambiguous_member" : "unknown_member", "Choose an exact advertised method signature");
                var method = candidates[0]; var parameters = method.GetParameters();
                var supplied = ((IEnumerable)Arg(args, "arguments", new object[0])).Cast<object>().Select(Unbox).ToArray();
                if (supplied.Length > parameters.Length || supplied.Length < parameters.Count(p => !p.IsOptional)) throw new ObjectFault("invalid_arguments", "Incorrect argument count");
                var values = parameters.Select((p, i) => i < supplied.Length ? ConvertArgument(supplied[i], p.ParameterType) : p.DefaultValue).ToArray();
                object result = method.Invoke(obj, values); var task = result as Task;
                if (task != null) { task.GetAwaiter().GetResult(); var resultProp = task.GetType().GetProperty("Result"); result = resultProp == null ? null : resultProp.GetValue(task, null); }
                return D("status", "returned", "effect", "unknown", "result", Box(result));
            }
            if (op == "watch") {
                var names = Arg(args, "names") as IEnumerable;
                if (names == null) names = obj.GetType().GetEvents().Select(e => e.Name).ToArray();
                foreach (string eventName in names) {
                    var eventInfo = obj.GetType().GetEvent(eventName, BindingFlags.Instance | BindingFlags.Public);
                    if (eventInfo == null) throw new ObjectFault("unknown_member", "Event is not advertised");
                    string key = id + "\n" + eventName; Unsubscribe(key);
                    if (Convert.ToBoolean(Arg(args, "enabled", true))) {
                        var invoke = eventInfo.EventHandlerType.GetMethod("Invoke");
                        var parameters = invoke.GetParameters().Select(p => Expression.Parameter(p.ParameterType, p.Name)).ToArray();
                        var callback = Expression.Call(Expression.Constant(this), typeof(Provider).GetMethod("OnEvent"), Expression.Constant(id), Expression.Constant(eventName));
                        var handler = Expression.Lambda(eventInfo.EventHandlerType, callback, parameters).Compile();
                        eventInfo.AddEventHandler(obj, handler); subscriptions[key] = Tuple.Create(obj, eventInfo, handler);
                    }
                }
                return D("watching", subscriptions.Keys.Where(k => k.StartsWith(id + "\n")).Select(k => k.Substring(id.Length + 1)).ToArray(), "values_in_events", false);
            }
            if (op == "release") {
                if (rootIds.Contains(id)) throw new ObjectFault("root_reference", "Root objects live for the provider session");
                foreach (string key in subscriptions.Keys.Where(k => k.StartsWith(id + "\n")).ToArray()) Unsubscribe(key);
                objects.Remove(id); return D("released", true);
            }
            throw new ObjectFault("unknown_operation", "Unknown object operation");
        }
        public void Serve() {
            Console.InputEncoding = new UTF8Encoding(false); Console.OutputEncoding = new UTF8Encoding(false);
            var protocolOutput = Console.Out;
            Console.SetOut(Console.Error);
            string line;
            while ((line = Console.ReadLine()) != null) {
                object id = null; Dictionary<string, object> result;
                try {
                    var request = json.Deserialize<Dictionary<string, object>>(line); id = Arg(request, "id");
                    var args = Arg(request, "args", new Dictionary<string, object>()) as Dictionary<string, object>;
                    if (args == null) throw new ObjectFault("invalid_request", "args must be an object");
                    result = D("id", id, "ok", true, "data", Request((string)Arg(request, "op"), args));
                } catch (Exception outer) {
                    Exception e = outer is TargetInvocationException && outer.InnerException != null ? outer.InnerException : outer;
                    var fault = e as ObjectFault;
                    result = D("id", id, "ok", false, "error", D("code", fault == null ? "application_exception" : fault.Code, "message", e.Message, "operation_may_have_executed", fault == null));
                }
                protocolOutput.WriteLine(json.Serialize(result)); protocolOutput.Flush();
            }
            Console.SetOut(protocolOutput);
        }
    }
}
