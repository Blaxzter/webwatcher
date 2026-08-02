<#
.SYNOPSIS
    Überträgt den Watcher auf den Server - ohne .venv, Laufzeitdaten und Secrets.

.DESCRIPTION
    scp kennt kein --exclude und würde die ~160 MB grosse .venv mitschleppen
    (alles andere zusammen sind ~165 KB). Ausserdem legt "scp -r ordner ziel"
    beim zweiten Aufruf ein verschachteltes ziel/ordner an, sobald das Ziel
    schon existiert.

    Stattdessen: lokal ein tar.gz packen, per scp hochladen, remote auspacken.
    Der Umweg über die Datei (statt tar | ssh) sorgt dafür, dass ssh nach einem
    Passwort oder einer Key-Passphrase fragen kann - bei einer Pipe ist dessen
    stdin belegt.

.EXAMPLE
    .\deploy.ps1
.EXAMPLE
    .\deploy.ps1 -Restart
.EXAMPLE
    # Auch die .env mitschicken - noetig nach neuem Token oder neuer Chat-ID
    .\deploy.ps1 -IncludeEnv -Restart
.EXAMPLE
    .\deploy.ps1 -Remote meinserver -RemoteDir /srv/watcher
.EXAMPLE
    .\deploy.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$Remote = "hetzner",
    [string]$RemoteDir = "~/docker/watcher",
    [switch]$Restart,
    [switch]$IncludeEnv,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# config.yaml geht mit, weil du sie lokal mit "webwatcher pick" pflegst.
$excludes = @(
    "--exclude=.venv"
    "--exclude=__pycache__"
    "--exclude=*.pyc"
    "--exclude=*.egg-info"
    "--exclude=.ruff_cache"
    "--exclude=data"
    "--exclude=*.sqlite3*"
    "--exclude=.git"
)

# Die .env bleibt normalerweise aus, damit der Server seine eigenen Zugangsdaten
# behält. Wer sie lokal pflegt (neue Chat-ID, neuer Token), nimmt -IncludeEnv.
if (-not $IncludeEnv) { $excludes += "--exclude=.env" }

$tempDir = [System.IO.Path]::GetTempPath()
$archiveName = "webwatcher-deploy.tar.gz"
$archive = Join-Path $tempDir $archiveName
$remoteArchive = "/tmp/webwatcher-deploy.tar.gz"

try {
    # Je nach PATH ist "tar" entweder das Windows-tar (bsdtar) oder Gits GNU tar.
    # GNU tar deutet "C:\..." hinter -f als Rechnernamen und bricht mit
    # "Cannot connect to C: resolve failed" ab. Deshalb wird das Archiv aus dem
    # temporaeren Verzeichnis heraus unter einem relativen Namen erzeugt - das
    # verstehen beide. Der Pfad hinter -C ist davon nicht betroffen.
    Push-Location $tempDir
    try {
        tar czf $archiveName -C $PSScriptRoot @excludes .
        if ($LASTEXITCODE -ne 0) { throw "tar ist fehlgeschlagen (Exitcode $LASTEXITCODE)" }
        $files = @(tar tzf $archiveName | Where-Object { $_ -notmatch '/$' })
    }
    finally { Pop-Location }

    $size = [math]::Round((Get-Item $archive).Length / 1KB)

    if ($DryRun) {
        Write-Host "Wuerde nach ${Remote}:${RemoteDir} uebertragen ($($files.Count) Dateien, $size KB):"
        $files | ForEach-Object { "  " + ($_ -replace '^\./', '') }
        return
    }

    Write-Host "Uebertrage $($files.Count) Dateien ($size KB) nach ${Remote}:${RemoteDir} ..."
    scp $archive "${Remote}:${remoteArchive}"
    if ($LASTEXITCODE -ne 0) { throw "scp ist fehlgeschlagen (Exitcode $LASTEXITCODE)" }

    ssh $Remote "mkdir -p $RemoteDir && tar xzf $remoteArchive -C $RemoteDir && rm -f $remoteArchive"
    if ($LASTEXITCODE -ne 0) { throw "Auspacken auf dem Server ist fehlgeschlagen (Exitcode $LASTEXITCODE)" }

    if (-not $IncludeEnv) {
        # Sonst wundert man sich, warum eine neue Chat-ID auf dem Server fehlt.
        Write-Host "Hinweis: .env wurde NICHT uebertragen (mit -IncludeEnv mitschicken)."
    }

    if ($Restart) {
        # "up -d" statt "restart": nur das liest eine geaenderte .env neu ein.
        Write-Host "Baue und starte neu ..."
        ssh $Remote "cd $RemoteDir && docker compose up -d --build"
        if ($LASTEXITCODE -ne 0) { throw "docker compose ist fehlgeschlagen (Exitcode $LASTEXITCODE)" }
        Write-Host "Fertig. Logs:  ssh $Remote 'cd $RemoteDir && docker compose logs -f'"
    }
    else {
        Write-Host "Uebertragen. Auf dem Server aktivieren:"
        Write-Host "  ssh $Remote 'cd $RemoteDir && docker compose up -d --build'"
    }
}
finally {
    Remove-Item $archive -Force -ErrorAction SilentlyContinue
}
