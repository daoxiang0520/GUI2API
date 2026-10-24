using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using System.Web.Script.Serialization;

// Owned test fixture: the documented native-object-model handshake exposes the
// same model used by the UI. No discovery-specific method names are required.
[ComVisible(true), ClassInterface(ClassInterfaceType.AutoDual)]
public class DiscoveryModel {
    readonly DiscoveryForm Form;
    public DiscoveryModel(DiscoveryForm form) { Form=form; }
    public int Count { get { return Form.Count; } }
    public string Label { get { return Form.Entry.Text; } set { Form.Entry.Text=value; } }
    public int Add(int amount) { Form.Count+=amount; Form.Save(); return Count; }
}
public class DiscoveryForm : Form {
    [DllImport("oleacc.dll")] static extern IntPtr LresultFromObject(ref Guid riid,IntPtr wParam,IntPtr punk);
    readonly string StatePath;
    readonly DiscoveryModel Model;
    public int Count;
    public readonly TextBox Entry;
    public DiscoveryForm(string path) {
        StatePath=path; Text="AppCtl native model discovery fixture"; Size=new Size(480,220); ShowInTaskbar=false;
        Entry=new TextBox { Name="ModelEntry", AccessibleName="Model entry", Location=new Point(20,20), Width=250 };
        Entry.TextChanged+=delegate { Save(); }; Controls.Add(Entry);
        Model=new DiscoveryModel(this);
        var button=new Button { AccessibleName="Increment model", Text="Increment model", Location=new Point(20,70), Width=200 };
        button.Click+=delegate { Model.Add(1); }; Controls.Add(button);
        Shown+=delegate { Save(); };
    }
    public void Save() {
        if(!IsHandleCreated || Entry==null) return;
        string json=new JavaScriptSerializer().Serialize(new { window=Handle.ToInt64(), pid=System.Diagnostics.Process.GetCurrentProcess().Id, count=Count, text=Entry.Text });
        File.WriteAllText(StatePath,json,System.Text.Encoding.UTF8);
    }
    protected override void WndProc(ref Message m) {
        if(m.Msg==0x003d && m.LParam.ToInt64()==-16 && Model!=null) {
            Guid iid=new Guid("00020400-0000-0000-C000-000000000046");
            IntPtr pointer=Marshal.GetIDispatchForObject(Model);
            try { m.Result=LresultFromObject(ref iid,m.WParam,pointer); } finally { Marshal.Release(pointer); }
            return;
        }
        base.WndProc(ref m);
    }
    [STAThread] public static void Main(string[] args) {
        Application.EnableVisualStyles(); Application.Run(new DiscoveryForm(args[0]));
    }
}
