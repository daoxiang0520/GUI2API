using System;
using System.Drawing;
using System.IO;
using System.Windows.Forms;

public class ResourceFixture : Form {
    public ResourceFixture(string state) {
        Text="GUI2API owned resource fixture"; Name="OwnedResourceWindow";
        Size=new Size(300,210); ShowInTaskbar=false; StartPosition=FormStartPosition.Manual;
        var area=Screen.PrimaryScreen.WorkingArea; Location=new Point(area.Left+40,area.Top+70);
        Controls.Add(new ResourceImage { Location=new Point(30,30),Size=new Size(160,96),
            Name="OwnedImage",AccessibleName="Owned resource image",AccessibleRole=AccessibleRole.Graphic });
        Shown+=delegate { File.WriteAllText(state,Handle.ToInt64().ToString()); };
        var timer=new Timer { Interval=30000 }; timer.Tick+=delegate { timer.Stop(); Close(); }; timer.Start();
    }
    [STAThread] public static void Main(string[] args) { Application.EnableVisualStyles(); Application.Run(new ResourceFixture(args[0])); }
}
public class ResourceImage : Control {
    protected override void OnPaint(PaintEventArgs e) {
        using(var red=new SolidBrush(Color.FromArgb(230,40,50)))
        using(var blue=new SolidBrush(Color.FromArgb(40,120,220))) {
            e.Graphics.FillRectangle(red,0,0,Width/2,Height);
            e.Graphics.FillRectangle(blue,Width/2,0,Width-Width/2,Height);
        }
    }
}
