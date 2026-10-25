using System;
using System.Drawing;
using System.IO;
using System.Text;
using System.Windows.Forms;
using System.Web.Script.Serialization;

public class CacheForm : Form {
    readonly string StatePath;
    readonly TextBox Entry;
    readonly Label Counter;
    int Count, Writes;
    protected override bool ShowWithoutActivation { get { return true; } }
    public CacheForm(string path) {
        StatePath=path; Text="GUI2API owned cache benchmark";
        Size=new Size(590,430); ShowInTaskbar=false; StartPosition=FormStartPosition.Manual;
        Location=new Point(30,30);
        Entry=new TextBox {Name="Payload",AccessibleName="Payload",Location=new Point(20,20),Width=280};
        Counter=new Label {Name="Counter",AccessibleName="Counter",Text="0",Location=new Point(350,25),Width=100};
        var increment=new Button {Name="Increment",AccessibleName="Increment",Text="Increment",Location=new Point(20,60),Width=140};
        increment.Click+=delegate {Count++;Counter.Text=Count.ToString();Save();};
        Entry.TextChanged+=delegate {Writes++;Save();};
        Controls.Add(Entry);Controls.Add(Counter);Controls.Add(increment);
        var panel=new Panel {Name="Background",AccessibleName="Background",Location=new Point(20,110),Size=new Size(530,260),AutoScroll=true};
        for(int i=0;i<100;i++) panel.Controls.Add(new Label {Name="Static"+i,AccessibleName="Static item "+i,Text="Static item "+i,Location=new Point((i%4)*130,(i/4)*22),Size=new Size(125,20)});
        Controls.Add(panel); Shown+=delegate {Save();};
    }
    void Save() {
        if(!IsHandleCreated)return;
        string json=new JavaScriptSerializer().Serialize(new {window=Handle.ToInt64(),pid=System.Diagnostics.Process.GetCurrentProcess().Id,
            count=Count,text=Entry.Text,writes=Writes});
        string temporary=StatePath+".tmp";File.WriteAllText(temporary,json,Encoding.UTF8);
        if(File.Exists(StatePath))File.Delete(StatePath);File.Move(temporary,StatePath);
    }
    [STAThread] public static void Main(string[] args) {Application.EnableVisualStyles();Application.Run(new CacheForm(args[0]));}
}
