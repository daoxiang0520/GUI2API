using System;
using System.Collections.Generic;
using System.Threading.Tasks;

namespace ObjectFixtures {
    public class Child {
        public string label { get; set; }
        public Child() { label = "子对象"; }
        public string echo(string text) { return text; }
    }
    public class Counter {
        public int count { get; set; }
        public string label { get; set; }
        public event EventHandler changed;
        readonly Child childObject = new Child();
        int getterCalls;
        public int probe { get { return ++getterCalls; } }
        public Counter() { label = ".NET 业务对象"; }
        public int add(int amount) { count += amount; if (changed != null) changed(this, EventArgs.Empty); return count; }
        public Child child() { return childObject; }
        public bool same_child(Child other) { return Object.ReferenceEquals(other, childObject); }
        public Task<string> echo(string text) { Console.WriteLine("fixture application log"); return Task.FromResult(text); }
        public void fail_after_add() { count++; throw new InvalidOperationException("fixture failure after a side effect"); }
    }
    public static class Factory {
        public static IDictionary<string, object> CreateRoots() {
            var counter = new Counter();
            return new Dictionary<string, object> { { "counter", counter }, { "same_counter", counter } };
        }
    }
}
