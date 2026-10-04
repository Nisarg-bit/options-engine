# Dot-source before running a script by hand:  . .\env.ps1
$here = $PSScriptRoot
$env:PYTHONPATH = ((@('.', 'pipeline','quant','strategy','signals','tracking','service','research','tools') | ForEach-Object { Join-Path $here $_ }) -join ';')
Write-Host "PYTHONPATH set for options-engine"
