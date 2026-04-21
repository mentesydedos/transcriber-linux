# Ejecutar como Administrador
$action   = New-ScheduledTaskAction -Execute 'C:\Users\alfonsoa\transcriber\start_transcriber.bat' `
                                    -WorkingDirectory 'C:\Users\alfonsoa\transcriber'
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId 'alfonsoa' -LogonType S4U -RunLevel Highest

Register-ScheduledTask -TaskName 'TranscriberTV' `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Transcriptor TV 24x7 - arranca con Windows sin necesidad de login' `
    -Force

Write-Host "Tarea registrada. El transcriptor arrancara automaticamente al iniciar Windows." -ForegroundColor Green
