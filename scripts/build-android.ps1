param([string]$JavaHome, [string]$SdkPath, [switch]$Offline)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
if(!$JavaHome){if(Test-Path 'D:\android-studio\jbr'){$JavaHome='D:\android-studio\jbr'}else{$JavaHome=$env:JAVA_HOME}}
if(!$JavaHome -or !(Test-Path "$JavaHome\bin\java.exe")){throw 'Supply -JavaHome pointing to JDK 17 or 21'}
$env:JAVA_HOME=$JavaHome
if($SdkPath){Set-Content local.properties ('sdk.dir='+$SdkPath.Replace('\','/').Replace(':','\:'))}
if(!(Test-Path local.properties)){throw 'Supply -SdkPath or configure local.properties'}
python scripts/bootstrap.py
if($LASTEXITCODE){throw 'Dependency preparation failed'}
$cached=Get-ChildItem "$env:USERPROFILE\.gradle\wrapper\dists\gradle-8.14.3-bin" -Recurse -Filter gradle.bat -ErrorAction SilentlyContinue | Select-Object -First 1
$gradle=if($cached){$cached.FullName}else{Join-Path $taskRoot 'gradlew.bat'}
$arguments=@('--no-daemon','assembleDebug','lintDebug')
if($Offline){$arguments=@('--offline')+$arguments}
& $gradle @arguments
if($LASTEXITCODE){throw 'Android build/checks failed'}
New-Item -ItemType Directory -Force dist/android | Out-Null
Copy-Item app/build/outputs/apk/debug/app-debug.apk dist/android/VoiceTranslator-0.1.0-arm64-debug.apk
Get-FileHash dist/android/VoiceTranslator-0.1.0-arm64-debug.apk
