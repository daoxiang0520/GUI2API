using System;
using System.Drawing;
using System.IO;
using System.Windows.Forms;
using System.Web.Script.Serialization;

public class LearningForm : Form {
    readonly string StatePath;
    Panel Details;
    int Opens,Closes;
    public LearningForm(string path) {
        StatePath=path; Text="AppCtl incremental learning fixture"; Size=new Size(500,300); ShowInTaskbar=false;
        var open=new Button {Name="OpenDetails",AccessibleName="Open details",Text="Open details",Location=new Point(20,20),Width=160};
        open.Click+=delegate {if(Details!=null)return; Opens++; Details=new Panel {Name="DetailsPanel",AccessibleName="Details panel",Location=new Point(20,70),Size=new Size(430,150)};
            var entry=new TextBox {Name="DetailEntry",AccessibleName="Detail entry",Location=new Point(10,10),Width=250};
            var close=new Button {Name="CloseDetails",AccessibleName="Close details",Text="Close details",Location=new Point(10,60),Width=160};
            close.Click+=delegate {Closes++; Controls.Remove(Details); Details.Dispose(); Details=null; Save();};
            Details.Controls.Add(entry); Details.Controls.Add(close); Controls.Add(Details); Save();};
        Controls.Add(open); Shown+=delegate {Save();};
    }
    void Save() {
        if(!IsHandleCreated)return;
        File.WriteAllText(StatePath,new JavaScriptSerializer().Serialize(new {window=Handle.ToInt64(),pid=System.Diagnostics.Process.GetCurrentProcess().Id,open=Details!=null,opens=Opens,closes=Closes}),System.Text.Encoding.UTF8);
    }
    [STAThread] public static void Main(string[] args) {Application.EnableVisualStyles();Application.Run(new LearningForm(args[0]));}
}
