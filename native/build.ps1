$ErrorActionPreference = 'Stop'
$nativeRoot = $PSScriptRoot
$projectRoot = Split-Path $nativeRoot -Parent
$outputDir = Join-Path $projectRoot '.build-tmp\native'
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsRoot = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$vsRoot) { throw 'Visual Studio x64 C++ tools are required.' }
$devCmd = Join-Path $vsRoot 'Common7\Tools\VsDevCmd.bat'
# This is compiler setup, not filesystem mutation through another shell.
$batch = Join-Path $outputDir 'compile.cmd'
$source = Join-Path $nativeRoot 'native_text.cpp'
$dll = Join-Path $outputDir 'wab_native_v5.dll'
@"
@call "$devCmd" -arch=x64 -host_arch=x64 >nul
@cl /nologo /LD /MT /O2 /std:c++17 /W4 /WX /DUNICODE /D_UNICODE "$source" /Fo"$outputDir\native_text.obj" /link /OUT:"$dll" /DYNAMICBASE /NXCOMPAT /INCREMENTAL:NO
@exit /b %errorlevel%
"@ | Set-Content -LiteralPath $batch -Encoding ascii
Push-Location $outputDir
try { & cmd.exe /d /c $batch; if ($LASTEXITCODE -ne 0) { throw "Native build failed: $LASTEXITCODE" } }
finally { Pop-Location }
Get-FileHash -LiteralPath $dll -Algorithm SHA256
