using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;
using System.Web.Script.Serialization;

public class ReliabilityForm : Form {
    [DllImport("kernel32.dll")] static extern bool AllocConsole();
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int code);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern IntPtr CreateFile(string path,uint access,uint share,IntPtr security,uint creation,uint flags,IntPtr template);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern bool WriteConsole(IntPtr output,string text,int length,out int written,IntPtr reserved);
    readonly string StatePath;
    readonly TextBox Entry;
    int Count,Menus,Enters;
    bool MenuVisible;
    bool ConsoleReady,ConsoleWritten;
    public ReliabilityForm(string path) {
        StatePath=path; Text="AppCtl reliability fixture"; Size=new Size(550,320); ShowInTaskbar=false;
        Entry=new TextBox {Name="Entry",AccessibleName="Mutable entry",Location=new Point(20,25),Width=300};
        Entry.TextChanged+=delegate {Entry.AccessibleName=Entry.Text.Length==0 ? "Mutable entry" : "";Save();};
        Entry.KeyDown+=delegate(object sender,KeyEventArgs e) {if(e.KeyCode==Keys.Enter) {Enters++;e.SuppressKeyPress=true;Save();}};
        var a=new Button {Name="A",Text="Increment A",AccessibleName="Increment A",Location=new Point(20,90),Width=140};
        var b=new Button {Name="B",Text="Increment B",AccessibleName="Increment B",Location=new Point(180,90),Width=140};
        a.Click+=delegate {Count++;Save();}; b.Click+=delegate {Count+=10;Save();};
        var menu=new ContextMenu(); menu.MenuItems.Add("Owned menu item",delegate {});
        menu.Collapse+=delegate {MenuVisible=false;Save();};
        var opener=new Button {Name="OpenMenu",Text="Open menu",AccessibleName="Open menu",Location=new Point(340,90),Width=140};
        opener.Click+=delegate {Menus++;MenuVisible=true;Save();menu.Show(Entry,new Point(80,0));MenuVisible=false;Save();};
        Controls.Add(Entry);Controls.Add(a);Controls.Add(b);Controls.Add(opener);
        Shown+=delegate {
            ConsoleReady=AllocConsole();
            if(ConsoleReady) {
                IntPtr output=CreateFile("CONOUT$",0x40000000,3,IntPtr.Zero,3,0,IntPtr.Zero);
                int written;string value="GUI2API_READONLY_CONSOLE_20261025\r\n";
                ConsoleWritten=WriteConsole(output,value,value.Length,out written,IntPtr.Zero) && written==value.Length;
                CloseHandle(output);
            }
            Save();
        };
    }
    void Save() {
        if(!IsHandleCreated)return;
        string json=new JavaScriptSerializer().Serialize(new {window=Handle.ToInt64(),pid=System.Diagnostics.Process.GetCurrentProcess().Id,
            count=Count,text=Entry.Text,menus=Menus,menu_visible=MenuVisible,enters=Enters,console_ready=ConsoleReady,console_written=ConsoleWritten});
        string temporary=StatePath+".tmp";File.WriteAllText(temporary,json,Encoding.UTF8);
        if(File.Exists(StatePath))File.Delete(StatePath);File.Move(temporary,StatePath);
    }
    [STAThread] public static void Main(string[] args) {Application.EnableVisualStyles();Application.Run(new ReliabilityForm(args[0]));}
}
