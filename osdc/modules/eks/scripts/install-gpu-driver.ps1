# Installs the NVIDIA WDDM 595.97 driver on a Windows node.
# Used by bake-windows-gpu-ami.sh (AMI baking) and as a reference for the
# process-isolated GPU requirement (WDDM, not MCDM — see commit message).
$ErrorActionPreference = 'Stop'
$url = 'https://ec2-windows-nvidia-drivers.s3.us-east-1.amazonaws.com/grid-20.0/595.97_grid_win10_win11_server2022_server2025_64bit_dch_international_aws_swl.exe'
$out = 'C:\driver59597.exe'

Write-Output 'downloading driver...'
curl.exe -L -o $out $url
if (-not (Test-Path $out)) { Write-Output 'DOWNLOAD FAILED'; exit 1 }
Write-Output ("downloaded bytes=" + (Get-Item $out).Length)

Write-Output 'installing silently (no reboot)...'
$p = Start-Process -FilePath $out -ArgumentList '-s','-noreboot' -Wait -PassThru
Write-Output ("installer exit code=" + $p.ExitCode)
Start-Sleep -Seconds 5

Write-Output '=== NVIDIA-SMI ==='
& 'C:\Windows\System32\nvidia-smi.exe' 2>&1 | Out-String | Write-Output
Write-Output '=== GPU DRIVER DONE ==='
