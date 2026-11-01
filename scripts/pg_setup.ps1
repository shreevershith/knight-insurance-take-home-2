# Creates the app's Postgres user and database from DATABASE_URL in .env (safe to run again).
# psql asks for the postgres admin password you set when installing PostgreSQL (up to 3 times).
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\pg_setup.ps1
$ErrorActionPreference = "Stop"
$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
$line = Get-Content (Join-Path $PSScriptRoot "..\.env") | Where-Object { $_ -match '^DATABASE_URL=' } | Select-Object -First 1
if (-not $line) { throw "DATABASE_URL not found in .env" }
if ($line -notmatch '://(?<user>[^:]+):(?<pass>[^@]+)@(?<host>[^:/]+):(?<port>\d+)/(?<db>\w+)') { throw "DATABASE_URL is not in the expected form" }
$u, $p, $h, $port, $db = $Matches.user, $Matches.pass, $Matches.host, $Matches.port, $Matches.db

function Admin([string]$sql) {
    & $psql -U postgres -h $h -p $port -d postgres -v ON_ERROR_STOP=1 -tAq -c $sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed (wrong postgres admin password?)" }
}

Write-Host "1/3 user '$u' (enter the postgres admin password when asked)"
$hasRole = Admin "SELECT 1 FROM pg_roles WHERE rolname = '$u'"
if ($hasRole -eq "1") { Admin "ALTER ROLE $u WITH LOGIN PASSWORD '$p'" | Out-Null }
else { Admin "CREATE ROLE $u WITH LOGIN PASSWORD '$p'" | Out-Null }

Write-Host "2/3 database '$db'"
$hasDb = Admin "SELECT 1 FROM pg_database WHERE datname = '$db'"
if ($hasDb -ne "1") { Admin "CREATE DATABASE $db OWNER $u" | Out-Null }
else { Admin "ALTER DATABASE $db OWNER TO $u" | Out-Null }

Write-Host "3/3 checking the app can log in"
$env:PGPASSWORD = $p
$who = & $psql -U $u -h $h -p $port -d $db -tAc "SELECT current_user"
Remove-Item Env:PGPASSWORD
if ($who -eq $u) { Write-Host "OK: the app can connect to '$db' as '$u'" -ForegroundColor Green }
else { Write-Host "FAIL: could not log in as '$u'" -ForegroundColor Red; exit 1 }
