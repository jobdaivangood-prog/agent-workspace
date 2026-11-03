using System;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Forms;
using System.Reflection;

[assembly: AssemblyTitle("ReHome")]
[assembly: AssemblyDescription("Codex backup and restore")]
[assembly: AssemblyProduct("ReHome")]
[assembly: AssemblyVersion("0.1.41.0")]
[assembly: AssemblyFileVersion("0.1.41.1")]
[assembly: AssemblyInformationalVersion("0.1.41-beta.1")]

internal static class ReHome {
    private const string ProgramPin = "__PROGRAM_PIN__";
    private const string LauncherPin = "__LAUNCHER_PIN__";
    private static string Hash(string path) {
        using (var sha=SHA256.Create())
        using (var file=File.OpenRead(path))
            return BitConverter.ToString(sha.ComputeHash(file)).Replace("-", "").ToLowerInvariant();
    }
    private static string Quote(string value) { return "\""+value.Replace("\"","\\\"")+"\""; }
    [STAThread]
    private static int Main(string[] args) {
        bool check = args.Length==1 && args[0]=="--check";
        try {
            if (args.Length>0 && !check) throw new InvalidOperationException();
            string root=AppDomain.CurrentDomain.BaseDirectory;
            string launcher=Path.Combine(root,"Start-ReHome.ps1");
            if (Hash(Path.Combine(root,"Program","manifest.json"))!=ProgramPin || Hash(launcher)!=LauncherPin)
                throw new InvalidOperationException();
            var start=new ProcessStartInfo();
            start.FileName=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"WindowsPowerShell","v1.0","powershell.exe");
            start.Arguments="-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "+Quote(launcher)+(check?" -Check":"");
            start.WorkingDirectory=root;
            start.UseShellExecute=false;
            start.CreateNoWindow=true;
            // A parent PowerShell 7 process may export its own module path.
            // Use only the Windows PowerShell system modules in this child.
            start.EnvironmentVariables["PSModulePath"]=Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System),"WindowsPowerShell","v1.0","Modules");
            start.RedirectStandardOutput=check;
            start.RedirectStandardError=check;
            using(var process=Process.Start(start)) {
                if(check) {
                    Console.Write(process.StandardOutput.ReadToEnd());
                    // Do not forward arbitrary PowerShell error payloads.
                    process.StandardError.ReadToEnd();
                }
                process.WaitForExit();
                return process.ExitCode;
            }
        } catch {
            if(check) Console.WriteLine("{\"status\":\"launcher_verification_failed\",\"profile_applied\":false}");
            else {
                Application.EnableVisualStyles();
                MessageBox.Show("Не удалось открыть ReHome. Полностью распакуйте архив в обычную локальную папку и повторите запуск.",
                                "ReHome",MessageBoxButtons.OK,MessageBoxIcon.Information);
            }
            return 1;
        }
    }
}
